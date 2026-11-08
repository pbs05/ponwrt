# AN7581 16K SRAM congestion profiles

These patches initialize LAN QDMA0 and WAN QDMA1 only when LMGR reports SRAM
mode and exactly 16,384 forwarding descriptors. Other chips, DRAM pools and the
2048-descriptor allocation fallback retain their existing configuration.

| Resource | LAN normal max/min | WAN normal max/min |
| --- | --- | --- |
| Total | 0x3ff0 / 0x3f00 | 0x3999 / 0x2e00 |
| Channel | 0x3ff0 / 0x0400 | 0x3999 / 0x0400 |
| Queue | 0x2000 / 0x0020 | 0x2000 / 0x0020 |

DEI limits halve each field separately. Configuration, packet and time triggers
use the SDK's 250 us interval. Existing congestion protection, unowned control
bits, scheduling, classification and wire markings are preserved. This is
initialization work, with no new per-packet work or userspace rule injection.

Reference: [AN7581 SDK profile](https://github.com/lotusmomo/airoha_sdk/blob/32b5aa356c2406edef8806aaf75451ff0a5286f2/private/qdma/qdma_lan/qdma_dev_7581.h),
[initialization selection](https://github.com/lotusmomo/airoha_sdk/blob/32b5aa356c2406edef8806aaf75451ff0a5286f2/private/qdma/qdma_lan/qdma_dev.c),
and [threshold programming](https://github.com/lotusmomo/airoha_sdk/blob/32b5aa356c2406edef8806aaf75451ff0a5286f2/private/qdma/qdma_lan/qdma_api.c).
The SDK's descriptor-storage variable called `qdmaDscpMode` is unrelated to IP
DSCP classification.

Historical XG-040G-MD measurements found channel/queue minima 22/2 and missing
19,840 of 669,696 burst datagrams at a 500 Mbps payload average on the
2.5G-to-1G hardware-offloaded LAN path. After the LAN repair, nine covered UDP
cases received every unique sequence among 26,487,697 sent datagrams, with
PPE BND and HW_OFFLOAD retained. These are local October 18 measurements on
the appliance patch stack, not a new result for this upstream branch.

TCP congestion drops remained under sustained competition. The tests did not
validate sustained UDP overload, all traffic combinations or a hard latency
bound. Raising admission thresholds can increase buffering delay. The WAN
profile has source/register evidence, but its causal performance benefit is
not established by the LAN measurement.

The retained host fixture `tests/airoha/test-congestion-profile.py` compiles the
actual LAN initializer from a prepared kernel tree against fake MMIO. Its
scope is the LAN helper and initialization ordering; it does not validate WAN
hardware or a complete kernel build. It was not rerun during this export.

```sh
python3 tests/airoha/test-congestion-profile.py /path/to/prepared/linux
```
