#!/usr/bin/python
# Copyright (c) 2011 Citrix Systems, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; version 2.1 only. with the special
# exception on linking described in file LICENSE.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

##
# Bootloader for EL-based distros that support Xen.
#
# We keep all logic for booting these distros contained within this file
# where possible.

# Some brief documentation of the other-config keys this tool knows about:
#
# install-repository: Required.  Path to a repository; 'http', 'https', or
#    'nfs'.  Should be specified as would be used by the target installer, not
#    including prefixes, e.g. method=.
#
# install-vnc:  Default: false.  Use VNC where available during the
#    installation.
#
# install-vncpasswd:  Default: empty.  The VNC password to use, when providing
#    one is possible via the command-line of the target distro
#
# install-round:  Default: 1.  The current bootloader round.  Not to be edited
#    by the user
#
# install-distro:  Default: 'rhlike'.  The general distribution type.  Currently
#    supported values are 'rhlike', 'sleslike', and 'debianlike'.
#
# install-arch:  Default: i386.  The architecture to install.

import sys
import subprocess
import os
import os.path
import shutil
import getopt
import tempfile
import urllib2
import traceback
import logging
import re
import itertools
import XenAPI
import xcp.cmd
import xcp.logger
from xen.lowlevel import xs

sys.path.append("/usr/lib/python")

BOOTDIR = "/var/run/xend/boot"
PYGRUB = "/usr/bin/pygrub"
DEBUG_SWITCH = "/var/run/nonpersistent/linux-guest-loader.debug"
PROGRAM_NAME = "eliloader"

never_latch = False
# Set this if you want 2nd round booting to never stop.

class PygrubError(Exception):
    def __init__(self, rc, err):
        # Pygrub reports errors with a Runtime exception.
        m = re.search('RuntimeError: (.*)$', err)
        self.value = "Pygrub error (%d): %s" % (rc, m.group(0))
    def __str__(self):
        return repr(self.value)

RPC_SUCCESS = "Success"

(
    DISTRO_RHLIKE,
    DISTRO_SLESLIKE,
    DISTRO_DEBIANLIKE,
    DISTRO_PYGRUB # Distro media bootable via pygrub
    ) = range(4)

distros = { "rhlike" : DISTRO_RHLIKE, "sleslike" : DISTRO_SLESLIKE, "debianlike": DISTRO_DEBIANLIKE, "pygrub" : DISTRO_PYGRUB }

rounds = {
    DISTRO_RHLIKE: 2, DISTRO_SLESLIKE: 2, DISTRO_DEBIANLIKE: 1, DISTRO_PYGRUB: 1
    }

guest_installer_dir = "/opt/xensource/packages/files/guest-installer"
mapfiles = []
if os.path.exists(guest_installer_dir):
    mapfiles = [ f for f in os.listdir(guest_installer_dir) if f.endswith('.map') ]

# We can sometimes tweak an installer's initrd to give it extra features, e.g.
# CD installs in PV guests.  These dictionaries specify the cpio archive used
# as a source for adding files from given an initrd whose md5sum when taken
# directly from the vendor's CD as the key.  Note that we use cpio archives
# because although it doesn't seem to be supported yet, it might that in future
# we can simply cat multiple archives together into a single image, and the
# Linux loader will do the right thing.

# Later initrds are cpio.gz archives
cpio_initrd_fixups = {}
# Earlier initrds are ext2.gz filesystems
ext2_initrd_fixups = {}

# Update cpio_initrd_fixups[] and ext2_initrd_fixups[] from the map files dumped
# in guest_installer_dir by the *-guest-installer components
for f in mapfiles:
    fd = open(os.path.join(guest_installer_dir, f))
    lineno = 0
    for line in fd:
        lineno += 1
        line = line.strip()
        if len(line) == 0 or line.startswith('#'):
            continue
        try:
            initrd_md5sum, initrd_type, overlay_fname, distro = line.split(None, 3)
        except:
            raise Exception, "missing field in file %s/%s line %d" % (guest_installer_dir, fd.name, lineno)
        if initrd_type == "cpio":
            cpio_initrd_fixups[initrd_md5sum] = overlay_fname
        elif initrd_type == "ext2":
            ext2_initrd_fixups[initrd_md5sum] = overlay_fname
        else:
            raise Exception, "incorrect initrd_type in file %s/%s line %d: must be cpio or ext2" % \
                (guest_installer_dir, fd.name, lineno)
    fd.close()

pv_kernel_max_size =  32 * 1024 * 1024
pv_initrd_max_size = 128 * 1024 * 1024
copy_block_size    =   1 * 1024 * 1024

#### EXCEPTIONS

class UsageError(Exception):
    pass

class APILevelException(Exception):
    exname = "INTERNAL_ERROR"
    def apifmt(self):
        rc = self.exname + "\n"
        for a in self.args:
            rc += a + "\n"
        return rc

class UnsupportedInstallMethod(APILevelException):
    exname = "UNSUPPORTED_INSTALL_METHOD"

class SupportPackageMissing(APILevelException):
    exname = "SUPPORT_PACKAGE_MISSING"

class InvalidSource(APILevelException):
    exname = "INVALID_SOURCE"

class ResourceTooLarge(APILevelException):
    exname = "LIMITS"

class ResourceAccessError(Exception):
    def __init__(self, source):
        self.source = source

class MountFailureException(Exception):
    pass

