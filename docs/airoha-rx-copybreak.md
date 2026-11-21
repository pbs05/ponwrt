# Optional Airoha RX copybreak

`/sys/module/airoha_eth/parameters/rx_copybreak` defaults to **0** (disabled).
Nonzero values are capped at 2048 bytes. The optional copy targets complete,
nonaggregated single-buffer frames backed by a buffer larger than 2 KiB.
It does not resize LRO buffers or change scatter geometry. PON control is
handled by its existing separate path before this decision.

Successful copies recycle the source page exactly once and use an ordinary
NAPI skb; allocation failure increments a counter and falls back to the
existing page-backed path. Aggregated LRO and partial scatter frames keep
the current upstream handling. BQL batching is a separate proposal.

Root-only `datapath_stats` in the existing Airoha debugfs directory reports
per-ring copied packets/bytes, allocation fallbacks, and BQL packets/calls.
Reads use memory counters, not MMIO or a coherent cross-ring snapshot.
There is no polling daemon. The RX candidate check still has a small cost
with copybreak disabled; BQL statistics are updated by the batching proposal.

Copying can release oversized pages sooner but adds CPU/memory bandwidth
work. Historical load comparisons did not justify enabling it globally;
the retained final change explicitly restored the zero default. Do not
infer a gain from the presence of the option or counters. New-base traffic,
latency, memory-pressure and allocation-failure qualification is pending.
No tests, builds, traffic generation or hardware changes were performed
while preparing this source submission.
