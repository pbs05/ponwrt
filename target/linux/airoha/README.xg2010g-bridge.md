# XG2010G wired bridge offload in PonWrt

The `gemtek_xg2010g` profile keeps `pon0` as WAN and bridges the four panel
Ethernet ports as LAN. Its wired bridge acceleration uses `bridger` to learn
the Linux bridge forwarding database, install TC flower rules, and program
Airoha PPE L2 templates and IP subflows. The former nft bridge flowtable
patch set is not used. Routed NAT offload still uses the normal nft `inet`
flowtable and is independent of the bridger service.

The expected direct Ethernet egress mapping is LAN1 GDM4/NBQ0, LAN2 GDM3/NBQ5,
LAN3 GDM4/NBQ1, and LAN4 GDM1/DSA port 4. PonWrt already writes the direct
SerDes NBQ into PPE entries and has the lossless per-device MIB reader.
GDM2 remains the PON WAN data path; its bearer mapping and invalidation
logic are retained. The L2 PPE template does not decrement IP TTL or IPv6
hop limit, while routed entries retain the decrement.

`bridger` is a distinct offload producer. Its TC `in_hw` rules and matching
PPE BND entries, together with bidirectional endpoint traffic, are the
evidence for hardware L2 offload. The conntrack `HW_OFFLOAD` mark is useful
for routed flows, not a substitute for the L2 checks. Stopping bridger
removes its acceleration rules and leaves the Linux bridge as the fallback.

The two local bridger backports report Netlink errors and try the DSA
conduit before its user port. This is needed for LAN4 ACK return traffic:
the lower `eth0` device accepts the TC flower rule when LAN4 does not.

The original project's 1 Gbps IPv4 TCP, single-tag VLAN, NAT, and TTL
observations apply only to that installed Ethernet-only firmware. This
PonWrt integration has separate source and build verification; it must not
inherit those runtime results without a matching device test. PON
registration, optical TX, QoS loopback, IPv6, PPPoE, QinQ, and sustained
high-speed offload are outside the source checks here.

Native AN7581 L2B entries use a separate hardware layout. The software L2
profile remains unchanged for IPv4/IPv6 subflows. Patch 930 preserves the
learned native ingress key and writes forwarding control at offset 44.
The unverified native NPU statistics redirect is skipped; a zero per-entry
packet counter is not proof that forwarding failed.

This full-bridger variant removes the nft bridge flowtable extension
repository-wide, while selecting bridger only for gemtek_xg2010g. Other
board profiles therefore do not retain that nft bridge fastpath by default.
It preserves the ordinary inet routed flowtable and upstream BBRv3/CAKE
and additional device definitions. It does not add diagnostics, HomeProxy,
the NPU dashboard, USB PCS changes, or PON configuration changes.