##### UTILITY FUNCTIONS

def mount(dev, mountpoint, options = None, fstype = None):
    cmd = ['/bin/mount']
    if options:
        assert type(options) == list

    if fstype:
        cmd.append('-t')
        cmd.append(fstype)

    if options:
        cmd.append("-o")
        cmd.append(",".join(options))

    cmd.append(dev)
    cmd.append(mountpoint)

    rc = xcp.cmd.runCmd(cmd, False, False)
    if rc != 0:
        raise MountFailureException, cmd

def umount(mountpoint):
    xcp.cmd.runCmd(["umount", mountpoint])

def get_decompressor(filename):
    archive = open(filename)
    header = archive.read(2)
    archive.close()

    if header == "\037\213": # Gzip compressed
        return ["/bin/zcat"]
    elif header == "\x5d\x00": # Lzma compressed
        return ["/usr/bin/xzcat", "--format=lzma"]
    else: # Assume uncompressed
        return None

# Copy from one fd to another
def copyfd(fromfd, tofd, limit):
    bytes_so_far = 0

    while bytes_so_far <= limit:
        block = fromfd.read(copy_block_size)
        l = len(block)

        if l == 0:
            break

        bytes_so_far += l
        tofd.write(block)
    else:
        return bytes_so_far, False

    return bytes_so_far, True

# Creation of an NfsRepo object triggers a mount, and the mountpoint is stored int obj.mntpoint.
# The umount is done automatically when the object goes out of scope
class NfsRepo:
    # repo is nfs:server:/path/to/repo or nfs://server/path/to/repo or nfs://server:/path/to/repo
    def __init__(self, repo):
        self.mntpoint = None
        xcp.logger.debug("Mounting NFS repo " + repo)

        # we deal with RHEL-like NFS paths - if it's a SLES one then
        # turn it into something we can understand first:
        if repo.startswith("nfs://"):
            rest = repo[6:]
            if not "/" in rest:
                raise InvalidSource, "NFS path was not in a valid format"
            server, dir = rest.split("/", 1)
            dir = "/" + dir
            server = server.rstrip(":")
        else:
            # work out the components:
            [_, server, dir] = repo.split(':', 2)

        if dir[0] != '/':
            raise InvalidSource, "Directory part of NFS path was not an absolute path."

        # make a mountpoint:
        self.mntpoint = tempfile.mkdtemp(dir = '/tmp', prefix = 'nfs-repo-')
        try:
            mount('%s:%s' % (server, dir), self.mntpoint, fstype = "nfs", options = ['ro'])
        except MountFailureException, e:
            # Mount failed.  Re-raise as InvalidSource.
            umount(self.mntpoint)
            os.rmdir(self.mntpoint)
            self.mntpoint = None
            raise InvalidSource, "nfs repo %s" % repo

    def __del__(self):
        # if we're getting called due to an unhandled exception, the
        # os module may have already been unloaded
        import os
        if self.mntpoint:
            umount(self.mntpoint)
            os.rmdir(self.mntpoint)

# Creation of an CdromRepo object triggers a mount, and the mountpoint is stored int obj.mntpoint.
# The umount is done automatically when the object goes out of scope
class CdromRepo:
    # img is a dev node
    def __init__(self, img):
        # make a mountpoint:
        xcp.logger.debug("Mounting CD repo " + img)
        self.mntpoint = tempfile.mkdtemp(dir = '/tmp', prefix = 'cdrom-repo-')
        try:
            mount(img, self.mntpoint, fstype = "iso9660", options = ['ro'])
        except MountFailureException, e:
            # Mount failed.  Re-raise as InvalidSource.
            umount(self.mntpoint)
            os.rmdir(self.mntpoint)
            self.mntpoint = None
            raise InvalidSource, "cdrom repo %s" % img

    def __del__(self):
        # if we're getting called due to an unhandled exception, the
        # os module may have already been unloaded
        import os
        if self.mntpoint:
            umount(self.mntpoint)
            os.rmdir(self.mntpoint)

# Modified from host-installer.hg/util.py
# source may be
#  http://blah
#  ftp://blah
#  file://blah
#
# Raises ResourceAccessError or InvalidSource.
#
def fetchFile(source, dest, limit):

    if source[:5] != 'http:' and source[:5] != 'file:' and source[:4] != 'ftp:':
        raise InvalidSource, "Unknown source type."

    # This something that can be fetched using urllib2
    xcp.logger.debug("Fetching '%s' to '%s'" % (source, dest))

    # Actually get the file
    try:
        fd = urllib2.urlopen(source)
        try:
            length = int(fd.info().getheader('content-length', None))
        except (ValueError, TypeError):
            length = None
    # Catch various errors and reraise as ResourceAccessError
    # urlopen can raise OSError if a file:// URL is not found,
    # HTTPError for HTTP error response codes,
    # URLError for network issues, bad hostname, malformed URL, etc., and
    # IOError for some FTP errors.
    except (OSError, urllib2.HTTPError, urllib2.URLError, IOError):
        log_exception("ERROR: ", traceback.format_exc())
        raise ResourceAccessError(source)
    fd_dest = open(dest, 'wb')

    dest_len, success = copyfd(fd, fd_dest, limit)

    dbg = ""
    if length is not None:
        dbg = "  expecting %d bytes, " % (length, )
    xcp.logger.debug(dbg + "got %d bytes, limit %d bytes" % (dest_len, limit))

    fd_dest.close()
    fd.close()

    if not success:
        raise ResourceTooLarge("File '%s' exceeds limit of %d bytes"
                               % (source, limit))

    if length is not None and length != dest_len:
        raise IOError("Closed connection during download")

