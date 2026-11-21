include $(TOPDIR)/rules.mk

PKG_VERSION:=1.0.0
PKG_RELEASE:=4
PKG_LICENSE:=GPL-2.0-only

LUCI_TITLE:=LuCI Airoha hardware QoS policy
LUCI_DEPENDS:=+luci-base +airoha-qos @TARGET_airoha_an7581

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage: package scanner marker for the LuCI build wrapper.
