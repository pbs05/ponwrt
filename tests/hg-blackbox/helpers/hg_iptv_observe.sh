#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Temporary test session only. Never writes a network setting or debugfs.
set -eu
dir=$1 token=$2 action=$3
shift 3
case "$dir" in /tmp/hiptv_*) ;; *) exit 2;; esac
case "${dir#/tmp/hiptv_}" in ''|*[!a-f0-9]*) exit 2;; esac
case "$token" in ''|*[!a-zA-Z0-9_-]*) exit 2;; esac
[ "$(cat "$dir/owner")" = "$token" ] || exit 1
clock() { read -r up rest < /proc/uptime; printf '%s\n' "${up%%.*}"; }
pulse() {
	[ ! -e "$dir/stopped" ] || exit 1
	clock > "$dir/lease.new"
	mv "$dir/lease.new" "$dir/lease"
}
stop() {
	touch "$dir/stopped"
	# Match our unique executable, never a name shared with a real service.
	for p in /proc/[0-9]*/exe; do
		[ "$(readlink "$p" 2>/dev/null || true)" = "$dir/probe" ] || continue
		pid=${p#/proc/}; pid=${pid%/exe}
		kill -TERM "$pid" 2>/dev/null || true
	done
}
release_later() {
	# Called only by the independent watchdog; the controller normally
	# verifies all peers and releases the lock immediately after cleanup.
	[ -e "$dir/coordinator" ] || return 0
	sleep 310
	lock=/tmp/hg-qos-regression.lock
	if [ "$(cat "$lock/owner" 2>/dev/null || true)" = "$token" ]; then
		rm "$lock/owner"; rmdir "$lock"
	fi
}
case "$action" in
pulse) pulse;;
stop) stop;;
check)
	for p in /proc/[0-9]*/exe; do
		[ "$(readlink "$p" 2>/dev/null || true)" != "$dir/probe" ] || exit 1
	done
	;;
run)
	limit=$1; shift
	case "$limit" in ''|*[!0-9]*) exit 2;; esac
	[ "$limit" -ge 1 ] && [ "$limit" -le 300 ] || exit 2
	pulse
	# A remote lifetime bound also applies if SSH/controller disappears.
	exec timeout -k 2 "$limit" "$dir/probe" "$@"
	;;
watchdog)
	echo ready > "$dir/watchdog.ready"
	while [ -d "$dir" ] && [ ! -e "$dir/stopped" ]; do
		lease=$(cat "$dir/lease" 2>/dev/null || echo 0)
		if [ "$(($(clock) - lease))" -ge 60 ]; then
			stop
			# Every remote helper has a <=300s hard bound. Keep the shared
			# lock until peers have certainly stopped, even on partition.
			release_later
			exit 0
		fi
		sleep 5
	done
	release_later
	;;
*) exit 2;;
esac
