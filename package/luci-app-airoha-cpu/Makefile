include $(TOPDIR)/rules.mk

PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only
LUCI_TITLE:=LuCI Airoha ingress CPU scheduling
LUCI_DEPENDS:=+luci-base +airoha-cpu +jsonfilter @TARGET_airoha_an7581

include $(TOPDIR)/feeds/luci/luci.mk
# call BuildPackage: LuCI package scanner marker.
