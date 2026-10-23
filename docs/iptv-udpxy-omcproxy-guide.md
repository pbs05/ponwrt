# XG-040G-MD IPTV：udpxy + omcproxy 配置指南

> **本文档已随「组播 VLAN 端口关闭 MAC 学习」修复移植到 PonWrt 树。**
> 差异说明：第 1 节里的「一键修补工具（`scripts/iptv/`）」指的是 openwrt_XGPON 树中的
> 远程体检/安装脚本，PonWrt 树里没有这份脚本；PonWrt 已把该修复直接编进固件
> （`target/linux/airoha/an7581/base-files` 下的 `iptv-stb-fix`、`iptv-stb-watchdog`、
> `init.d/iptv-stb`、`hotplug.d/iface/99-iptv-mc-nolearn`，以及 `luci-app-iptv` 的
> `iptv-apply` 补丁），刷机即生效，无需再手工打补丁。其余章节（udpxy/omcproxy/防火墙/
> 排查）照常适用。

> 目标：局域网内任意设备（电脑/VLC/手机/盒子）观看运营商 IPTV
> - **单播化**：`rtp://239.0.0.x:5140` 组播流 → `http://192.168.33.254:4022/udp/239.0.0.x:5140`（VLC 填 http 地址）
> - **组播直连**：`rtp://239.0.0.x:5140`（VLC 直接填 rtp 地址，走 omcproxy 转发）
> - 本文档中的命令都在路由器 SSH（root@192.168.33.254）上执行

---

## 0. 拓扑原理

```
                         ┌────────── 路由器 ──────────┐
 运营商OLT/XGPON(pon0)    │                          │
   │ VLAN4075 (组播)      │ br-iptv (桥: lan2+ct-iptv-mc)  ← 组播源入口
   ├─ ct-iptv-mc@pon0 ────┤    IP: 172.16.200.1/24     │
   │                      │    ↑ omcproxy (IGMP代理)    │
   │ VLAN3300 (业务)       │    ↓ udpxy :4022           │
   ├─ ct-iptv-svc@pon0 ───┤                          │
   │                      │    br-lan (192.168.33.254) │──→ LAN设备
   └── pon0.100(PPPoE) ───┘                          │    VLC 播放
```

- **ct-iptv-mc**：承载组播的 VLAN（本例 4075），桥进 `br-iptv`
- **udpxy**：HTTP 单播网关。收到 `http://路由IP:4022/udp/<组>:<端口>` 请求后，在 br-iptv 上加入组播并把流改发为普通 HTTP 单播给请求者 —— **最稳定，推荐**
- **omcproxy**：IGMP 代理，把 br-lan 上客户端的组播加入请求转发到 br-iptv 侧，并把组播数据从 br-iptv 转发到 br-lan —— 用于 VLC 直填 `rtp://` 地址
- br-iptv 必须有一个 IP（`172.16.200.1/24`），否则 IGMP 加入无法发出

## 1. 前置：确认 IPTV 桥接（ImmortalWrt「网络→接口→添加 IPTV」向导已建）

如果 `/etc/config/network` 里没有以下内容，先建（数值按你运营商实际 VLAN）：

```
# 组播 VLAN 4075
config device
	option name 'ct-iptv-mc'
	option type '8021q'
	option ifname 'pon0'
	option vid '4075'
# 业务 VLAN 3300
config device
	option name 'ct-iptv-svc'
	option type '8021q'
	option ifname 'pon0'
	option vid '3300'
# 桥：lan2 + 两个 VLAN
config device
	option name 'br-iptv'
	option type 'bridge'
	option igmp_snooping '0'
	list ports 'lan2'
	list ports 'ct-iptv-svc'
	list ports 'ct-iptv-mc'
# 接口（无路由用途，仅承载组播）
config interface 'luci_iptv'
	option proto 'static'
	option device 'br-iptv'
	option ipaddr '172.16.200.1'
	option netmask '255.255.255.0'
	option delegate '0'
```

> `igmp_snooping '0'` 很关键：关闭后组播在桥内全端口广播，udpxy/omcproxy 一定能收到。

