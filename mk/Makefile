USE_BRANDING := yes
IMPORT_BRANDING := yes
REPONAME :=linux-guest-loader
include $(B_BASE)/common.mk
include $(B_BASE)/rpmbuild.mk

-include $(MY_OBJ_DIR)/version.inc
.PHONY: $(MY_OBJ_DIR)/version.inc
$(MY_OBJ_DIR)/version.inc:
	$(version-makefile) > $@
	$(call hg_cset_number,$(REPONAME)) >> $@
	echo LGL_VERSION := \$$\(PLATFORM_VERSION\) >> $@
	echo LGL_RELEASE := xs\$$\(CSET_NUMBER\) >> $@

LGL_SOURCES := $(wildcard ../*.py)
DATA_FILES := $(wildcard $(PROJECT_OUTPUTDIR)/rhel*guest-installer/*initrd-additions*) \
	$(wildcard $(PROJECT_OUTPUTDIR)/ubuntu*guest-installer/*initrd-additions*)

PACKAGE_OUTPUT := $(MY_MAIN_PACKAGES)/linux-guest-loader-data-$(LGL_VERSION)-$(LGL_RELEASE).noarch.rpm

LGL_SPEC := linux-guest-loader.spec
LGL_SRC_DIR := linux-guest-loader-$(LGL_VERSION)
LGL_SRC := $(RPM_SOURCESDIR)/linux-guest-loader-$(LGL_VERSION).tar.gz
DATA_TARBALL := $(RPM_SOURCESDIR)/data.tar.gz
LGL_SRPM := linux-guest-loader-$(LGL_VERSION)-$(LGL_RELEASE).src.rpm
LGL_STAMP := $(MY_OBJ_DIR)/.rpmbuild.lgl.stamp

.PHONY: build
build: $(LGL_STAMP) $(PACKAGE_OUTPUT) $(MY_OUTPUT_DIR)/linux-guest-loader.inc $(MY_SOURCES)/MANIFEST

$(MY_SOURCES)/MANIFEST: $(MY_SOURCES_DIRSTAMP) $(RPM_BUILD_COOKIE)
	( echo "$(COMPONENT) gpl file $(RPM_SRPMSDIR)/$(LGL_SRPM)" ; \
	) >$@

$(MY_OUTPUT_DIR)/linux-guest-loader.inc: $(MY_OUTPUT_DIR)/.dirstamp
	( echo LGL_PKG_NAME := linux-guest-loader ;\
	  echo LGL_PKG_VERSION := $(LGL_VERSION)-$(LGL_RELEASE) ;\
	  echo LGL_PKG_FILE := RPMS/noarch/linux-guest-loader-$(LGL_VERSION)-$(LGL_RELEASE).noarch.rpm ;\
	) >$@

.PHONY: pylint
pylint:
	run-pylint.sh $(LGL_SOURCES)

.PHONY: sources
sources: $(MY_SOURCES)/MANIFEST

.PHONY: clean
clean:
	rm -f $(LGL_STAMP) $(LGL_SRC) $(RPM_SPECSDIR)/$(LGL_SPEC)

.SECONDARY: $(LGL_SRC)
$(LGL_SRC): $(LGL_SOURCES)
	$(call mkdir_clean,$(MY_OBJ_DIR)/$(LGL_SRC_DIR))
	mkdir -p $(MY_OBJ_DIR)/$(LGL_SRC_DIR)
	cp -f $^ $(MY_OBJ_DIR)/$(LGL_SRC_DIR)
	tar zcf $@ -C $(MY_OBJ_DIR) $(LGL_SRC_DIR)
	rm -rf $(MY_OBJ_DIR)/$(LGL_SRC_DIR)

.SECONDARY: $(RPM_SPECSDIR)/%.spec
$(RPM_SPECSDIR)/%.spec: *.spec.in
	sed -e 's/@LGL_VERSION@/$(LGL_VERSION)/g' \
	  -e 's/@LGL_RELEASE@/$(LGL_RELEASE)/g' \
	  < $< \
	  > $@

.SECONDARY: $(DATA_TARBALL)
$(DATA_TARBALL): $(DATA_FILES)
	tar zcvf $@ -C $(PROJECT_OUTPUTDIR) `echo $^ | sed -e 's!$(PROJECT_OUTPUTDIR)/!!g'`

$(RPM_SRPMSDIR)/$(LGL_SRPM): $(RPM_DIRECTORIES) $(RPM_SPECSDIR)/$(LGL_SPEC) $(LGL_SRC) $(DATA_TARBALL)
	$(RPMBUILD) -bs --with data $(RPM_SPECSDIR)/$(LGL_SPEC)

$(LGL_STAMP): $(RPM_SRPMSDIR)/$(LGL_SRPM)
	# work around rpmbuild removing source and spec
	ln -f $(RPM_SPECSDIR)/$(LGL_SPEC) $(RPM_SPECSDIR)/$(LGL_SPEC).keep
	ln -f $(LGL_SRC) $(LGL_SRC).keep
	$(RPMBUILD) --rebuild --with data $(RPM_SRPMSDIR)/$(LGL_SRPM)
	mv -f $(RPM_SPECSDIR)/$(LGL_SPEC).keep $(RPM_SPECSDIR)/$(LGL_SPEC)
	mv -f $(LGL_SRC).keep $(LGL_SRC)
	touch $@

$(PACKAGE_OUTPUT): $(MY_MAIN_PACKAGES)/.dirstamp
	cp -fp $(RPM_RPMSDIR)/noarch/linux-guest-loader-data-$(LGL_VERSION)-$(LGL_RELEASE).noarch.rpm $@