# Test existence of a file
# just return True for "exists" or False for "does not exist"
#
# Raises InvalidSource.
def checkFile(source):

    if source[:5] != 'http:' and source[:5] != 'file:' and source[:4] != 'ftp:':
        raise InvalidSource, "Unknown source type."

    # This something that can be fetched using urllib2
    xcp.logger.debug("Checking " + source)
    try:
        request = urllib2.Request(source)
        if source[:5] == 'http:':
            request.get_method = lambda : 'HEAD'
        fd = urllib2.urlopen(request)
        fd.close()
        return True
    except StandardError:
        return False

def close_mkstemp(dir = None, prefix = 'tmp'):
    fd, name = tempfile.mkstemp(dir = dir, prefix = prefix)
    os.close(fd)
    return name

def canonicaliseOtherConfig(vm_uuid):
    session = XenAPI.xapi_local()
    session.login_with_password("", "", "", PROGRAM_NAME)
    try:
        vm = session.xenapi.VM.get_by_uuid(vm_uuid)
        other_config = session.xenapi.VM.get_other_config(vm)
    finally:
        session.logout()

    def collect(d, k, default = None):
        if d.has_key(k):
            return d[k]
        else:
            return default
    rc = { 'install-repository': collect(other_config, 'install-repository'),
           'install-vnc':        collect(other_config, 'install-vnc', "false") in ["1", "true"],
           'install-vncpasswd':  collect(other_config, 'install-vncpasswd'),
           'install-distro':     collect(other_config, 'install-distro', 'rhlike'),
           'install-round':      collect(other_config, 'install-round', '1'),
           'install-arch':       collect(other_config, 'install-arch', 'i386'),
           'install-args':       collect(other_config, 'install-args', None),
           'install-kernel':     collect(other_config, 'install-kernel', None),
           'install-ramdisk':    collect(other_config, 'install-ramdisk', None),
           'install-proxy':      collect(other_config, 'install-proxy', None),
           'debian-release':     collect(other_config, 'debian-release') }
    return rc

def propagatePostinstallLimits(session, vm):

    platform = session.xenapi.VM.get_platform(vm)

    try:
        key_from = "pv-postinstall-kernel-max-size"
        key_to   = "pv-kernel-max-size"
        if key_from in platform:
            session.xenapi.VM.remove_from_platform(vm, key_to)
            session.xenapi.VM.add_to_platform(vm, key_to, platform[key_from])
            session.xenapi.VM.remove_from_platform(vm, key_from)
    except StandardError:
        pass

    try:
        key_from = "pv-postinstall-ramdisk-max-size"
        key_to   = "pv-ramdisk-max-size"
        if key_from in platform:
            session.xenapi.VM.remove_from_platform(vm, key_to)
            session.xenapi.VM.add_to_platform(vm, key_to, platform[key_from])
            session.xenapi.VM.remove_from_platform(vm, key_from)
    except StandardError:
        pass

def switchBootloader(vm_uuid, target_bootloader = "pygrub"):
    if never_latch: return
    session = XenAPI.xapi_local()
    session.login_with_password("", "", "", PROGRAM_NAME)
    try:
        xcp.logger.debug("Switching to " + target_bootloader)
        vm = session.xenapi.VM.get_by_uuid(vm_uuid)
        session.xenapi.VM.set_PV_bootloader(vm, target_bootloader)
        propagatePostinstallLimits(session, vm)
    finally:
        session.logout()

def unpack_cpio_initrd(filename, working_dir):
    xcp.logger.debug("Unpacking cpio '%s' into '%s'" % (filename, working_dir))
    prog = get_decompressor(filename)

    if prog is not None:
        decomp = subprocess.Popen(prog + [filename], stdout = subprocess.PIPE)
        source = decomp.stdout
    else:
        source = open(filename)

    cpio = subprocess.Popen(["/bin/cpio", "-idu", "--quiet"], cwd = working_dir,
                            stdin = subprocess.PIPE)

    dest_len, success = copyfd(source, cpio.stdin, pv_initrd_max_size)
    xcp.logger.debug("  got %d bytes, limit %d bytes" % (dest_len, pv_initrd_max_size))

    cpio.stdin.close()
    cpio.wait()

    source.close()
    if prog is not None:
        decomp.wait()

    if not success:
        raise ResourceTooLarge("Unpacking cpio '%s' exceeds limit of %d bytes"
                               % (filename, pv_initrd_max_size))

def mount_ext2_initrd(infile, outfile, working_dir):
    xcp.logger.debug("Mounting ext2 '%s' on '%s'" % (infile, outfile))
    prog = get_decompressor(infile)

    if prog is not None:
        decomp = subprocess.Popen(prog + [infile], stdout = subprocess.PIPE)
        source = decomp.stdout
    else:
        source = open(infile)

    dest = open(outfile, "w")

    dest_len, success = copyfd(source, dest, pv_initrd_max_size)
    xcp.logger.debug("  got %d bytes, limit %d bytes" % (dest_len, pv_initrd_max_size))

    dest.close()

    source.close()
    if prog is not None:
        decomp.wait()

    if not success:
        raise ResourceTooLarge("Unpacking cpio '%s' exceeds limit of %d bytes"
                               % (infile, pv_initrd_max_size))

    mount(outfile, working_dir, options = ['loop'])

