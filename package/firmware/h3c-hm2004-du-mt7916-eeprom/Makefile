include $(TOPDIR)/rules.mk

PKG_NAME:=h3c-hm2004-du-mt7916-eeprom
PKG_RELEASE:=1

include $(INCLUDE_DIR)/package.mk

define Package/h3c-hm2004-du-mt7916-eeprom
  SECTION:=firmware
  CATEGORY:=Firmware
  TITLE:=H3C HM2004-DU MT7916 EEPROM template
endef

define Package/h3c-hm2004-du-mt7916-eeprom/description
  MT7916 EEPROM for H3C HM2004-DU, extracted from the stock reservearea
  partition (mtd14) at offset 0x4c000, 4096 bytes. Unlike a bare MediaTek
  template it already carries board configuration and partial calibration
  fields; mt7915e still merges per-device iCal from the chip eFuse.

  The board DTS reads the same blob from the factory UBI volume through the
  wifi_eeprom nvmem cell; this package keeps Wi-Fi working when the factory
  volume has not been populated yet.
endef

define Build/Compile
endef

define Package/h3c-hm2004-du-mt7916-eeprom/install
	$(INSTALL_DIR) $(1)/lib/firmware/mediatek
	$(INSTALL_DATA) ./files/mt7916_eeprom.bin \
		$(1)/lib/firmware/mediatek/mt7916_eeprom.bin
endef

$(eval $(call BuildPackage,h3c-hm2004-du-mt7916-eeprom))
