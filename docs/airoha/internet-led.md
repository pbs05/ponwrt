# Installed-default-route Internet LED

The XG-040G-MD Internet LED (`green:wan-online`, GPIO20) is on when at least
one installed IPv4 or IPv6 main-table default route has a device with carrier:

```uci
config led
	option name 'Internet'
	option sysfs 'green:wan-online'
	option trigger 'default-route'
```

Select **Default route connection** in **System > LED Configuration**.
No logical interface name, physical or PPP-generated device name, VLAN ID,
ISP or protocol is encoded in the implementation. Reconnecting or renaming
an interface, changing its protocol, or switching the default uplink does
not require editing the LED configuration when managed by netifd.
IPv6-only default routes count.
The LED indicates a connected default route, not end-to-end reachability,
a recent WireGuard handshake, or a check of policy-specific routing tables.

Use the kernel's installed routes instead of simply selecting the first
interface returned by `network_find_wan()`: the latter can select a service
interface whose DHCP default route was not installed. This distinction was
observed on HG's existing IPTV interface during diagnosis.

The existing OpenWrt LED init application-trigger hook applies the current
state at boot and on LED configuration reload. Interface hotplug reapplies
only LEDs using this trigger on `ifup`, `ifdown` and `ifupdate`, reading current
routes instead of trusting a potentially delayed event. It adds no daemon,
timer, packet hook, network restart or optional package dependency.
The global `leds_off` choice is respected. Without a connected default-route
device the LED is off. The boot migration preserves any existing configuration
for this LED; it does not overwrite explicit user choices.

The PON driver's `pon0-online` trigger describes PON service authentication,
which is independent of an established PPP session on this installation.
Do not set `service_ready` just to illuminate the Internet LED. Registration,
LOS and hardware LAN LED triggers are not changed by this fix.

## Deployment

Applied to the running HG on 2026-10-19 without restarting the network or
reconnecting PPP. The Internet LED's brightness reads back as `1`; the PPP
uptime continued increasing and the other LED triggers and LAN hardware
offload settings were retained. A physical LED observation, forced disconnect,
interface rename and protocol-change exercise were not performed.

## Submission validation

The trigger is available on the AN7581 target; only MD UBI receives the new
default migration. Existing choices, including none, are retained. This is an
application trigger handled by the existing LED init hook, not a new kernel
trigger or network service. It observes main-table route/carrier state and
netifd events, not arbitrary out-of-band route changes or policy tables.

Direct source export on master `03c1345447`; whitespace reviewed. No new tests,
full build, flash or physical LED verification for this submission. Earlier
brightness readback is not a complete protocol/rename/disconnect matrix.
