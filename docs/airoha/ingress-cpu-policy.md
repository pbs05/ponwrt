# XG-040G-MD ingress CPU policy

This series keeps forwarding policy outside the Ethernet driver. It supplies a
read-only driver map and an optional userspace policy. `airoha-cpu` ships with
`enabled=0`: installing/flashing this package does **not** enable CPU pinning.
It requires the XG-040G-MD board, four online CPUs, mounted debugfs and the
`airoha-napi-map 1` driver interface. No CPU-frequency policy is changed.

## Scheduling model

| Work | Default allowed CPU(s) | Mechanism |
| --- | --- | --- |
| QDMA1 ordinary WAN RX, TX completions | 1 | IRQ banks 0/1 plus corresponding threaded NAPI |
| QDMA0 ordinary LAN RX, TX completions | 2 | IRQ banks 0/1 plus corresponding threaded NAPI |
| Both QDMA special RX, including WAN ring 6 multicast copy | 3 | IRQ banks 2/3 plus corresponding threaded NAPI |
| Known migratable non-QDMA GIC device IRQs, new IRQ default | 0 | Allowlisted device actions and default IRQ mask |
| Ordinary userspace including procd and rtp2httpd | 0,3 | Existing unrestricted threads; new children inherit |
| Unbound kernel workqueues | 0,3 | Global unbound workqueue mask |

The mapping is by **ingress**, including the corresponding TX-completion
instances. A packet can leave by the opposite physical direction; the CPU
policy does not pretend to reserve every WAN transmit instruction on CPU1.
DSA switching and PPE offload continue independently of these CPU affinities.
RPS and RFS are disabled only on discovered Airoha RX queues and associated DSA
ports. XPS, qdiscs, DSCP classification, PPE rules, real-time priorities and
threaded-NAPI mode are unchanged. There is no periodic policy daemon outside
an active trial.

This reduces avoidable migration and ordinary application competition. It
does not promise hard real-time latency: timers, RCU, bound workqueues,
interrupt-disabled sections and kernel work on behalf of sockets still exist.
CPU0 and CPU3 share ordinary applications; CPU3 also handles special receive
copies. Busy applications can still contend there, so latency is measured
under representative load before deciding to retain the policy.

## Device interface and rollback

`/sys/kernel/debug/airoha_eth-*/napi_map` has a versioned header and the fields
`qdma kind queue bank irq pid descriptors buffer_bytes queued`. It identifies
the internal dummy-netdev NAPI threads that regular netdev enumeration misses.
It is read-only and carries no CPU selection. RX instances with `bank=-1` are
allocated but have no routed interrupt and are not pinned. TX completion
instances use bank 0. IRQ action names are cross-checked against the map before
any write; neither Linux IRQ numbers nor truncated task names alone identify
queues. The map associates NAPI with its IRQ using `netif_napi_set_irq()`;
that association by itself does not change thread affinity.

The controller preflights the whole policy, journals original masks before
writing, verifies requested **and effective** IRQ masks and NAPI thread masks,
and restores on a failed operation. The runtime journal is root-only under
`/tmp/airoha-cpu/`, includes the boot ID, and never becomes a boot configuration.
Each task is identified by TID and `/proc` start time. Exited/reused tasks are
skipped; external changes to a different mask are reported and preserved.
The interface event hook reuses original rollback values and handles newly
created NAPI instances. A changed UCI policy requires restore/reapply.

User tasks with an existing custom CPU mask are preserved. procd and existing
unrestricted threads are restricted to the service mask; descendants inherit
it. Restore also restores surviving children born during ownership. Linux does
not record affinity provenance: a newly created child explicitly assigned
exactly the managed mask cannot be distinguished from an inheriting child and
will also be restored. Preexisting custom tasks are excluded. A custom procd
mask incompatible with the selected policy fails preflight.

The existing `packet_steering` service calls the platform hook. While this policy
owns steering, generic steering cannot overwrite it. When disabled and no trial
is active, the hook delegates to the original OpenWrt script, preserving both
`packet_steering` and `steering_flows`. Other affinity managers such as irqbalance
must not independently manage these IRQs; conflicting writes are an error,
not silently corrected by continuous polling.

## Operator workflow (on a qualifying installed kernel)

```sh
airoha-cpuctl plan                  # JSON preview, no changes
airoha-cpuctl status                # actual IRQ/thread masks and map
airoha-cpuctl trial network 180     # IRQ/NAPI/RPS only
airoha-cpuctl restore
airoha-cpuctl trial services 180    # also userspace + other device IRQs
airoha-cpuctl restore
airoha-cpuctl trial full 180        # also unbound workqueue mask
airoha-cpuctl restore
```

A detached local watchdog is acknowledged before the first trial write. It
checks the monotonic lease every two seconds and restores after expiry even
when SSH disconnects. `renew TOKEN` extends only a still-valid lease; UCI stays
disabled. `restore` is idempotent and retains failures for retry. It never
restores a previous setting over an externally changed, different mask.

Only after acceptance:

```sh
uci set airoha_cpu.main.enabled='1'
uci commit airoha_cpu
/etc/init.d/airoha-cpu reload
```

To disable, set `enabled=0`, commit and reload. No reboot is required for the
CPU policy. No online CPU hotplug is supported while it is active: restore
before changing the online CPU set. The black-box script uses trials, never
enables the persistent setting, and checks config hashes after cleanup.

## Limits and submission validation

The global unbound mask does not constrain all bound/per-CPU kernel workers.
This package does not install or modify WireGuard/mihomo and does not claim
that VPN cryptography or socket work is excluded from WAN/LAN CPUs. Timers,
RCU, bound workqueues and other execution contexts can still interfere.

The CPU policy requires the IRQ/NAPI metadata proposal. The separate multicast
proposal changes WAN ring6 depth; that memory/capacity choice is not implemented
by this package. Optional IFC filtering and DSCP hardware policy are likewise
separate concerns.

Existing `tests/airoha/test-cpuctl.py` exercises the real ucode policy under
in-memory I/O and stub UCI. It is retained, not newly run during this export.
No new tests, package/kernel build, flash, browser or hardware operations on
the current baseline. Historical use does not establish a universal WAN
throughput, idle-latency or loaded-jitter benefit. Measure representative
loads and restore the baseline if pinning creates a bottleneck.