def md5sum(filename):
    p = subprocess.Popen(["md5sum", filename], stdout=subprocess.PIPE)
    stdout, _ = p.communicate()

    if p.returncode != 0:
        raise InvalidSource("md5sum command failed.")
    return stdout.split()[0]

def log_exception(prefix, backtrace):
    for line in backtrace.strip().split("\n"):
        xcp.logger.debug(prefix + line)

#### INITRD TWEAKING

def mkcpio(working_dir, output_file):
    """ Make a cpio archive containg the files in working_dir, writing the
    archive to output_file.  It will be uncompressed. """

    xcp.logger.debug("Building initrd from " + working_dir)

    # set output_file to be a full path so that we don't create the output
    # file under the new working directory of the cpio process.
    output_file = os.path.realpath(output_file)
    cpio = subprocess.Popen(["/bin/cpio", "-F", output_file, "-oH", "newc",
                             "--quiet"], cwd = working_dir,
                            stdin = subprocess.PIPE, stdout = None)

    for root, ds, files in os.walk(working_dir):
        assert root.startswith(working_dir), "Root of current walk path starts with original walk path"
        base = root[len(working_dir) + 1:]
        for f in files + ds:
            path = os.path.join(base, f)
            cpio.stdin.write(path + "\n")

    cpio.communicate()

def tweak_initrd(filename):
    """ Patch an initrd with custom files if they are available.  Returns the
    filename of a patched initrd that should be used instead of the file as
    passed in as filename.  The caller is responsible for removing the old
    version of the initrd. """

    digest = md5sum(filename)
    initrd_path = None
    _initrd_path = None

    xcp.logger.debug(filename + " has MD5 " + digest)

    if cpio_initrd_fixups.has_key(digest):
        # we can patch this initrd, let's unpack it to a temporary directory:
        xcp.logger.debug("Fixup with " + cpio_initrd_fixups[digest])
        working_dir = tempfile.mkdtemp(dir = "/tmp", prefix = "initrd-fixup-")
        cpio_overlay = os.path.join(guest_installer_dir, cpio_initrd_fixups[digest])
        if not os.path.isfile(cpio_overlay):
            raise SupportPackageMissing, "Dom0 does not contain a required file: %s" % cpio_overlay

        try:
            try:
                # unpack the vendor initrd, then unpack our changes over it:
                unpack_cpio_initrd(filename, working_dir)
                unpack_cpio_initrd(cpio_overlay, working_dir)

                # now repack to make the final image:
                _initrd_path = close_mkstemp(dir = BOOTDIR, prefix="tweaked-initrd-")
                mkcpio(working_dir, _initrd_path)
            except:
                xcp.logger.debug("Cleaning '%s' and '%s'" % (working_dir, _initrd_path))
                raise
            else:
                initrd_path = _initrd_path
                _initrd_path = None
        finally:
            # clean up the working_dir tree
            shutil.rmtree(working_dir)
            if _initrd_path:
                os.unlink(_initrd_path)

    elif ext2_initrd_fixups.has_key(digest):
        # we can patch this initrd, let's unpack it to a temporary directory:
        working_dir = tempfile.mkdtemp(dir = "/tmp", prefix = "initrd-fixup-")
        cpio_overlay = os.path.join(guest_installer_dir, ext2_initrd_fixups[digest])
        if not os.path.isfile(cpio_overlay):
            raise SupportPackageMissing, "Dom0 does not contain a required file: %s" % cpio_overlay

        try:
            try:
                # unpack the vendor initrd, then unpack our changes over it:
                _initrd_path = close_mkstemp(dir = BOOTDIR, prefix="tweaked-initrd-")
                mount_ext2_initrd(filename, _initrd_path, working_dir)
                unpack_cpio_initrd(cpio_overlay, working_dir)
            except:
                xcp.logger.debug("Cleaning '%s' and '%s'" % (working_dir, _initrd_path))
                raise
            else:
                initrd_path = _initrd_path
                _initrd_path = None
        finally:
            umount(working_dir)
            shutil.rmtree(working_dir)
            if _initrd_path:
                os.unlink(_initrd_path)

    return initrd_path

def tweak_bootable_disk(vm):
    if never_latch: return
    session = XenAPI.xapi_local()
    session.xenapi.login_with_password("", "", "", PROGRAM_NAME)
    try:
        # get all VBDs, set bootable = (device == 0):
        vm_ref = session.xenapi.VM.get_by_uuid(vm)
        vbds = session.xenapi.VM.get_VBDs(vm_ref)

        for vbd in vbds:
            session.xenapi.VBD.set_bootable(vbd, session.xenapi.VBD.get_userdevice(vbd) == "0")
    finally:
        session.logout()

##### DISTRO-SPECIFIC CODE

