# Airoha PCS configuration and AN7581 HSGMII recovery

The first patch retains the first failed pre/post configuration operation until
a new configuration attempt. The pre callback is void, so later callbacks
return the saved error and link-up avoids reopening a partially configured
port. Ethernet SCU selection and MAC reset/fragment setup are checked. This is
not a retrofit of error checking into every older analog-initialization write.

The second patch covers AN7581 Ethernet 2500Base-X with in-band negotiation
disabled. In external-PHY mode, phylink does not run its ordinary PCS polling
timer; an additional workaround only in `pcs_get_state()` would miss that mode.
The match-data flag limits the worker to this hardware path; no runtime UCI
or new stable kernel ABI is introduced.

While link-up is requested, the sleepable worker reads three status registers
once per second. Missing RX sync and a present analog signal, confirmed after
100 ms, permit lane-local CDR recovery. Six signal samples require four
positives. Isolated FreqDet/internal-port differences and read errors do not
authorize recovery. Preserve SDK CDR ordering and 700/100 us sleeping waits,
then verify after 100 ms. Three failures enter a 30-second cooldown.

Configuration, link callbacks and recovery use a per-port mutex. Work is
cancelled before teardown, outside that mutex. The worker takes no RTNL/PHY/
phylink lock. It does not reset the PHY, MAC or shared blocks, perform a full
calibration, or change PON, rate adaptation, fragmentation, QDMA or QoS.
There is no new per-packet processing or ordinary-state logging.

Reference: [SDK Ethernet xSGMII implementation](https://github.com/lotusmomo/airoha_sdk/blob/32b5aa356c2406edef8806aaf75451ff0a5286f2/linux/arch/arm64/mach-econet/sgmii/air_eth_xsgmii.c),
particularly `TMR_SGMII_LINKDN_CB`, `RX_SigDet_Flag` and `RX_CDR_RST`.

The retained host fixtures compile actual callback/recovery functions against
controlled MMIO, time and workqueue stubs. They cover failures and deterministic
lifecycle interleavings, not real scheduling or physical recovery:

```sh
python3 tests/airoha/test-pcs-config.py /path/to/prepared/linux
python3 tests/airoha/test-pcs-recovery.py /path/to/prepared/linux
```

During this export the patches were applied to the current upstream source
subset; no new test, full kernel build, flash or physical fault injection was
performed. Historical loss of HG connectivity has not been proved to originate
in this PCS path. This proposal must not be cited as its demonstrated cure.
