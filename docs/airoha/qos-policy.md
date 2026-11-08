# AN7581 priority and explicit DSCP hardware policy

This is an optional coordinated QoS feature with an intentional AN7581 default
queue-policy change. It is not an assertion that every upstream priority
mapping is universally wrong. Other Airoha SoCs retain their prior mapping.

## Modes

`priority` is the package default (`compat` remains an alias). Priority 6/7
maps to Q7, 0/4/8–15 to Q4, and 1/2/3/5 to Q0. Larger non-classid values use
the low four bits, like Linux PRIO. Classid-based hardware paths retain their
ordinary class handling. Original `skb->priority` remains available to software
qdiscs. PPE keeps its existing `rt_tos2priority()` classification input, so this
mode does not promise identical inputs across CPU, PPE and switch-only paths.

Explicit `dscp` mode owns the shared hardware scheduling domain and uses a full
64-entry map directly for CPU TX metadata, PPE queue metadata and switch UP.
All eight queues remain available; this result is not passed through the
three-band priority map. The bounded CPU header reader does not linearize or
allocate skb data; unsupported formats fall back to priority policy. The mode
does not overwrite skb priority, DSCP, ECN or wire PCP.

The versioned base DSCP map is `[0,0,2,2,6,6,4,4]` repeated eight times. It
preserves the prior PPE numeric convention, not a standard DiffServ/WMM table.
Business marking belongs in administrator firewall/application policy. No
client identities, operator VLANs, application classifications or nft/tc rules
are installed by these packages.

## Ownership, configuration and limits

The FE/QDMA/switch transaction checks activity and ownership, writes/readbacks
owned fields, and performs bounded masked rollback. Optional QoS failure must
not block basic switch/interface initialization. Unknown or failed restoration
is reported; it is not displayed as a confirmed active profile.

Priority mode accepts compatible eight-band ETS offload and retains HTB's
entrypoint; unrelated owners cannot replace each other. DSCP mode rejects
conflicting ETS/HTB hardware setup. Hardware SP has Q7 highest and Q0 lowest;
there is no minimum-bandwidth or starvation guarantee.

`airoha-qos` supplies a Generic Netlink controller and boot-only UCI apply.
`luci-app-airoha-qos` exposes Network → Airoha QoS, explaining both modes and
showing the complete effective/saved maps and pending/fault state. Saving only
commits `airoha_qos`; it does not invoke global apply or reboot. Hardware mode/
map changes require reboot. Service restart does not replace an active policy.

PPE retains its original flow lifecycle. Marks must be consistent in both
directions before learning and stable within an accelerated connection. This
feature does not implement per-direction learning or per-packet post-bind DSCP
tracking, and does not close the asymmetric-flow issue #37. New application or
firewall marking can be subject to existing flowtable bypass/cache semantics.

## Review and evidence

This proposal depends on the separate TX correctness series in PR #66; its two
commits are included as prerequisites. The priority/ETS policy, DSCP metadata,
shared transaction, Netlink/controller and LuCI components remain separate
commits. Existing KUnit cases are embedded in the kernel patches. Retained host
controller/UI fixtures and a QoS-only KUnit configuration are provided:

```sh
mkdir -p tmp
python3 tests/airoha/test-qosctl.py
node tests/airoha/test-qos-ui.js
```

During export, the six kernel patches were composed against the selected
current-upstream source baseline and reproduced the reviewed source tree.
Current device-bit APIs, DSA statistics fields, LRO admission and BQL locks are
preserved. No new tests, complete kernel/package build, browser verification or
device operation was performed. Historical appliance builds/measurements are
not new-base validation or proof of exact ASIC queue selection/fairness.