### ★★ 必做：组播 VLAN 端口必须关闭 MAC 学习（否则机顶盒报 2001 网络不可达）

`br-iptv` 把「业务 VLAN(3300)」和「组播 VLAN(4075)」桥进同一个域后，会踩一个**隐蔽的
网桥 MAC 学习陷阱**：

- 运营商 IPTV 组播流的**源 MAC** 和 IPTV**网关/EPG 的 MAC 是同一台设备**
  （本线路实测 `aa:bb:cc:00:11:22`，它同时是 100.64.0.1 网关的 MAC）。
- 该 MAC 只在组播 VLAN 上有流量，于是网桥把它学在 `ct-iptv-mc`(4075) 端口上：
  `bridge fdb show br br-iptv` → `aa:bb:cc:00:11:22 dev ct-iptv-mc`。
- 结果：机顶盒发往网关的**所有单播**（DHCP 续租、DNS、EPG/HTTP、鉴权）都被只转发到 4075，
  业务 VLAN 3300 一个包都收不到 → 盒子 DNS/EPG 全部超时 → **错误 2001「检测到网络不可达」**。
  （广播/组播因泛洪照常能通，所以盒子能拿到 IP，很具迷惑性。）

修复（本仓库已在固件中固化，见下）：

```sh
bridge link set dev ct-iptv-mc learning off        # 关闭组播 VLAN 端口的 MAC 学习
bridge fdb del aa:bb:cc:00:11:22 dev ct-iptv-mc   # 清掉学错的表项（30s 内自动老化也行）
# 验证：网关 MAC 应学在 ct-iptv-svc 上
bridge fdb show br br-iptv | grep -v '^33:33\|^01:00:5e'
#   aa:bb:cc:00:11:22 dev ct-iptv-svc   ← 正确
```

> ⚠️ netifd **不支持**用 UCI 给网桥端口设 `learning`（实测在 device 段写
> `option learning '0'`，`/etc/init.d/network reload` 后仍是 `learning on`，属无效配置）。
> 本仓库的做法（已随固件打包进 xg-040g 的 base-files）：
> - `/etc/hotplug.d/iface/99-iptv-mc-nolearn`：`luci_iptv`/`lan` 接口 ifup 时自动应用；
> - `/etc/init.d/iptv-stb`（S95，已 enable）：开机兜底，防止网桥端口重建后学习被重置。
>
> 判断是否踩坑的最快命令：`bridge fdb show br br-iptv`，看网关 MAC 是否落在 `ct-iptv-mc` 上。

**替代方案（更"标准"，但要动更多配置）**：把组播 VLAN 从给盒子的桥里拿出去——
`br-iptv` 只留 `lan2 + ct-iptv-svc`(3300)，`ct-iptv-mc`(4075) 单独成接口给路由器自己用，
再由 `omcproxy` 做 IGMP 代理把组播从 4075 侧转发到 `br-iptv`（等价于光猫内部的组播转发行为）。
好处是 mac 学习陷阱从结构上不存在、只有盒子点播的组才下发到 LAN2；
代价是盒子看直播依赖路由器上的 omcproxy 常驻正常。
当前采用的 `learning off` 方案更简单，代价是 LAN2 会收到全部组播（OLT 本来就把所有组都灌给 ONU，
所以并不增加上行流量）。

### 一键修补工具（`scripts/iptv/`）

本仓库把这套修复做成了可复用工具，**在开发机本地运行、远程修补软路由**：

```sh
cd scripts/iptv
./apply-iptv-fix.sh --check  <路由器IP>     # 体检诊断（只读）
./apply-iptv-fix.sh --apply  <路由器IP>     # 安装修复（自动探测网桥/组播VLAN端口）
./apply-iptv-fix.sh --rollback <路由器IP>   # 卸载
./apply-iptv-fix.sh --export-source-tree    # 写进固件源码树（重编译刷机后自带）
```

安装后有**四个触发点**互为保险：

