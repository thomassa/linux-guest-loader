PACKAGE_NAME := linux-guest-loader

PACKAGE_VERSION := 1
PACKAGE_SUBVERSION := 9
PACKAGE_EXTRAVERSION := 0
PACKAGE_RELEASE := 1

SHELL = /bin/sh
RPM_BASE := $(CURDIR)/build
RPM_SPECSDIR := $(RPM_BASE)/SPECS
RPM_SOURCESDIR := $(RPM_BASE)/SOURCES
RPM_SRPMDIR := $(RPM_BASE)/SRPMS
RPM_BRPMDIR := $(RPM_BASE)/RPMS

LGL_SOURCES := $(wildcard *.py)
LGL_VERSION := $(PACKAGE_VERSION).$(PACKAGE_SUBVERSION).$(PACKAGE_EXTRAVERSION)
LGL_RELEASE := $(PACKAGE_RELEASE)

PACKAGE_OUTPUT := $(PACKAGE_NAME)-$(LGL_VERSION)-$(LGL_RELEASE).noarch.rpm

LGL_SPEC := $(RPM_SPECSDIR)/$(PACKAGE_NAME).spec
LGL_SRC_DIR := $(PACKAGE_NAME)-$(LGL_VERSION)
LGL_SRC := $(RPM_SOURCESDIR)/$(PACKAGE_NAME)-$(LGL_VERSION).tar.gz
LGL_SRPM := $(PACKAGE_NAME)-$(LGL_VERSION)-$(LGL_RELEASE).src.rpm

.PHONY: build
build: $(LGL_SRPM)
	rpmbuild --rebuild --define "_topdir $(RPM_BASE)" \
	$(RPM_SRPMDIR)/$(LGL_SRPM)

$(LGL_SRPM): $(RPM_BASE) $(LGL_SPEC) $(LGL_SRC)
	rpmbuild -bs  --define "_topdir $(RPM_BASE)" $(LGL_SPEC)

$(RPM_BASE): 
	-mkdir -p $@
	-mkdir -p $@/SPECS
	-mkdir -p $@/SOURCES
	-mkdir -p $@/SRPMS
	-mkdir -p $@/RPMS
	-mkdir -p $@/BUILD

.SECONDARY: $(LGL_SRC)
$(LGL_SRC): $(LGL_SOURCES)
	mkdir -p $(LGL_SRC_DIR)
	cp -f $^ $(LGL_SRC_DIR)
	tar zcf $@ $(LGL_SRC_DIR)
	rm -rf $(LGL_SRC_DIR)

.SECONDARY: $(RPM_SPECSDIR)/%.spec
$(RPM_SPECSDIR)/%.spec: mk/$(PACKAGE_NAME).spec.in
	sed -e 's/@LGL_VERSION@/$(LGL_VERSION)/g' \
	-e 's/@LGL_RELEASE@/$(LGL_RELEASE)/g' \
	< $< \
	> $@

.PHONY: clean
clean:
	rm -f $(LGL_SRC) $(LGL_SPEC)
	rm -rf $(RPM_BASE)
