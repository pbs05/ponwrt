# Automatic AN7581 PON multicast admission

## Policy and activation

Built with `CONFIG_NET_AIROHA_MCAST`, this kernel-owned IFC gate operates
independently of the runtime PPE multicast-offload switch. No UCI switch,
periodic manager, player or userspace classifier is introduced. Enabling this
kernel feature therefore also opts into automatic filtering on supported
AN7581 PON topology. Multicast routing/proxy remains necessary when the network
uses routed IPTV; the gate does not implement IGMP proxying.

Local memberships on the actual PON device or a direct single 802.1Q VLAN,
and positive explicit multicast routes, authorize full IPv4 groups in their
actual VLAN scope. Unresolved/negative data-created routes cannot authorize a
group. Other PON IPv4/IPoE UDP multicast is discarded before CPU receive work.
IGMP, 224.0.0/24, unicast, IPv6, PPPoE and QinQ keep ordinary processing.
This is group admission, not a source ACL or firewall replacement: the kernel
still enforces source filters and downstream forwarding authorization.

No logical name, operator VID, channel, source address or port is hardcoded.
VLAN interface replacement rebuilds the exception set from live state. One
VLAN's join does not authorize the same group on another VLAN. This cannot
make an operator transmit IPTV on an arbitrary VID.

## Events, capacity and failure

On membership/interface/route events, disable the gate immediately and schedule
a snapshot on the unbound workqueue. A new join notifies before its first IGMP
report. Rebuild with filtering disabled and enable only if the generation is
still current. During updates the filter deliberately permits background
traffic instead of applying an obsolete deny policy. There is no added
per-packet software classifier or periodic scan.

There are 64 TCAM and 32 LUT1 entries. Four lookups are reserved; the remaining
28 are shared by distinct groups and VLANs with exceptions. A single tagged
IPTV scope can therefore carry 27 distinct groups. A group used by several
VLANs shares its lookup. Exhaustion disables the hardware gate, with a later
event able to recover it. This is fail-open, not a resource-overflow denial.

Unsupported upper devices/bridging/source-discovery scopes bypass their scope;
bridged physical PON, unobserved namespace or unsupported 802.1ad topology can
bypass the entire classifier. Priority-tagged VID 0 bypasses filtering.

The driver takes ownership only when IFC is disabled and actions are invalid.
Save/verify/restore the old TCAM/LUT/global state; never overwrite another owner.
A hardware transaction failure disables the gate and stops retrying for this
driver lifetime. Failed ownership/programming is reported, not a stale whitelist.

## Drop-only coexistence with PPE

Allowed packets must miss IFC entirely. The earlier terminal no-op ALLOW design
interfered with cold PPE learning: permitted video arrived but BIND did not form,
and learned keys lost ingress VID metadata. The retained correction emits only
DROP actions, with admitted group/VLAN lookup hits used as negative match
conditions. It adds no force-CPU fallback or global PPE hash-mode change.

Keep the firmware's `PPE_GLO_CFG_FLOW_DROP_UPDATE` default. Dropped background
flows may still have UNB records in PPE; that is not proof of CPU delivery or
an unexpected BIND. Do not promise filtering in the PON MAC or optical line,
or infer the exact IFC/PPE physical stage order from CPU observations.

## Status and costs

Read-only root debugfs `pon_multicast_filter` shares the driver's diagnostic
directory supplied by the IRQ/NAPI proposal. It reports state, errors, rule
and lookup use, generations and the last successfully activated VLAN/group
snapshot. Old admissions are historical unless active and generations agree.
The bounded software snapshot is about 800 bytes; ordinary status does not
scan TCAM/LUT/PPE. A requested MIB31 read is bounded hardware work. No control
file or background polling service is added.

The separately proposed LuCI NPU update can consume this optional interface;
it is not required for enforcement. No measured percentage CPU or power saving
is claimed, and unrelated polling/interrupt load may remain.

## Source and evidence

Register/layout facts came from the SDK IFC headers at
`32b5aa356c2406edef8806aaf75451ff0a5286f2` and the owner's unstripped original
module (build ID `40f62209a0eed4e7c40aeea2afd4f6c7bde24e4d`). The driver is an
independent implementation; no vendor module or disassembly is shipped.
Native TCAM flag/VID matching and full-address LUT1 matching replace selectors
that bounded trials showed were unsupported in LUT1.

Earlier paired experiments demonstrated background suppression while valid
local/router-side RTP continued without missing/duplicate/socket errors. A
separate ~5-second precise DROP of a BIND flow produced exactly 18101 missing
RTP packets for 18101 IFC hits and recovered without clearing BIND. That
qualified the tested routed-with-CPU-copy path, not every pure-hardware path.
The first terminal-ALLOW cold-learning failure is explicitly not retained.

The five old 945 patches are consolidated into two: group snapshot/report
ordering and final drop-only driver/applied diagnostics. The current PON flag
bit-index API is used. The existing fixture retains mocked policy assembly,
VLAN replacement/isolation, capacity, failure and generation cases; it is not
an ASIC emulator or a live operator-VLAN migration.

For this submission only source composition and whitespace were checked.
No new tests, build, flash or device access. The new baseline and wider topology,
pure-hardware, long-duration and migration cases still need qualification.