| # | 位置 | 覆盖场景 |
|---|---|---|
| 1 | `/etc/hotplug.d/iface/99-iptv-mc-nolearn` | 接口 ifup（开机、ifdown/ifup） |
| 2 | `/etc/init.d/iptv-stb`（S95，procd） | 开机立即应用 + 拉起守护进程 |
| 3 | `/usr/libexec/iptv-apply` 补丁 | 该包的 `reload_runtime` 之后自动补一刀 |
| 4 | `/usr/libexec/iptv-stb-watchdog` | 常驻轮询兜底（见下方"已知盲区"） |

实现只有一份：`/usr/libexec/iptv-stb-fix`，参数在 `/etc/iptv-stb-fix.conf`。

> **已知盲区（实测）**：netifd 的 `ubus call network reload` / `/etc/init.d/network reload`
> **不会**发出 iface hotplug 事件（只有显式 `ifdown/ifup` 才发），却会重建网桥端口并把
> `learning` 重置为 `on`。因此必须靠触发点 4 的轮询（默认 15s）兜住；轮询还会顺手清掉
> reload 瞬间学进组播口的残留单播表项。
>
> **与本项目 `iptv-apply` 生成的 nft 规则的关系**：该规则集本意就是让盒子单播只走业务 VLAN
> （`iifname lan2 oifname ct-iptv-svc accept`），并把 `lan2 → ct-iptv-mc` 的非 IGMP 流量
> `drop`（`block-stb-data-to-igmp-vlan`）。MAC 学习一旦学错端口，盒子的单播就不再泛洪、
> 直接被送去组播口，于是撞上这条规则被丢弃 —— 两者是同一个故障的两层，修复互补、不冲突。

## 2. 配置 udpxy（HTTP 单播化）

```sh
uci set udpxy.@udpxy[0].disabled=0
uci set udpxy.@udpxy[0].bind=192.168.33.254     # 监听地址（LAN IP）
uci set udpxy.@udpxy[0].port=4022               # 服务端口
uci set udpxy.@udpxy[0].source=172.16.200.1     # ★组播源接口的IP（br-iptv），必须是有IP的接口
uci set udpxy.@udpxy[0].status=1
uci commit udpxy
/etc/init.d/udpxy enable
/etc/init.d/udpxy restart
```

验证：
```sh
netstat -tln | grep 4022                                  # 应监听 192.168.33.254:4022
ps w | grep udpxy                                          # 应见 -a 192.168.33.254 -p 4022 -m 172.16.200.1
curl -s http://192.168.33.254:4022/status | head           # 状态页
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" -m 8 \
     "http://127.0.0.1:4022/udp/239.0.0.11:5140"           # 期望 200 和持续增长的字节数
```

> ⚠️ udpxy 新版 `-m` 只认**接口 IP**（不是接口名）；接口无 IP 会报 `Invalid multicast address`。

## 3. 配置 omcproxy（IGMP 代理，供组播直连）

```sh
# 清掉默认配置（默认把 wan/wan6 当上行，不对）
uci delete omcproxy.@proxy[0] 2>/dev/null
uci add omcproxy proxy
uci set omcproxy.@proxy[0].scope=global
uci set omcproxy.@proxy[0].uplink=luci_iptv     # 上行=组播入口接口（luci_iptv→br-iptv）
uci add_list omcproxy.@proxy[0].downlink=lan    # 下行=局域网接口
uci commit omcproxy
/etc/init.d/omcproxy enable
/etc/init.d/omcproxy restart
```

验证：
```sh
ps w | grep omcproxy      # 应见 omcproxy br-iptv br-lan scope=global
cat /proc/net/ip_mr_vif   # 应有 vif0=br-iptv（BytesIn 增长）
```

## 4. 防火墙：给 IPTV 域加 zone

```sh
uci set firewall.iptv=zone
uci set firewall.iptv.name=iptv
uci set firewall.iptv.network=luci_iptv
uci set firewall.iptv.input=ACCEPT
uci set firewall.iptv.output=ACCEPT
uci set firewall.iptv.forward=ACCEPT
uci set firewall.iptv.family=ipv4
uci set firewall.iptv2lan=forwarding
uci set firewall.iptv2lan.src=iptv
uci set firewall.iptv2lan.dest=lan
uci commit firewall
/etc/init.d/firewall reload
```

