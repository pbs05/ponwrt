# Board-opt-in EN7581 native CPUFreq

## Intended behavior

The Nokia XG-040G-MD **UBI** image exposes 500 through 1400 MHz, in 50 MHz
steps, through the normal kernel CPUFreq policy. The compiled default governor
remains `ondemand`. Thus 1400 MHz is the default **upper limit**, not a fixed
idle clock. Changing Max Frequency in LuCI does not select `performance`.

The default is in the board device tree and kernel driver. No rc.local command,
init service, delayed job, UCI default or PLL write from userspace implements
it. The earlier experimental `hg-cpu-frequency` service and its sysupgrade
retention entries were removed from HG before this change. This work does not
update BL2, BL31 or U-Boot, change CPU voltage or enable 1500 MHz.

The user reported an immediate HG reboot when selecting 1500 MHz with the old
plugin. There is no crash/reset log identifying the cause. Insufficient timing
margin at the existing voltage and a failure during the old userspace PLL
transition are possible; it is not evidence that 1400 MHz is stress validated.
The plugin's inherited claim of stability to 1500 MHz has been removed.

## Source references and design choices

* [fzs209/immortalwrt, XG-040G-MD branch, pinned DTS](https://github.com/fzs209/immortalwrt/blob/8bf099e8558a910f6545796dd6efbfb59b9b5e91/target/linux/airoha/dts/an7581.dtsi)
  supplies native OPPs through 1400 MHz and uses a native PLL fallback. Its
  global 700 MHz base renumbers existing performance states; that part is not
  carried over.
* [naoki66's AN7581 direct-PLL opt-in](https://github.com/naoki66/ImmortalWrt-for-Gemtek-brightspeed/blob/478998097b47a3decba9bbf2bf3dd15cf82fdd16/target/linux/airoha/patches-6.18/0402-pmdomain-airoha-allow-board-opt-in-direct-pll.patch)
  separates a working SMC interface from one that can actually represent higher
  OPPs. This is a useful same-SoC reference, not a test on HG.
* [Airoha ATF clock implementation](https://github.com/Ansuel/atf-airoha/blob/94892b9e42eb1f6992e60bce904c3ef42ea995c8/plat/ecnt/en7523/ecnt_cpufreq.c)
  defines the original 500–1200 MHz state table, the eight-bit PCW field,
  PLL2/ARMPLL switching order, source-selection delay and readback. Its frequency
  decoder rejects ARMPLL frequencies outside its known table. This explains
  why an out-of-band PLL change can leave SMC-based CPUFreq reporting unknown;
  it does not establish the cause of a board reset.

### Kernel patch

`608-pmdomain-airoha-add-opt-in-native-EN7581-PLL-control.patch` adds the optional
`airoha,force-direct-pll` backend. Without that property the existing SMC path
is unchanged. Direct mode is rejected for other SoC compatibles.

Performance state N remains `500 + N * 50` MHz. States above 18 are rejected
before arithmetic or hardware writes. The native clock reader decodes the
selected clock source, PLL and divider, rather than asking an ATF table that
ends at 1200 MHz. The driver preserves register bits outside the relevant
fields and the previous PLL write-protection value.

The transition switches to PLL2, programs ARMPLL, waits 20 microseconds and
switches back. Source and programmed fields are read back; a programming error
attempts to restore the previous PLL settings. If switching back fails, PLL2
is kept enabled and the call reports an error. Readback confirms register
programming only, not analog PLL lock or CPU stability.

All four cores share this clock. A small SCU helper provides a common mutex
for the native PLL transaction and thermal mux setup, which use the same PLL
write-protection register. A raw spinlock with local IRQs disabled additionally
keeps clock readers from mixing the PCW of one state with the divider of
another. Other CPUs need not be stopped. The mutex can wait for the thermal
initialization's existing 10 ms sequence; normal EN7581 temperature reads do
not change the mux. This is control-path locking, not a packet-path hook or a
background task, and its wall-clock cost has not been measured.

The initial local draft used `stop_machine()`. Source review found that its
CPU hotplug locking is unsuitable inside CPUFreq paths that can already hold
the hotplug lock. That build was cancelled and this draft was never deployed;
the final design uses the shared register transaction mutex instead.

### Board patch

Only `an7581-nokia_xg-040g-md-ubi.dts` opts in and appends states 15–18 for
1250, 1300, 1350 and 1400 MHz. Existing states 0–14 and their CPU OPP references
are retained. The shared AN7581 DTS and the MD common file are not changed,
because the latter is also included by the TF board.

The existing shared CPUFreq policy and CPU cooling registration remain in use.
This does not revise the platform's existing thermal thresholds or prove their
behavior under overclocking. ATF/bootloader startup may still use 1200 MHz;
1400 becomes available when Linux starts its governor.

### LuCI patch

`luci-app-airoha-npu` r11 recognizes the board's native PLL property. It reports
the kernel frequency, hides the old direct-PLL controls, and rejects the old
RPC write method even if called from a cached page. Max Frequency and Governor
remain the normal CPUFreq controls. Changes made on this status page are
runtime-only; reboot uses the firmware table and default governor again,
unless the operator separately installs a policy service.

The r10 numeric-status fix is retained for older kernels that return the literal
`<unknown>` from `cpuinfo_cur_freq`. Non-native boards retain their existing
manual controls, with corrected wording that promises no stable overclock.

## Submission baseline / limits

Backend and board-ceiling changes are separate commits: the backend can be
accepted without raising the board default. Only MD UBI opts in, not shared,
TF or USB-SFP definitions. Current upstream AN7581 still defaults to ondemand.

The rebase preserves newer thermal diode/mux selection, readback retries and
post-sampling checks; it wraps the actual shared write-protection transaction.
It does not replace the new thermal implementation with the older one.

Use normal CPUFreq controls and update any optional NPU plugin to its native-aware
version. Out-of-band userspace PLL writes are not serialized by the kernel mutex.
Register readback is not analog-lock or electrical-stability validation.

This export checked source composition and whitespace only. No new tests, full
kernel/DTS build, schema validation, flash or overclock stress. Earlier device
use does not qualify every unit/environment or the new baseline. No reset
root cause or universally safe higher frequency is claimed.