def rhel_first_boot_handler(vm, repo_url):
    need_clean = True

    if checkFile(repo_url + "images/xen/vmlinuz"):
        vmlinuz_suburl = "images/xen/vmlinuz"
        ramdisk_suburl = "images/xen/initrd.img"
    else:
        vmlinuz_suburl = "isolinux/vmlinuz"
        ramdisk_suburl = "isolinux/initrd.img"

    vmlinuz_file = close_mkstemp(dir = BOOTDIR, prefix = "vmlinuz-")
    ramdisk_file = close_mkstemp(dir = BOOTDIR, prefix = "ramdisk-")

    # download the kernel and ramdisk:
    vmlinuz_url = repo_url + vmlinuz_suburl
    ramdisk_url = repo_url + ramdisk_suburl
    try:
        try:
            fetchFile(vmlinuz_url, vmlinuz_file, pv_kernel_max_size)
            fetchFile(ramdisk_url, ramdisk_file, pv_initrd_max_size)

            modified_ramdisk = tweak_initrd(ramdisk_file)
            if modified_ramdisk:
                os.unlink(ramdisk_file)
                ramdisk_file = modified_ramdisk
        except:
            xcp.logger.debug("Cleaning '%s' and '%s'" % (vmlinuz_file, ramdisk_file))
            raise
        else:
            need_clean = False

    finally:
        if need_clean:
            os.unlink(vmlinuz_file)
            os.unlink(ramdisk_file)

    return vmlinuz_file, ramdisk_file

# Return the extra arg needed by RHEL kernel for it to locate installation media
def rhel_first_boot_args(repo):
    if True in [ repo.startswith(x) for x in ("http", "ftp", "nfs") ]:
        if not repo.endswith("/"):
            return "method=%s/" % repo
    if repo == "cdrom":
        return ''
    return "method=%s" % repo

def sles_first_boot_handler(vm, repo_url, other_config):

    # look for the xen kernel and initrd in boot first:
    if other_config['install-arch'] == 'x86_64':
        bootdir =      'boot/x86_64/'
        kernel_fname = 'vmlinuz-xen'
        initrd_fname = 'initrd-xen'
    else:
        bootdir =      'boot/i386/'
        kernel_fname = 'vmlinuz-xenpae'
        initrd_fname = 'initrd-xenpae'

    vmlinuz_url = repo_url + bootdir + kernel_fname
    vmlinuz_file = close_mkstemp(dir = BOOTDIR, prefix = "vmlinuz-")
    ramdisk_url = repo_url + bootdir + initrd_fname
    ramdisk_file = close_mkstemp(dir = BOOTDIR, prefix = "ramdisk-")
    try:
        fetchFile(vmlinuz_url, vmlinuz_file, pv_kernel_max_size)
        fetchFile(ramdisk_url, ramdisk_file, pv_initrd_max_size)
    except:
        xcp.logger.debug("Cleaning '%s' and '%s'" % (vmlinuz_file, ramdisk_file))
        os.unlink(vmlinuz_file)
        os.unlink(ramdisk_file)
        raise

    return vmlinuz_file, ramdisk_file

# Return the extra arg needed by SLES kernel for it to locate installation media
def sles_first_boot_args(repo):
    args = ""
    if repo == "cdrom":
        # this should really be "install=cd", but the sles installer interprets
        # the disk as a harddrive.  If/when we fix that, we will need to replace
        # the following line.
        args = args + " install=hd"
    else:
        if True in [ repo.startswith(x) for x in ("http", "ftp", "nfs") ]:
            if not repo.endswith("/"):
                args = args + " install=%s/" % repo
            else:
                args = args + " install=%s" % repo
    return args;

def debian_first_boot_handler(vm, repo_url, other_config):

    if not other_config.has_key('debian-release'):
        raise UnsupportedInstallMethod, \
            "other-config:debian-release was not set to an appropriate value, " \
            "and this is required for the selected distribution type."
    if not other_config.has_key('install-arch'):
        raise UnsupportedInstallMethod, \
            "other-config:install-arch was not set to an appropriate value, " \
            "and this is required for the selected distribution type."

    if other_config['install-repository'] == "cdrom":
        cdrom_dirs = { 'i386': 'install.386/',
                       'amd64': 'install.amd/',
                       'x86_64': 'install.amd/' }
        vmlinuz_url = repo_url + cdrom_dirs[other_config['install-arch']] + "xen/vmlinuz"
        ramdisk_url = repo_url + cdrom_dirs[other_config['install-arch']] + "xen/initrd.gz"
        if not checkFile(vmlinuz_url):
            vmlinuz_url = repo_url + cdrom_dirs[other_config['install-arch']] + "vmlinuz"
            ramdisk_url = repo_url + cdrom_dirs[other_config['install-arch']] + "initrd.gz"
        if not checkFile(vmlinuz_url):
            vmlinuz_url = repo_url + "install/vmlinuz"
            ramdisk_url = repo_url + "install/initrd.gz"
    else:
        comp = repo_url.split('/dists/', 1)
        if len(comp) != 2 or comp[1].replace('/','') == "":
            repo_url += "dists/%s/" % other_config['debian-release']
        boot_dir = "main/installer-%s/current/images/netboot/xen/" % other_config['install-arch']
        vmlinuz_url = repo_url + boot_dir + "vmlinuz"
        ramdisk_url = repo_url + boot_dir + "initrd.gz"

    # download the kernel and ramdisk:
    vmlinuz_file = close_mkstemp(dir = BOOTDIR, prefix = "vmlinuz-")
    ramdisk_file = close_mkstemp(dir = BOOTDIR, prefix = "ramdisk-")

    try:
        fetchFile(vmlinuz_url, vmlinuz_file, pv_kernel_max_size)
        fetchFile(ramdisk_url, ramdisk_file, pv_initrd_max_size)
    except:
        xcp.logger.debug("Cleaning '%s' and '%s'" % (vmlinuz_file, ramdisk_file))
        os.unlink(vmlinuz_file)
        os.unlink(ramdisk_file)
        raise

    # Possibly apply tweaks to initrd.
    modified_ramdisk = tweak_initrd(ramdisk_file)
    if modified_ramdisk:
        os.unlink(ramdisk_file)
        ramdisk_file = modified_ramdisk

    return vmlinuz_file, ramdisk_file

