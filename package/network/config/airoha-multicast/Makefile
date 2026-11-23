include $(TOPDIR)/rules.mk

PKG_NAME:=airoha-multicast
PKG_RELEASE:=3
PKG_LICENSE:=GPL-2.0-only
include $(INCLUDE_DIR)/package.mk

define Package/airoha-multicast
  SECTION:=net
  CATEGORY:=Network
  TITLE:=AN7581 routed multicast offload policy
  DEPENDS:=@TARGET_airoha_an7581 +flock +jsonfilter
endef

define Package/airoha-multicast/conffiles
/etc/config/airoha_multicast
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./src/* $(PKG_BUILD_DIR)/
endef

define Build/Compile
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_CPPFLAGS) -Wall -Wextra \
		-o $(PKG_BUILD_DIR)/airoha-mcastctl $(PKG_BUILD_DIR)/airoha-mcastctl.c $(TARGET_LDFLAGS)
endef

define Package/airoha-multicast/install
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/usr/libexec $(1)/etc/config $(1)/etc/init.d \
		$(1)/etc/hotplug.d/iface $(1)/etc/uci-defaults
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/airoha-mcastctl $(1)/usr/sbin/
	$(INSTALL_BIN) ./files/airoha-multicast.init $(1)/etc/init.d/airoha-multicast
	$(INSTALL_BIN) ./files/airoha-multicast-boot $(1)/usr/libexec/
	$(INSTALL_BIN) ./files/airoha-multicast-policy $(1)/usr/libexec/
	$(INSTALL_BIN) ./files/95-airoha-multicast $(1)/etc/hotplug.d/iface/
	$(INSTALL_BIN) ./files/airoha-multicast-firewall $(1)/usr/libexec/
	$(INSTALL_BIN) ./files/95-airoha-multicast-firewall $(1)/etc/uci-defaults/
	$(INSTALL_CONF) ./files/airoha_multicast $(1)/etc/config/airoha_multicast
endef

$(eval $(call BuildPackage,airoha-multicast))
