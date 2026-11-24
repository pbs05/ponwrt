#!/bin/sh
# Called by netifd's packet_steering service, including interface.* events.
# A disabled policy delegates to the original implementation with its options.
/usr/sbin/airoha-cpuctl sync >/dev/null
rc=$?
[ "$rc" = 0 ] && exit 0
[ "$rc" = 10 ] || exit "$rc"
flows="$(uci -q get 'network.@globals[0].steering_flows')"
case "$flows" in ''|*[!0-9]*) flows=0;; esac
if [ "$flows" -gt 0 ]; then
	exec /usr/libexec/network/packet-steering.uc -l "$flows" "$1"
fi
exec /usr/libexec/network/packet-steering.uc "$1"
