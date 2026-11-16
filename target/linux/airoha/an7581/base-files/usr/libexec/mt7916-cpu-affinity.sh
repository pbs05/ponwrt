#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# Separate MT7916 TX and NAPI work across the four A53 cores on HG5585F.

. /lib/functions.sh

case "$(board_name)" in
fiberhome,hg5585f-ct|\
fiberhome,hg5585f-ct-usb-sfp|\
fiberhome,hg5585f-cu|\
fiberhome,hg5585f-cu-usb-sfp)
	;;
*)
	exit 0
	;;
esac

command -v taskset >/dev/null 2>&1 || exit 0

idx=0
for pid in $(ls /proc | grep '^[0-9]*$' | sort -n); do
	[ -r "/proc/$pid/comm" ] || continue
	read -r cname < "/proc/$pid/comm" 2>/dev/null || continue
	case "$cname" in
		mt76-tx*)
			taskset -p 8 "$pid" >/dev/null 2>&1
			;;
		napi/phy0-0)
			case $((idx % 6)) in
				0|1|2) mask=1 ;;
				3)     mask=2 ;;
				4|5)   mask=4 ;;
			esac
			taskset -p "$mask" "$pid" >/dev/null 2>&1
			idx=$((idx + 1))
			;;
	esac
done

exit 0
