# QDMA IRQ/NAPI observability

Register RX-bank and TX-completion IRQ associations in NAPI and expose a
root-only `napi_map` under `/sys/kernel/debug/airoha_eth-DEVICE/`. The driver's
internal dummy netdev is not enumerated by ordinary netdev-genl discovery.
The map records QDMA, queue kind/id, bank, IRQ, NAPI thread PID, ring descriptors,
buffer bytes and queued count. A missing thread PID is zero, not a guessed
thread from task-name/order matching. Preallocated unrouted rings remain visible.

Reads take the internal netdev lock while inspecting thread identities. Remove
and drain debugfs readers before NAPI/thread teardown. This is diagnostic
metadata, not a CPU-affinity policy or automatic balancing implementation.

The second patch records enabled coherent/no-descriptor error causes before
the existing W1C acknowledgment. Counters retain per-bank error IRQ count,
seen/last masks and jiffies. These describe acknowledged errors, not lost
packets, descriptor recovery, inactive masked causes or a hardware fault cure.
Rows are not atomic cross-bank snapshots. Reading performs no MMIO access.

When DEBUG_FS is disabled the hot-path recorder returns without diagnostics;
when enabled it checks a fixed mask and updates memory only on observed error
causes. There is per-bank storage and a small IRQ-path check, not per-packet
logging or a userspace polling daemon. IRQ masks, acknowledgment order, DMA
state and packet forwarding are unchanged.

This export is stacked on the QoS proposal for common driver source context;
the map/recorder themselves do not require the multicast backend or a CPU
policy package. Existing host fixtures are retained without being rerun. Both
patches apply after that source baseline. No new tests, full build, flash or
hardware verification for the new submission baseline is claimed.
