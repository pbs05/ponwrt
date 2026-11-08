# Bounded optional PON RX metadata

AN7581 creates root-only debugfs `pon_rx_samples` in the existing
`airoha_eth-*` directory. Sampling starts disabled. It stores at most
64 fixed-size header records for one IPv4 multicast group or raw PON control,
with a maximum 60-second RX-checked lifetime. No worker, per-packet printk,
forwarding/register change or payload capture is added. When disabled, the
existing PON statistics path checks the active flag only.

Use one complete command per write, for example `control 8 30` or
`239.1.2.3 32 10`. `stop` retains records; `clear` stops and empties them.
Reads return an immediate bounded snapshot; they do not wait for completion.
Check `active=0` before retaining the final sample. A new arm clears old records.
Only two VLAN headers and fixed IPv4/TCP/UDP fields are parsed. Non-first IPv4
fragments have no transport ports; the length is that descriptor's buffer.

These are CPU-delivered first-buffer records after FE/PPE, before software
GRO, not a line-rate optical capture. Fully offloaded packets without a CPU
copy are invisible. Preserve source port, CPU reason and raw words when
interpreting them. `pon_id_field` is raw msg0[29:14], not proof of an on-wire
EPON LLID; the same field has protocol/path-dependent meaning in the SDK.
This observation interface does not determine why background traffic arrived
and does not filter it.

The fixed buffer is device-managed and outlives NAPI teardown; debugfs removal
drains readers/writers. Arming, rearming and snapshots use a short spinlock;
header parsing is outside that lock, with selector/expiry checks repeated
before committing a sample. No optional userspace package is required.
A separately submitted PON collector enhancement reads an existing snapshot
without arming it and remains compatible with kernels lacking this interface.

Source/export composition and whitespace checked on the pinned submission
base. Historical limited sampling informed the field contract; no new tests,
full build, flash or fresh concurrency/hardware qualification were performed.
