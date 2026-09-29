# PonWrt

English | [简体中文](README_zh.md)

PonWrt is based on [ImmortalWrt](https://github.com/immortalwrt/immortalwrt) and adds support for Airoha AN7581 and AN7583 PON devices.

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
git clone https://github.com/pbs05/ponwrt.git
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