def debian_first_boot_args(repo):
    return ""

def pygrub_first_boot_handler(vm_uuid, repo_url, other_config):
    def pygrub_parse(s):
        if not s.startswith("linux "):
            raise InvalidSource, "Syntax error parsing pygrub output, linux prefix missing"

        s = s[6:]

        ret = {'ramdisk': None, 'args': None}

        while s != "":
            if s[0] == "(":
                idx = s.find(")")
                if idx == -1:
                    raise InvalidSource, "Syntax error parsing pygrub output, closing parenthesis missing"
                item = s[1:idx]
                s = s[idx+1:]

                idx = item.find(" ")
                if idx == -1:
                    raise InvalidSource, "Syntax error parsing pygrub output, key value separator missing"
                key = item[:idx]
                val = item[idx+1:]

                ret[key] = val

            else:
                raise InvalidSource, "Syntax error parsing pygrub output, opening parenthesis missing"
        return ret

    if other_config['install-repository'] == "cdrom":
        (rc, out, err) = xcp.cmd.runCmd([PYGRUB] + sys.argv[1:], True, True)
        if rc != 0:
            raise InvalidSource, "Error %d running %s" % (rc,PYGRUB)

        output = pygrub_parse(out)

        if not output.has_key('kernel'):
            raise InvalidSource, "No kernel in pygrub output"

        return output['kernel'], output['ramdisk']
    else:
        if not other_config.has_key('install-kernel') or other_config['install-kernel'] is None:
            raise InvalidSource, "install-distro=pygrub requires install-kernel for network boot"

        # download the kernel and ramdisk:
        vmlinuz_url = repo_url + other_config['install-kernel']
        vmlinuz_file = close_mkstemp(dir = BOOTDIR, prefix = "vmlinuz-")

        if other_config.has_key('install-ramdisk') and other_config['install-ramdisk'] is not None:
            ramdisk_url = repo_url + other_config['install-ramdisk']
            ramdisk_file = close_mkstemp(dir = BOOTDIR, prefix = "ramdisk-")
        else:
            ramdisk_url = None
            ramdisk_file = None

        try:
            fetchFile(vmlinuz_url, vmlinuz_file, pv_kernel_max_size)
            if ramdisk_url is not None and ramdisk_file is not None:
                fetchFile(ramdisk_url, ramdisk_file, pv_initrd_max_size)
        except:
            os.unlink(vmlinuz_file)
            xcp.logger.debug("Cleaning '%s' and '%s'" % (vmlinuz_file, ramdisk_file))
            if ramdisk_file is not None:
                os.unlink(ramdisk_file)
            raise

        return vmlinuz_file, ramdisk_file

def pygrub_first_boot_args(repo):
    return ""

##### MAIN HANDLERS

def handle_first_boot(vm, img, args, other_config):
    if other_config['install-distro'] not in distros.keys():
        raise RuntimeError, "other-config:install-distro was not present or known."
    distro = distros[other_config['install-distro']]

    repo = other_config['install-repository']
    vnc = other_config['install-vnc']
    vncpasswd = other_config['install-vncpasswd']

    # extract the kernel and ramdisk

    # sanity check repo:
    if repo == "cdrom":
        pass
    elif repo and True in [repo.startswith(x) for x in ['http', 'ftp', 'nfs']]:
        pass
    else:
        raise UnsupportedInstallMethod, \
            "other-config:install-repository was not set to an appropriate value, " \
            "and this is required for the selected distribution type."

    # calculate repo_url, a prefix that can be passed into fetchFile
    if repo == "cdrom":
        # CdromRepo.__init__ triggers a mount.  CdromRepo.__del__ does the umount.
        cdrom_repo = CdromRepo(img)
        repo_url = "file://%s/" % cdrom_repo.mntpoint
    elif repo.startswith("nfs"):
        # NfsRepo.__init__ triggers a mount.  NfsRepo.__del__ does the umount.
        nfs_repo = NfsRepo(repo)
        repo_url = "file://%s/" % nfs_repo.mntpoint
    else:
        repo_url = repo
        if not repo_url.endswith("/"):
            repo_url += "/"

    # invoke distro specific handler for extraction of kernel and ramdisk
    if distro == DISTRO_RHLIKE:
        kernel, ramdisk = rhel_first_boot_handler(vm, repo_url)
    elif distro == DISTRO_SLESLIKE:
        kernel, ramdisk = sles_first_boot_handler(vm, repo_url, other_config)
    elif distro == DISTRO_DEBIANLIKE:
        kernel, ramdisk = debian_first_boot_handler(vm, repo_url, other_config)
    elif distro == DISTRO_PYGRUB:
        kernel, ramdisk = pygrub_first_boot_handler(vm, repo_url, other_config)
    else:
        raise UnsupportedInstallMethod

    if repo == 'cdrom':
        # SLES/RHEL: booting from CDROM this time but booting from 1st disk next time
        tweak_bootable_disk(vm)

    # Calculate the extra args need by kernel to locate installation repository
    if distro == DISTRO_RHLIKE:
        extra_args = rhel_first_boot_args(repo)
    elif distro == DISTRO_SLESLIKE:
        extra_args = sles_first_boot_args(repo)
    elif distro == DISTRO_DEBIANLIKE:
        extra_args = debian_first_boot_args(repo)
    elif distro == DISTRO_PYGRUB:
        extra_args = pygrub_first_boot_args(repo)
    else:
        raise UnsupportedInstallMethod

    # Tell eliloader to run 2nd boot phase next time this vm is started
    if rounds[distro] == 1:
        switchBootloader(vm)

    # Put it all together
    args += " " + extra_args
    if vnc:
        args += " vnc"
    if vncpasswd:
        args += " vncpassword=%s" % vncpasswd

    # Or set user defined options if available
    if other_config['install-args'] is not None:
        args += " " + other_config['install-args']

    if ramdisk is not None:
        print 'linux (kernel %s)(ramdisk %s)(args "%s")' % (kernel, ramdisk, args)
    else:
        print 'linux (kernel %s)(args "%s")' % (kernel, args)

