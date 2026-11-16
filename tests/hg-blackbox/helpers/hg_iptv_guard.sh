#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runtime-only transaction and watchdog. Never edits UCI or reboots HG.
set -eu
dir=$1
token=$2
action=$3
shift 3
lock=/tmp/hg-qos-regression.lock
control=/sys/kernel/debug/ppe/multicast_enable
held=0
finish() {
	rc=$?
	if [ "$held" = 1 ]; then rmdir "$dir/operation" 2>/dev/null || true; fi
	if [ "$action" = restore ] && [ "$rc" -ne 0 ]; then
		echo "restore failed ($rc); inspect saved state" > "$dir/restore.error"
	fi
}
trap finish EXIT
case "$dir" in /tmp/hiptv_*) ;; *) exit 2;; esac
case "${dir#/tmp/hiptv_}" in ''|*[!a-f0-9]*) exit 2;; esac
case "$token" in ''|*[!a-zA-Z0-9_-]*) exit 2;; esac

uptime_seconds() {
	read -r up ignored < /proc/uptime
	printf '%s\n' "${up%%.*}"
}
owned() {
	[ -f "$lock/owner" ] && [ "$(cat "$lock/owner")" = "$token" ]
}
operation() {
	n=0
	until mkdir "$dir/operation" 2>/dev/null; do
		n=$((n + 1)); [ "$n" -lt 15 ] || return 1
		sleep 1
	done
	held=1
	trap 'exit 130' INT TERM HUP
}
touch_lease() {
	if ! owned || [ -f "$dir/restored" ] || [ -f "$dir/restore.error" ]; then return 1; fi
	uptime_seconds > "$dir/lease.new"
	mv "$dir/lease.new" "$dir/lease"
}
restore() {
	[ ! -f "$dir/restored" ] || return 0
	operation
	[ ! -f "$dir/restored" ] || return 0
	owned || { echo 'test lock owner changed' > "$dir/restore.error"; return 1; }
	read -r pause snoop querier gro bridge ingress < "$dir/original"
	# Inputs were validated by the controller, but refuse corrupt recovery data.
	case "$pause$snoop$querier" in [01][01][01]) ;; *) return 1;; esac
	case "$gro" in on|off) ;; *) return 1;; esac
	echo 0 > "$control"
	if [ -f "$dir/gro.changed" ]; then ethtool -K "$ingress" rx-gro-list "$gro"; fi
	if [ -f "$dir/bridge.changed" ]; then
		ip link set dev "$bridge" type bridge mcast_snooping "$snoop" mcast_querier "$querier"
	fi
	[ "$(cat "/sys/class/net/$bridge/bridge/multicast_snooping")" = "$snoop" ]
	[ "$(cat "/sys/class/net/$bridge/bridge/multicast_querier")" = "$querier" ]
	[ "$(ethtool -k "$ingress" | sed -n 's/^rx-gro-list: \(on\|off\).*/\1/p')" = "$gro" ]
	if [ "$pause" = 0 ]; then
		airoha-mcastctl get > "$dir/restore.policy"
		[ "$(jsonfilter -i "$dir/restore.policy" -e '@.broken')" = 0 ]
		[ "$(jsonfilter -i "$dir/restore.policy" -e '@.ppe_stopped')" = 0 ]
		echo 1 > "$control"
	fi
	airoha-mcastctl get > "$dir/restored.policy"
	[ "$(jsonfilter -i "$dir/restored.policy" -e '@.paused')" = "$pause" ]
	printf 'runtime restored\n' > "$dir/restored"
	owned && rm "$lock/owner" && rmdir "$lock"
}

case "$action" in
restore)
	# A failing restoration retains its lock, state and script for manual recovery.
	# Run as a separate shell so set -e remains effective inside restore().
	restore
	;;
watchdog)
	printf 'watchdog ready\n' > "$dir/watchdog.ready"
	while [ -d "$dir" ] && [ ! -f "$dir/restored" ]; do
		[ ! -f "$dir/restore.error" ] || exit 1
		lease=$(cat "$dir/lease" 2>/dev/null || echo 0)
		now=$(uptime_seconds)
		if [ "$((now - lease))" -ge 60 ]; then
			/bin/sh "$dir/guard.sh" "$dir" "$token" restore >> "$dir/watchdog.log" 2>&1
			exit $?
		fi
		sleep 5
	done
	;;
setup|mode|pulse)
	operation
	touch_lease
	read -r pause snoop querier gro bridge ingress < "$dir/original"
	case "$action" in
	setup)
		echo 0 > "$control"
		touch "$dir/bridge.changed"
		ip link set dev "$bridge" type bridge mcast_snooping 1 mcast_querier 1
		;;
	mode)
		case "$1" in 0|1) ;; *) exit 2;; esac
		case "$2" in
		keep) ;;
		on|off) touch "$dir/gro.changed"; ethtool -K "$ingress" rx-gro-list "$2";;
		*) exit 2;;
		esac
		echo "$1" > "$control"
		;;
	pulse)
		airoha-mcastctl get
		cat /sys/kernel/debug/ppe/multicast
		;;
	esac
	;;
*) exit 2;;
esac
