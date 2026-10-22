# PonWrt

English | [简体中文](README_zh.md)

PonWrt is based on [ImmortalWrt](https://github.com/immortalwrt/immortalwrt) and adds support for Airoha AN7581 and AN7583 PON devices.

## HWTC O5 investigation branch

This fork's `rayniel/hwtc-control-latency` branch addresses a measured
post-O5 control-path stall. Flow replacement waited for a networking RCU grace
period while the xPON caller held its PLOAM command lock and the OMCI daemon
waited for the mapping write before replying. The first fix expedited that
wait, but retained it even when no hardware path was being retired.

Flow replacement now checks the old GEM/T-CONT paths. When all remain usable,
including VLAN/P-bit-only changes and path additions, it frees the old table
with `kfree_rcu()` without synchronously waiting. Removing or reassigning any
old path, or clearing a legacy fallback mapping, still waits for an expedited
grace period before returning, so the caller can safely retire old hardware
mappings. Controller unregister retains its networking RCU synchronization.
This does not ignore OLT Deactivate, force O5, change credentials, suppress
key-confirm errors or make unsupported OMCI entities return success.

`feeds.conf.default` pins driver commit `67df6ec` and userspace commit `70302c9`:
`kmod-airoha-xpon` r75, `kmod-airoha-en7572` r15,
`airoha-pond` 0.3.0-r3, `airoha-ponctl` 0.2.0-r2 and
`luci-app-pon` 0.1.0-r4. These include the opt-in stock BWmap policy and the
four missing standard PM entities. Rebuild the kernel/firmware as well as
the feeds; updating only the xPON package cannot change this `airoha_eth` fix.
If reusing a build tree, update the two PON feed definitions to these pinned
revisions rather than retaining previously selected feed versions. A local
`feeds.conf` overrides `feeds.conf.default`; check both the effective feed URLs
and the installed feed revisions. The pinned objects are fetched from the
public `pbs05` repositories, without reverting to their older `main` tips.

This branch integrates `master` through `edaeeb300c9d` (Linux 6.18.55),
including Airoha thermal sensor fixes, opt-in fraglist GRO, HG5585F-CT PHY
identification and UNG00A port/LED corrections. Patch 925 retains PPE
mapping serialization and affected-flow invalidation while preserving patch
922's conditional RCU retirement. A clean Git merge alone is not sufficient:
the complete kernel patch stack and both target builds must pass before
deployment, followed by device-specific PON and offload validation.

EN7572 frontend r14 rejects populated BOB calibration tables with analog codes
that exceed their register field widths. Validation runs with BEN closed,
before uploading calibration to MD32; invalid tables fail probe explicitly
instead of being reported as ready or truncated into registers. Erased-table
handling remains unchanged. This protects against observed invalid calibration
data, but does not fix its underlying read cause or guarantee XGTC/O5 recovery.

Driver r75 improves the existing implementation using selected external fixes,
not a wholesale fork merge. Threaded PLOAM IRQ processing tries `state_lock`
and defers FIFO draining to `link_work` on contention, preserving the
`state_lock -> ploam_lock` order without blocking an IRQ that MAC recovery
must synchronize. Deferred Deactivate is still processed if frontend status
sampling fails. AES switch completion remains advisory; a rate-limited warning
and `key_switch_timeouts` in the debugfs `crypto` snapshot expose missing
completion without claiming that hardware switched keys.

Frontend r15 reloads MD32 on subsequent interface starts, reusing probe's image
for the first prepare. GPIO TX_DISABLE and BEN close before reload; failure
leaves the provider unprepared, with an explicit error. Empty-table defaults
and r14's validation remain. This does not reload firmware on every OLT
Deactivate, replace the activation state machine, copy the full A2 analog
calibration into A0 or enable experimental GPON support. Existing ACK, BWmap
and RCU-path fixes remain in place.

For the affected AN7583/HWTC setup, select **Stock filters (HWTC compatibility)**
under **PON → Configuration → PON line → XG-PON BWmap compatibility**, save
and apply, then restart the PON line or reboot. The stock policy remains off
by default. Use the matching AN7583/device profile, not the HG5382A profile
from the unrelated AN7581 report.

No local firmware compilation or stable-O5 validation has been performed.
The fixes remove observed defects; whether the OLT continues to Deactivate
must be verified after deployment, with fresh OMCI/PLOAM and service counters.
Captured Deactivate cycles also occur without slow mapping writes, so the
flow-update refinement is not an established fix for the remaining rejection.

本分支针对实测的 O5 后控制通路阻塞：保留全部旧 GEM/T-CONT 硬件路径的更新
改为 RCU 延后释放旧流表，不再同步等待；删除或改绑旧路径仍等待加速宽限期，
不直接删除硬件回收前的同步保护。需要重新编译内核/固件及
固定版本的 PON feeds，单独升级 xPON 包不会包含这处底层以太网驱动修复。
HWTC 场景仍需显式开启 BWmap 原厂兼容模式。本分支尚未证明稳定 O5；
必须刷入后确认没有撤销循环、业务映射有效，而不只看瞬间显示 O5。
当前固定驱动 r75 / 前端 r15：借鉴外部实现完善 IRQ 锁顺序、MD32 暖启动和
AES 超时诊断，不替换状态机或跨模式复制模拟校准；userspace 固定版本不变。

## ⚠️ Disclaimer

PonWrt is an open-source firmware project intended for research and development.

Flashing firmware or modifying PON-related settings involves risks and may result in boot failure, loss of configuration or device-specific data, or failure to register with the PON network. Please back up the original firmware and device data before making any changes.

Users are responsible for ensuring that their use complies with applicable laws, regulations, and network operator requirements. Do not use this project for unauthorized network access, impersonating or cloning another device's identity, or interfering with operator networks.

The authors and contributors are not responsible for device damage, network service disruption, or other consequences resulting from flashing, configuring, or using this project.

## Supported devices

| SoC | Device | Profile | Stock calibration / identity data |
| --- | --- | --- | --- |
| AN7581 | FiberHome HG5382A | `fiberhome_hg5382a` | `factory` |
| AN7581 | FiberHome HG5585F CT | `fiberhome_hg5585f-ct` | `factory` |
| AN7581 | FiberHome HG5585F CU | `fiberhome_hg5585f-cu` | `factory` |
| AN7581 | Gemtek XG2010G | `gemtek_xg2010g` | `dsd` |
| AN7581 | Nokia XG-040G-MD UBI | `nokia_xg-040g-md-ubi` | `bosa`, `ri` |
| AN7581 | Nokia XG-040G-TF UBI | `nokia_xg-040g-tf-ubi` | `bosa`, `ri` |
| AN7581 | UnionMan UNG00A | `unionman_ung00a` | `reservearea` |
| AN7581 | ZNXT ZN504XG-D | `znxt_zn504xg-d` | `reservearea` |
| AN7581 | ZNXT ZN515XG-D | `znxt_zn515xg-d` | `reservearea` |
| AN7581 | H3C HM2004-DU | `h3c_hm2004-du` | `factory` |
| AN7583 | Nokia XG-040G-MF | `nokia_xg-040g-mf`, `nokia_xg-040g-mf-ubi` | `bosa`, `ri` |

## Build

```sh
#  Install the required toolchain and libraries for building
sudo apt update -y
sudo apt full-upgrade -y
sudo apt install -y ack antlr3 asciidoc autoconf automake autopoint binutils bison build-essential \
  bzip2 ccache clang cmake cpio curl device-tree-compiler ecj fastjar flex gawk gettext gcc-multilib \
  g++-multilib git gnutls-dev gperf haveged help2man intltool lib32gcc-s1 libc6-dev-i386 libelf-dev \
  libglib2.0-dev libgmp3-dev libltdl-dev libmpc-dev libmpfr-dev libncurses-dev libpython3-dev \
  libreadline-dev libssl-dev libtool libyaml-dev libz-dev lld llvm lrzsz mkisofs msmtp nano \
  ninja-build p7zip p7zip-full patch pkgconf python3 python3-pip python3-ply python3-docutils \
  python3-pyelftools qemu-utils re2c rsync scons squashfs-tools subversion swig texinfo uglifyjs \
  upx-ucl unzip vim wget xmlto xxd zlib1g-dev zstd

#  Or use the ImmortalWrt one-click script to install dependencies
sudo bash -c 'bash <(curl -s https://build-scripts.immortalwrt.org/init_build_environment.sh)'


#  Clone the source code
git clone --branch rayniel/hwtc-control-latency https://github.com/rayniel/ponwrt.git
cd ponwrt


#  Update and install feeds (package sources)
./scripts/feeds update -a
./scripts/feeds install -a


#  Select a configuration, using 7581 as an example
cp configs/an7581.config .config
#  If the target is AN7583, use the following line instead:
cp configs/an7583.config .config



#  Start building
# -j$(nproc) means compiling in parallel with all CPU cores to speed things up
make defconfig
make -j$(nproc)
```

Images are written to `bin/targets/airoha/an7581/` or `bin/targets/airoha/an7583/`.

## Install

Use [AN758x-Stock2UBI](https://github.com/pbs05/an758x-stock2ubi) to back up the stock flash and install the UBI layout. Boot images and Web recovery are provided by [AN758x U-Boot](https://github.com/pbs05/uboot-an758x).

After installing PonWrt, restore the stock calibration and identity data through U-Boot Web or **Network → PON → Configuration → PON board data** in LuCI. Convert FiberHome `factory` backups with [FiberHome Factory](https://github.com/pbs05/fiberhome-factory) first. Restore converted FiberHome data, `reservearea`, or `dsd` backups to the PonWrt `factory` volume. Nokia `bosa` and `ri` backups use volumes with the same names.
