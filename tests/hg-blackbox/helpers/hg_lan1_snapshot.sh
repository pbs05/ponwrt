#!/bin/sh
# Read-only HG Ethernet PCS snapshot. No MDIO, mux writes or recovery actions.
set -eu
[ "$(tr -d '\000' < /proc/device-tree/model)" = 'Nokia XG-040G-MD (UBI)' ] || exit 64
[ "$(tr -d '\000' < /proc/device-tree/soc/pcs@1fa09000/compatible)" = 'airoha,an7581-pcs-eth' ] || exit 64
command -v devmem >/dev/null || exit 69
read32() {
    case "$1" in
        0x1fa70b04|0x1fa7443c|0x1fa7b530|0x1fa09000) ;;
        *) return 65 ;;
    esac
    HG_L1_VAL=$(devmem "$1" 32)
    case "$HG_L1_VAL" in 0x????????) ;; *) return 66 ;; esac
    case "${HG_L1_VAL#0x}" in *[!0-9a-fA-F]*) return 66 ;; esac
    printf '%s\n' "$HG_L1_VAL"
}
printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
printf 'kernel=%s\n' "$(uname -r)"
printf 'uptime=%s\n' "$(cut -d ' ' -f 1 /proc/uptime)"
for HG_L1_KEY in carrier speed carrier_changes; do
    if [ "$HG_L1_KEY" = speed ]; then
        HG_L1_VALUE=$(cat /sys/class/net/lan1/speed 2>/dev/null) || HG_L1_VALUE=-1
    else
        HG_L1_VALUE=$(cat "/sys/class/net/lan1/$HG_L1_KEY")
    fi
    printf '%s=%s\n' "$HG_L1_KEY" "$HG_L1_VALUE"
done
for HG_L1_KEY in rx_packets tx_packets rx_bytes tx_bytes rx_errors tx_errors rx_dropped tx_dropped rx_crc_errors; do
    printf '%s=%s\n' "$HG_L1_KEY" "$(cat "/sys/class/net/lan1/statistics/$HG_L1_KEY")"
done
HG_L1_SYNC=$(read32 0x1fa70b04)
HG_L1_PORT=$(read32 0x1fa7443c)
HG_L1_FREQ=$(read32 0x1fa7b530)
HG_L1_MAC=$(read32 0x1fa09000)
printf 'pcs_rx_sync=%s\ninternal_port_link=%s\nfreqdet_lock=%s\nmac_stop_bits=%s\n' \
    "$(((HG_L1_SYNC >> 5) & 1))" "$(((HG_L1_PORT >> 3) & 1))" \
    "$((HG_L1_FREQ & 1))" "$((HG_L1_MAC & 15))"
HG_L1_LED=/sys/class/leds/mt7530-0:0f:green:lan-1
for HG_L1_KEY in trigger device_name interval offloaded link rx tx; do
    if [ -r "$HG_L1_LED/$HG_L1_KEY" ]; then
        printf 'led_%s=%s\n' "$HG_L1_KEY" "$(cat "$HG_L1_LED/$HG_L1_KEY")"
    fi
done
printf '__KERNEL_LOG__\n'
dmesg | sed -n '/lan1: Link /p; /PCS .*HSGMII/p; /PCS .*configuration failed/p'