> 说明：br-iptv（含 lan2）单独成 zone，组播数据允许流向 lan zone；不开 lan→iptv，IGMP 由 omcproxy 代为代理。若 lan2 上插了不信任设备，可把 input 改 REJECT（udpxy 收组播走的是本地已加入的组播套接字，通常不受 input REJECT 影响，如异常再放开）。

## 5. VLC 播放

| 方式 | VLC 网络串流地址 | 适用 |
|---|---|---|
| 单播（udpxy，推荐） | `http://192.168.33.254:4022/udp/239.0.0.11:5140` | 电脑/手机/盒子全兼容，可快进缓存 |
| 组播直连（omcproxy） | `rtp://239.0.0.11:5140` | 局域网有线设备；无线 AP 若开启 IGMP snooping 需正常发查询 |

**VLC 打开方式**：媒体 → 打开网络串流 → 粘贴地址 → 播放。
把常用频道做成播放列表最方便，见同目录 `docs/iptv-playlist.m3u`（把名字改成你的频道名）。
换台只要换组播地址中的 `239.0.0.xx` 和端口即可（运营商频道通常同为 `:5140`）。

## 6. 故障排查

| 现象 | 检查 |
|---|---|
| udpxy 拉流 0 字节 | ① 先确认组播真的在：`ip maddr`不可用时在路由器跑 `tcpdump -i br-iptv udp port 5140` 看是否有包；② 组播源接口 IP 是否配置；③ 桥的 `igmp_snooping` 是否 0 |
| 直连 rtp:// 黑屏 | 用 udpxy http 地址代替（更稳）；无线设备先试有线；检查 AP 的 IGMP snooping/querier |
| 重启后失效 | 确认 `S50udpxy`/`S99omcproxy` 存在（`ls /etc/rc.d/`）；检查 br-iptv IP 是否还在（`ip addr show br-iptv`） |
| 某些频道没有 | 运营商权限/按需下发；先用 tcpdump 确认该组播地址有数据再排查其他 |
| **机顶盒报 2001「检测到网络不可达」**（LAN2 直连盒子） | ① `bridge fdb show br br-iptv` 看网关 MAC 是否被学在 `ct-iptv-mc` 上 → 是则执行 §1 的 `learning off` 修复；② 在 `ct-iptv-svc` 上抓包确认盒子的 DNS/EPG 单播有没有出去：`tcpdump -i ct-iptv-svc -n -e ether host <盒子MAC>`；③ 盒子能拿到 100.64.x 的 IP 只说明广播通，**不代表单播通**（本故障正是如此） |
| 盒子 IP/掩码显示 255.255.255.0、网关 100.64.0.1「看着不同网段」 | 盒子 UI 显示的掩码不准（实测盒子真实掩码是 /20，网关 100.64.0.1 与 100.64.0.100 同网段），不用管；盒子上抓包能看到它正常 ARP 到网关并收到应答 |
| 升级固件后 | `/etc/config` 保留则全部自动恢复；重置则按本文 1–4 节重做 |

## 7. 本线路实测事实（示例数据已脱敏）

- 设备：`nokia,xg-040g-md-ubi`（Nokia XG-040G-MD），OpenWrt/ImmortalWrt 6.18 `airoha/an7581`，
  路由器 LAN `192.168.33.254`，上网 `pon0.100` PPPoE（拿到的地址是 `100.64.x.x` 大内网）。
- **OLT 通过 OMCI 下发的业务 GEM/VLAN**：`ponctl --device pon0 data-path show`
  → `count=5`：`100`(上网) / `3300`(IPTV 业务) / `3500` / `4040` / `65535`(OMCI 管理)。
  组播 VLAN `4075` 不在该列表里，但组播流确实从 4075 送来（源 `198.51.100.x` → `239.0.0.x:5140`）。
- IPTV 侧关键 MAC：
  - `aa:bb:cc:00:11:22` = **网关 100.64.0.1 + IPTV 组播源 + EPG 出口**（同一个 MAC，就是它引发学习陷阱）
  - `aa:bb:cc:00:11:33` = 业务 VLAN 上另一台运营商设备
  - 机顶盒 MAC `aa:bb:cc:00:11:44`，DHCP 拿到 `100.64.0.100`，DHCP 服务器就是 `100.64.0.1`。
