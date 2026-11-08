include $(TOPDIR)/rules.mk

PKG_NAME:=airoha-qos
PKG_RELEASE:=6
PKG_LICENSE:=GPL-2.0-only
include $(INCLUDE_DIR)/package.mk

define Package/airoha-qos
  SECTION:=net
  CATEGORY:=Network
  TITLE:=AN7581 coordinated boot QoS profile
  DEPENDS:=@TARGET_airoha_an7581
endef

define Package/airoha-qos/conffiles
/etc/config/airoha_qos
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./src/* $(PKG_BUILD_DIR)/
endef

define Build/Compile
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_CPPFLAGS) -Wall -Wextra \
		-o $(PKG_BUILD_DIR)/airoha-qosctl $(PKG_BUILD_DIR)/airoha-qosctl.c $(TARGET_LDFLAGS)
endef

define Package/airoha-qos/install
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/etc/config $(1)/etc/init.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/airoha-qosctl $(1)/usr/sbin/
	$(INSTALL_BIN) ./files/airoha-qos.init $(1)/etc/init.d/airoha-qos
	$(INSTALL_CONF) ./files/airoha_qos $(1)/etc/config/airoha_qos
endef

$(eval $(call BuildPackage,airoha-qos))
