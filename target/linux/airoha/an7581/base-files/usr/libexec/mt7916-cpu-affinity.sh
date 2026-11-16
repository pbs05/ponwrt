#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# Pin MT7916 TX and NAPI RX worker threads across the 4 A53 cores on
# AN7581 FiberHome HG5585F boards to prevent single-core softirq saturation.

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
			# Isolate TX thread on CPU3 away from NAPI RX workers.
			taskset -p 8 "$pid" >/dev/null 2>&1
			;;
		napi/phy0-0)
			# Spread the 6 phy0 NAPI workers across CPU0, CPU1, and CPU2 (0,0,0,1,2,2).
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