def handle_second_boot(vm, img, args, other_config):
    distro = distros[other_config['install-distro']]

    prepend_args = [PYGRUB]


    if distro == DISTRO_SLESLIKE:
        # SLES 9/10 installers do not create /boot/grub/menu.lst when installing on top of XEN
        # SLES 11 does not have this problem.
        # If pygrub with no options fails then this must be one of the problematic versions, in
        # which case /we/ need to tell pygrub where to find the kernel and initrd.

        cmd = ["pygrub", "-q", "-n", img]
        (rc, out, err) = xcp.cmd.runCmd(cmd, True, True)
        if rc > 1:
            raise PygrubError(rc, err)

        if rc != 0:
            # need to emulate domUloader.  This is done by finding a kernel that
            # we can boot if possible, and then setting PV-bootloader-args.
            if other_config['install-arch'] == 'x86_64':
                kernel = 'vmlinuz-xen'
                initrd = 'initrd-xen'
            else:
                kernel = 'vmlinuz-xenpae'
                initrd = 'initrd-xenpae'

            xcp.logger.debug("SLES_LIKE: Pygrub failed, trying again..")
            for k, i in [ ("/%s" % kernel, "/%s" % initrd ), ("/boot/%s" % kernel , "/boot/%s" % initrd ) ]:
                xcp.logger.debug("SLES_LIKE: Trying %s and %s" % (k, i) )
                cmd = ["pygrub", "-n", "--kernel", k, "--ramdisk", i, img]
                (rc, out, err) = xcp.cmd.runCmd(cmd, True, True)
                if rc > 1:
                    raise PygrubError(rc, err)

                if rc == 0:
                    # found it - make the setting permanent:

                    xcp.logger.debug("SLES_LIKE: success.")

                    session = XenAPI.xapi_local()
                    session.login_with_password("", "", "", PROGRAM_NAME)
                    try:
                        prepend_args += ["--kernel", k, "--ramdisk", i]
                        vm_ref = session.xenapi.VM.get_by_uuid(vm)
                        if not never_latch:
                            session.xenapi.VM.set_PV_bootloader_args(vm_ref, "--kernel %s --ramdisk %s" % (k, i))
                    finally:
                        session.logout()
                    break

    elif distro == DISTRO_RHLIKE:
        # Oracle 5.x uek kernel doesn't boot, so we have to override
        # pygrub's default by setting PV-bootloader-args (with --entry N)

        cmd = ["pygrub", "-q", "-l", img]
        (rc, out, err) = xcp.cmd.runCmd(cmd, True, True)
        if rc != 0:
            raise PygrubError(rc, err)

        # Get the title 'title:' lines from pygrub
        p = re.compile('^title:')
        titles = [l for l in out.splitlines() if p.search(l)]

        found_ole_5x = False
        p = re.compile(r'Oracle.*el5uek', re.IGNORECASE)
        idx = 0

        ole5uek_lst = [bool(p.search(t)) for t in titles]
        found_ole_5x = True in ole5uek_lst

        # Get the (pygrub) indices for non el5uek kernel
        indices = []
        for (i, b) in itertools.izip(itertools.count(), ole5uek_lst):
            if b:   # el5uek kernel, so skip the index
                pass
            else:
                indices.append(i)

        if found_ole_5x:
            if not indices:
                raise Exception("Could not find non el5uek kernel")
            else:
                idx = indices[0]

            xcp.logger.debug("RHEL_LIKE: Pygrub found Oracle 5.x .el5euk kernel")

            session = XenAPI.xapi_local()
            session.login_with_password("", "", "", PROGRAM_NAME)
            try:
                prepend_args += ["--entry", str(idx)]
                vm_ref = session.xenapi.VM.get_by_uuid(vm)
                if not never_latch:
                    session.xenapi.VM.set_PV_bootloader_args(vm_ref, "--entry %s" % idx)
            finally:
                session.logout()
    else:
        raise UnsupportedInstallMethod

    pygrub_args = prepend_args + sys.argv[1:]
    xcp.logger.debug("pygrub cmd is:"+ str(pygrub_args))

    # now exec pygrub - hackily call update_rounds since we won't get to
    # run again.
    if not never_latch:
        switchBootloader(vm)
        update_rounds(vm, 2, 2)
    xcp.logger.debug("Launching pygrub for real..")
    os.execv(PYGRUB, prepend_args + sys.argv[1:])

