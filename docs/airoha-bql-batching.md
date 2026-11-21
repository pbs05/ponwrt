# Airoha BQL completion batching proposal

A hardware TX ring can carry packets from different software TX queues.
Aggregate completion packets/bytes by the actual `struct netdev_queue *`,
then call BQL once per owner at the end of the completion poll. Never
aggregate different software queues merely because their hardware ring matches.

Retain the current upstream `dev->txq_lock[qidx]` serialization across
completion rings, now held around each aggregate. Keep current device flag,
PON accounting and shared-ring wake-all behavior. This is an optimization
proposal, not a new explanation for historic loss or management stalls.

Each IRQ queue receives preallocated storage sized to its completion ring
(2048/1024 entries). Shutdown reuses IRQ0 storage only after NAPI stops and
processes one 1024-descriptor TX ring at a time. No allocation occurs per skb.
Owner lookup is linear in the number of distinct owners within a poll;
pathological owner diversity has quadratic lookup work, so no universal CPU
improvement is claimed. Completion is deferred to the end of this poll,
which also needs latency/queue-occupancy qualification.

The source export preserves the reviewed prepared-kernel tree on the pinned
submission base. No tests, build, flash or fresh throughput/latency matrix
were executed during submission preparation. Existing historic results do
not qualify this adaptation. Keep this proposal separate from RX copybreak,
which adds a copy and is disabled by default.
