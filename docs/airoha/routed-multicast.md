# AN7581 routed IPv4 multicast offload

## Scope and dependencies

This is an opt-in integration proposal, not a general multicast router or a
replacement for IGMPproxy, bridge snooping, fw4 or business classification.
Enable `CONFIG_NET_AIROHA_MCAST` explicitly in a qualifying kernel configuration;
its default remains n. The package's saved default is enabled with logical
interfaces `iptv` and `lan`; select the actual configured logical interfaces.
Installing a package does not prove that the kernel/backend is available.
No operator VLAN, channel list, endpoint address or forwarding permission is
provided. VLAN IDs are derived from the validated interfaces.

This series is stacked on the QoS, UDP fraglist, IPMR GSO and NPU diagnostic
proposals. It uses the QoS egress helpers and the corrected IPv6 debugfs context;
UDP/GSO prerequisites cover the software learning/fallback path. Those changes
remain separately reviewable. The old internal 942-01 through 942-15 series is
folded into three kernel patches: bridge state, IPMR state, and the final backend.
The WAN CPU-copy RX-ring increase remains a fourth independent patch.

## Admission and delivery

- Exact IPv4 UDP (S,G) multicast routes, a validated PON ingress and one bridge
  or bridge-VLAN egress scope, uniform supported VLAN actions and standard TTL
  thresholds. Unsupported routes/options/fragments/tunnels/targets stay software.
- Source-aware bridge forwarding state includes membership, router ports, VLAN,
  port/STP/filter mode and event generation. Membership/event state is separate
  from the final route and software-TX acceptance evidence.
- An opaque cookie tracks the same learning datagram through every required
  accepted output. BIND is published after successful DMA mapping and the TX
  doorbell, under the TX lock. Submission is not physical-egress completion and
  does not drain other software queues or RX rings.
- Preserve the learned hardware key. Clear stale unbound action bytes and assert
  SDK structure offsets before constructing the action. Bound-key replacement,
  aged/reused slots and event epochs are validated before mutation.
- Only actual local consumers request a CPU copy. Restore ingress metadata and
  pre-routing headers for that copy; avoid duplicate IPMR routing and bypass GRO
  for learning/local-copy paths where ordering/metadata requires it.

## SDK egress and lifetime

For ordinary switch-only LAN output, use the SDK GDM1/STAG tuple: fport 1,
fqos 1, fast 0, nbq 1, with the complete destination bitmap. Cache negotiated
link speeds outside the packet path and choose the fastest subscribed member,
tying by hardware port number. Use that member's observed software TX channel.
Unknown speed/identity and native/mixed output keep QDMA replication; native
GDM4 uses the existing reserved replication slot. Shared-channel fanout does
not implement per-port QDMA shaping.

The source reference is lotusmomo/airoha_sdk commit
`32b5aa356c2406edef8806aaf75451ff0a5286f2`, especially `ra_nat_multicast.c`,
`ra_nat_entry.c` and `foe_fdb_7581_64Byte.h`. The implementation uses Linux route,
bridge, notifier and netlink lifetimes instead of vendor interface-name hooks.
A link query uses RTNL only in the existing maintenance worker, not packet RX.

Route, bridge, link, address and policy events revoke affected state. A bounded
64-slot miss cache suppresses repeated unsupported-route lookups for at most a
second; events advance its epoch and permit immediate recheck. It never grants
hardware drop authority or drops packets. A two-second maintenance worker checks
aging and link-cache work; no new busy-polling daemon is added.

Uncertain hardware writes/revocations mark the backend broken. Replica-slot
isolation can contain replicated output, but cannot contain GDM1 direct output;
the final fault fence may stop PPE. This can affect other acceleration and is
not lossless software fallback. Do not write to a slot that has changed owner.
Recovery from a hardware invalidation fault requires a reboot/device recovery.

The conditional AN7581 WAN ring6 change uses 1024 descriptors for sustained CPU
copies, preserving LRO buffer geometry and IRQ routing. It applies only when
multicast support is compiled; this can allocate additional ring memory even
when saved runtime policy is disabled. It is not an IRQ-affinity change.

## Configuration and online policy

`airoha-multicast` supplies a Generic Netlink controller, bounded boot worker,
applied-policy state and hotplug recovery. LuCI separates Save from Save & Apply.
A capable kernel withdraws old flows and revalidates a changed scope online;
software forwarding and playback jitter may occur. WAN/LAN are not restarted.
Saved but unapplied edits are not used for interface-recreation recovery. Older
kernels report reboot-required rather than pretending that a live change worked.

The optional fw4 include retires multicast flows after a firewall update;
it grants no forwarding permission and is separate from unicast flowtables.
Existing packets on the wire cannot be recalled. A diagnostic pause is retained.
Status reports readiness, active entries and refusal/error counters separately.
Software action diagnostics are not hardware register readback.

## Evidence and limits

Earlier AN7581 observations covered software/hardware RTP delivery, local copies,
natural leave and non-subscriber MT7530 `eth-mac` hardware counters. One recorded
~126-second pair received 325441 / 323529 RTP packets with no missing, duplicate
or reordered packets and no TS/TTL/socket errors. Non-subscriber total TX stayed
below the existing 65536-byte budget, excluding sustained video flooding but not
proving zero packet leakage. Earlier software `ethtool -S` counters were not
valid evidence for physical-port isolation; use the standard hardware groups.

Existing host fixtures cover actual encoders, event selection, miss-cache keys,
TX submission ordering, SDK switch selection/fault fences and policy control under
stubs. They do not model ASIC buffer return, concurrent DMA or physical CPU-copy
formats. Blackbox infrastructure is proposed separately.

For this export, only source rebase/composition and whitespace were checked.
No tests were rerun and no new kernel/package build, flash or device access was
performed. Broader physical topologies, long-duration concurrent load, source
key isolation and firmware combinations require qualification on the new base.
The historical PSE exhaustion investigation must not be reduced to a newly
proved single cause by this source export. IFC background filtering and CPU
placement are separate proposals.