def update_rounds(vm, current_round, rounds_required):
    session = XenAPI.xapi_local()
    session.xenapi.login_with_password("", "", "", PROGRAM_NAME)
    try:
        vm_ref = session.xenapi.VM.get_by_uuid(vm)

        # remove the install-round field: ignore errors as the key might
        # not be there and this is OK (default value is 1).
        session.xenapi.VM.remove_from_other_config(vm_ref, "install-round")

        # write a new value in for install-round if appropriate:
        if current_round != rounds_required:
            session.xenapi.VM.add_to_other_config(vm_ref, "install-round", str(current_round + 1))
        else:
            # All rounds complete. Remove install-distro key from other_config param.
            # If we don't do this and we later perform a "convert to template" on this VM,
            # the GUI will infer from the presence of this key that it must query the user
            # for the install media location.  This is unecessary since the template already
            # contains a fully installed disk image, that only needs to be copied.
            session.xenapi.VM.remove_from_other_config(vm_ref, "install-distro")

    finally:
        session.logout()

def find_host_size_limits():
    global pv_kernel_max_size, pv_initrd_max_size
    store = xs.xs()

    try:
        pv_kernel_max_size = int(store.read("", "/mh/limits/pv-kernel-max-size"), 10 )
    except (ValueError, TypeError, xs.Error):
        pass

    try:
        pv_initrd_max_size = int(store.read("", "/mh/limits/pv-ramdisk-max-size"), 10 )
    except (ValueError, TypeError, xs.Error):
        pass

    xcp.logger.debug("Host limits: kernel %d, ramdisk %d" %
                     (pv_kernel_max_size, pv_initrd_max_size))

def find_vm_size_limits(vm):
    global pv_kernel_max_size, pv_initrd_max_size
    store = xs.xs()

    try:
        domains = store.ls("", "/vm/" + vm + "/domains")
        domid = int(domains[0], 10)
    except (ValueError, TypeError, IndexError, xs.Error):
        raise APILevelException("Unable to find domid for " + vm)

    xs_path = "/local/domain/%s/platform/pv-%s-max-size"

    try:
        pv_kernel_max_size = int(store.read("", xs_path % (domid, "kernel")), 10)
    except (ValueError, TypeError, xs.Error):
        pass

    try:
        pv_initrd_max_size = int(store.read("", xs_path % (domid, "ramdisk")), 10)
    except (ValueError, TypeError, xs.Error):
        pass

    xcp.logger.debug("VM limits: kernel %d, ramdisk %d" %
                     (pv_kernel_max_size, pv_initrd_max_size))

def main():
    if True: #os.path.exists(DEBUG_SWITCH):
        xcp.logger.logToSyslog(level=logging.DEBUG)
    else:
        xcp.logger.logToSyslog()

    try:
        argv = sys.argv[1:]
        xcp.logger.debug(str(argv))
        for a in ['--default_args=', '--extra_args=', '--args=']:
            while a in argv:
                argv.remove(a)
        opts, mandargs = getopt.getopt(
            argv, "q", ["vm=", "logging", "quiet", "args=",
                        "extra_args=", "default_args="])
    except getopt.GetoptError:
        raise UsageError

    vm = None
    img = None
    args = ""
    for opt, val in opts:
        if opt == "--vm":
            vm = val
        if opt == "--logging":
            log_details = True
        if opt in ["--args", "--extra_args", "--default_args"]:
            args += val + " "

    if len(mandargs) < 1:
        raise UsageError

    img = mandargs[0]

    # support running this bootloader multiple times.  We switch bootloader
    # if all required rounds are completed
    other_config = canonicaliseOtherConfig(vm)
    current_round = int(other_config['install-round'])

    find_host_size_limits()
    try:
        find_vm_size_limits(vm)
    except:
        xcp.logger.debug("Ignorning exception while finding vm size limits")

    # how many rounds are required?
    try:
        distro = distros[other_config['install-distro']]
        rounds_required = rounds[distro]
    except:
        raise UnsupportedInstallMethod, "Distribution '%s' is not supported." % other_config['install-distro']

    # Make urllib2 use proxy server if one is supplied
    proxy = other_config['install-proxy']
    if proxy:
        proxy_support = urllib2.ProxyHandler({"http" : proxy})
        opener = urllib2.build_opener(proxy_support)
        urllib2.install_opener(opener)

    if current_round == 1:
        handle_first_boot(vm, img, args, other_config)
    elif current_round == 2:
        handle_second_boot(vm, img, args, other_config)

    update_rounds(vm, current_round, rounds_required)

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except APILevelException, e:
        log_exception("APIERROR: ", traceback.format_exc())
        raise RuntimeError, e.apifmt()
    except ResourceAccessError, e:
        raise RuntimeError("Could not access %s" % e.source)
    except PygrubError, x:
        log_exception("PYERROR: ", traceback.format_exc())
        raise RuntimeError, str(x)
    except UsageError, e:
        msg = "Invalid usage. Usage: eliloader --vm <vm> <image>"
        print >> sys.stderr, msg
        raise RuntimeError, "Invalid command line arguments."
    except StandardError, e:
        log_exception("ERROR: ", traceback.format_exc())
        raise
