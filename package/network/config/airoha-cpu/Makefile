include $(TOPDIR)/rules.mk

PKG_NAME:=airoha-cpu
PKG_RELEASE:=2
PKG_LICENSE:=GPL-2.0-only
include $(INCLUDE_DIR)/package.mk

define Package/airoha-cpu
  SECTION:=net
  CATEGORY:=Network
  TITLE:=Reversible ingress CPU policy for XG-040G-MD
  DEPENDS:=@TARGET_airoha_an7581 +ucode +ucode-mod-fs +ucode-mod-uci +@BUSYBOX_CONFIG_TASKSET +@BUSYBOX_CONFIG_FEATURE_TASKSET_FANCY +@BUSYBOX_CONFIG_FEATURE_TASKSET_CPULIST
endef

define Package/airoha-cpu/conffiles
/etc/config/airoha_cpu
endef

define Build/Compile
endef

define Package/airoha-cpu/install
	$(INSTALL_DIR) $(1)/etc/config $(1)/etc/init.d $(1)/usr/sbin \
		$(1)/usr/libexec/airoha-cpu $(1)/usr/libexec/platform
	$(INSTALL_CONF) ./files/airoha_cpu $(1)/etc/config/
	$(INSTALL_BIN) ./files/airoha-cpu.init $(1)/etc/init.d/airoha-cpu
	$(INSTALL_BIN) ./files/airoha-cpuctl $(1)/usr/sbin/
	$(INSTALL_DATA) ./files/policy.uc $(1)/usr/libexec/airoha-cpu/
	$(INSTALL_BIN) ./files/guard $(1)/usr/libexec/airoha-cpu/
	$(INSTALL_BIN) ./files/packet-steering.sh $(1)/usr/libexec/platform/
endef

$(eval $(call BuildPackage,airoha-cpu))