- 机顶盒信息页显示的「掩码 255.255.255.0」不准：由它 `100.64.15.255:2103` 的广播地址反推，
  盒子真实掩码是 **/20**，所以网关 `100.64.0.1` 与它同网段，能正常 ARP，不用管这个显示。
- 机顶盒启动时解析 `ntp.<省份>.chinaunicom.com`、`<省份>.eds.169ol.com`（联通 EPG 域名）等。

## 8. 本次实际配置参考（2026-09 生效值）

- udpxy：`bind 192.168.33.254` `port 4022` `source 172.16.200.1`（br-iptv 的 IP）
- omcproxy：`uplink luci_iptv`（→br-iptv），`downlink lan`（→br-lan）
- 实测：`239.0.0.11:5140` 与 `239.0.0.15:5140` 经 http 拉流均 **200 / ~13MB/12s**；组播经 br-lan 拉流 **10.9MB/10s**

## 9. 「备份配置 → 恢复到同款新机器」注意事项

OpenWrt 的备份（LuCI 备份/恢复、`sysupgrade -b`）**只打包 `/etc/config/*` 加一份内置 keep 白名单**，
`/etc/hotplug.d/`、`/etc/init.d/` 里的自定义脚本**默认不在里面**。也就是说：

- ❌ 只恢复默认备份 → 恢复了会触发该故障的桥接拓扑（`/etc/config/network`），却没带修复脚本
  → 新机器上机顶盒照样报 2001。
- ✅ 本仓库已修好这一点：`/etc/sysupgrade.conf` 与
  `base-files/lib/upgrade/keep.d/iptv-stb` 都把以下条目纳入备份/保留清单：
  `/etc/sysupgrade.conf`、`/etc/hotplug.d/iface/99-iptv-mc-nolearn`、`/etc/init.d/iptv-stb`、
  `/etc/rc.d/S95iptv-stb`、`/etc/uci-defaults/99-iptv-stb-enable`。
  实测 `sysupgrade -b` 出来的包里这些文件（含 `S95iptv-stb` 软链接）都在，权限也是可执行。

两种恢复路径都覆盖了：

| 新机器固件 | 结果 |
|---|---|
| 用本仓库构建的固件（含上述 base-files + keep.d） | 修复随镜像自带，**不恢复备份也不会复现** |
| 任何其它固件 + 恢复本文档所述备份 | 修复脚本随备份一起过去，恢复后重启即生效 |

**恢复后自检两条命令**：

```sh
bridge -d link show dev ct-iptv-mc | tr ',' '\n' | grep learning   # 期望 learning off
bridge fdb show br br-iptv | grep -v '^33:33\|^01:00:5e'           # 期望网关 MAC 在 ct-iptv-svc 上
```

**另一个坑（与 IPTV 无关但同样重要）**：`/etc/config/pon` 里存着 ONT 的注册身份
（`serial_number <你的 ONT 序列号>` + `loid <你的 LOID>`），它同样随备份过去。
所以**不要两台同 SN/LOID 的设备同时接在同一 PON 口上**，OLT 侧会打架（先下线旧机器再上新机器）。

> ⚠️ **换板型时不要用旧备份包**：早期版本的修复脚本带「板型守卫」
> （`case "$(cat /tmp/sysinfo/board_name)" in nokia,xg-040g-md*) ... *) exit 0 ;; esac`），
> 换到别的板型（实测 `fiberhome,hg5585f-cu`）上会**静默 `exit 0`**：补丁"跑过了"、
> 日志里什么都没有、问题照旧。现在这套工具已经堵掉这个坑：
> - `scripts/iptv/apply-iptv-fix.sh --apply <IP>` 装的是**不含板型守卫**的版本（按设备是否存在判断）；
> - `--check` 会识别出这种失效的旧补丁并明确告警；
> - `restore-iptv-fix.sh` 会读备份包里的守卫列表，与本机板型不匹配就直接拒绝恢复。
>
> 换机器/换板型的推荐顺序：`--apply <新IP>` → `--check <新IP>` →（需要留档时）`backup-iptv-fix.sh`。
