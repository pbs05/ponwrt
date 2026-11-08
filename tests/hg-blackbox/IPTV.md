# 真实 IPTV / rtp2httpd 黑盒回归

## PSE 耗尽与历史换台压力回归

使用 --profile pse-stress，保持现有卸载、IFC、GRO、绑核和频率，默认重放 30 分钟。
固定模板来自 2026-10-17 的 195 秒故障负载：每 200 秒重复 23 个动作，换流重叠
1.3 秒，共 207 次请求。seconds=610 重放原来的三轮／69 次请求。
模板只保存频道槽位；实际地址、源和 URL 由本地配置提供，不依赖历史目录。

在原有配置中增加 pse_stress：seconds（610～3600，默认 1800）、
channels（按模板顺序的 10 个频道槽位），以及可选
expected_path（switch 或 replicate）。频道项沿用 channel 的
group/port/source/source_port/ingress_ttl/dscp/ecn，另有：

    "urls": {
      "hg": "http://192.0.2.1:5140/rtp/239.1.1.1:2000",
      "fnos": "http://192.0.2.3:5140/rtp/239.1.1.1:2000"
    }

10 个槽位顺序对应历史负载中的深圳、东方、江苏、湖南、四川、北京、浙江、广东、
CCTV4K、山东 UHD；这里只规定重放映射，不扫描或校验完整频道列表。选用无信号频道
会使媒体检查失败，不能用该次结果通过媒体完整性验收。

    python3 tests/hg-blackbox/hg-iptv-regression.py \
      --config tests/hg-blackbox/local/iptv.json --profile pse-stress

显式增加 --run 才开始连接和播放。预检必须看到两个真实 VIF、可用 IRQ 错误计数和
空闲的频道目录。HG 临时原生探针只读映射固定 AN7581 FE 页，每 50ms 读取实际 FQ
（+0x108）、ISR 和上下文寄存器；不写选择器、硬件策略或清除告警。
FQ 低于 1024 或 PSE_FQ_EMPTY 出现立即报错，并在私有
/root/hiptv_pse_TOKEN/fault.txt 保存首个故障和最多 64 KiB 的事务日志。
正常采样不写闪存。故障后不自动复位、重试负载或宣称能阻止失联。

组播事务日志仅由按需诊断镜像提供；普通固件不包含其常驻缓冲区和额外 MMIO
读取。缺少该文件时，脚本在 `diagnostics.multicast_transactions` 中把
`before`/`after` 记为 `false`，继续执行原有 FQ、错误中断、媒体和路径检查；
故障文件仍保存 FQ/ISR。临时诊断补丁见
[诊断扩展说明](https://github.com/pbs05/ponwrt/pull/84)。

采集器和 HTTP 客户端单次均有不超过 300 秒的远端期限，控制器分段续接；共享租约
失效时仅停止自有探针。HG/fnOS/接收端同时保存自有日志，失败清理不删除远端证据。
管理 ping 由 fnOS 本地独立执行。精确元组 BIND、实际出口及可选路径必须符合预期，
采样覆盖不足判 INCONCLUSIVE。退组后检查 FQ 恢复至初始中位数 ±256 页；同时仍需
运行已有 reliability 的原生 RTP 与物理端口检查，压力回归本身不替代这些测试。

前 3 个周期各采集 180 秒固定负载 CPU 和 ping P99，输出 performance.json。
两份固件均使用同样配置、模板和诊断。只有主动安排相同背景负载的测试才在本地配置
填写 background_controlled=true；默认不能自动断言背景受控。比较命令：

    python3 tests/hg-blackbox/helpers/hg_iptv_pse.py \
      --compare BASELINE/performance.json CANDIDATE/performance.json

配对中位数要求：媒体速率至少为基线 95%，整机 CPU 增量 ≤1 个百分点，
最忙核 ≤3 个百分点，ping P99 增量 ≤2 ms；配置、负载或背景控制不匹配为
INCONCLUSIVE。CPU/媒体速率衡量固定节目负载，不表示最大吞吐或功耗收益。

### 多物理出口资格验证

可在 pse_stress 中增加 receivers 列表，最多三个额外 Linux 接收端。每项包含
ssh、arch、address、interface、hg_port、ppe_bit，可选 known_hosts。示例：

    "receivers": [
      {"ssh":"iptv-peer","arch":"x86_64","address":"192.0.2.4",
       "interface":"eth1","hg_port":"lan3","ppe_bit":3}
    ]

接收端可为另一台 Linux 主机，或 fnOS 上独立接线且没有桥接回同一 LAN 的网口；
不能因测试临时把多根网线加入同一个软件桥。控制器不改接收机网络配置。
额外接收端地址、HG 物理出口和复制位必须各不相同，且不能同时列为非订阅观察口。
预检检查实际接口地址、MAC/FDB、桥从属及 carrier，不按接口名称推导复制位。

压力阶段后会运行“多口转发”和“多口+HG 本机”两个有界 RTP 阶段，核对组合 BIND
位图、本机副本位、各接收端 RTP/TTL/DS field/socket drops、自然退组和已配置的
非订阅口硬件计数。先退出 fnOS，其他接收端继续观察。保存实际链路速率：
不同速率、包含原生 2.5G 口的混合输出分别记录是否真实覆盖。
同一个拓扑不能替代全部拓扑；应分别准备普通 LAN 多口、不同速率和原生混合输出。
未配置 receivers 时明确为 NOT_CONFIGURED，不能据压力阶段通过就宣布多口验收完成。

## 保持现有策略的可靠性回归

新增 `--profile reliability`，不指定 profile 时仍是原有开关对照测试。
两种模式默认都只打印本地计划；显式 `--run` 才会连接设备、订阅和播放。

```sh
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --profile reliability
# 确认现网可进行播放测试后，增加 --run。
```

该 profile 不调用旧的 setup/mode/quiesce/restore，不暂停卸载、不清表，也不
修改 IFC、GRO、桥 snooping/querier、CPU 绑核或频率。设备必须已有可用配置。
**只读配置不等于零风险**：订阅仍会触发驱动学习和表项更新。2026-10-16 的一次
旧固件基线测试在首个 fnOS HTTP 阶段失去 HG 网络/SSH，由用户手动重启恢复；
当次未刷入新代码、未完成测量，不能给出性能或可靠性通过结论。此类故障后先
调查，不自动重启、重试负载或刷机。CPU/应用探针有本地期限，但内核或网络
硬故障时不能保证远程看门狗执行。

默认用例：

- HG / fnOS 两种部署，各运行 HTTP、原生 RTP、同一部署的 HTTP+RTP，三轮，
  预热 10 秒、测量 45 秒；下一轮反向排序。组合负载不加入另一个部署的接收 socket。
- HG / fnOS 各五次入组，默认首包必须在 1 秒以内，5 秒无有效 RTP 即停止接收。
  `IP_ADD_MEMBERSHIP` 完成到首个匹配 RTP 使用同一接收主机的单调时钟；随后
  完整接收 15 秒。入组等待不计为播放间断。
- 两端同时订阅，分别验证 HG 先退出及 fnOS 先退出；另一接收者继续观察至少
  10 秒。最后退出等待最多 90 秒，检查成员、正向 IPMR 路由及目标 BIND 消失，
  IFC 恢复 active。始终自然退组，不强制撤销其他订阅者。
- 所有对照用相同的轻量诊断、记录诊断时间段；测量期间不读取完整 PPE 表。
  原生 socket 报告实际 `SO_RCVBUF`、inode、预热与测量期 drops。探针溢出单列
  FAIL，链路丢包归因仍是 INCONCLUSIVE；rtp2httpd socket 新增 drops 单独判 FAIL。
- `path_checks` 使用已连接非订阅物理口的硬件计数；其他业务导致计数超预算是
  INCONCLUSIVE，不能用 BIND 位图宣称已排除洪泛。未配置物理端口时不覆盖洪泛。

可选配置（这些值均有范围校验）：

```json
"reliability": {
  "rounds": 3, "seconds": 45, "warmup": 10,
  "join_rounds": 5, "join_seconds": 15, "leave_seconds": 90,
  "first_timeout": 5, "max_join_seconds": 1
}
```

`background_channel` 可指定 `source/source_port/group/port/ingress_vlan`，用于
“已经被过滤的背景流 → 本地订阅”的专门资格验证。脚本检查无成员、IFC active、
丢弃计数增加，并保存测量窗口外的 PPE 快照。**这些还不能单独证明某个准确
流和 VID 的持续背景流**：UNB 的改写/VLAN字段不是有效绑定动作。
因此还需独立的被动入口观察文件 `evidence_file`，字段为上述准确元组加：

```json
{"boot_id":"HG 当前 boot UUID", "observed_at":1792140000,
 "seconds":5, "packets":1000, "unsubscribed":true, "filter_active":true}
```

必须是同次启动、最近 120 秒内、连续至少 5 秒/100 包且在未订阅和过滤开启时的
准确入口观测，不能手工猜填、拿全局 MIB 替代或关闭过滤来制造证据。没有此证据
时专门用例标 INCONCLUSIVE；仍可完成普通频道的入退组测试，并以
`background_verified=false` 明确区分。该输入不引入常驻程序或可选路由器包依赖。

临时会话按唯一目录和 token 持有测试锁，探针最长 300 秒；60 秒没有心跳，
各主机看门狗只终止该唯一路径的探针。协调端等待远端期限过去才释放锁。
正常收尾检查探针退出、配置/boot ID/绑核/GRO/桥设置和 WAN 是否保持，异常时
保留失败状态及证据，不覆盖外部配置。`--gro-ab` / `--paths-only` 不能与此模式混用。

## 接收完整性与调度回归补充

混合阶段先启动两端 RTP 观察器，等待确认入组及预热后才启动 HTTP 客户端。
观察窗口覆盖 HTTP 连接、预热和全部测量时间；用控制端保守时间界限校验覆盖，
不假设三台主机时钟同步。窗口不足为 INCONCLUSIVE，不能据此排除上游缺包。
纯性能阶段仍不启动附加 RTP 订阅，避免将观察器成本混进原本的转发路径。

原生探针新增只读 `diag` 命令：记录 boot ID、UDP/softnet/IRQ/softirq/内存计数，
并用 INET_DIAG 查询 UDP socket 容量和 drops，按 rtp2httpd 的 FD inode 关联。
HG 另记录单个 CDM2 RX CPU drop 寄存器，不清零计数。每阶段在开始、运行中两次、
结束取样；这部分有界观察开销包含在 CPU 测量内，各组执行相同观察。
实际 HG socket 接收上限需至少达到请求值的两倍，即 2 MiB 请求通常显示 4 MiB。
socket 新增 drops 判 FAIL；共享 UDP/CDM2 计数上升、socket 权限不足或诊断功能
不可用判 INCONCLUSIVE 并保留原始数据。上游源地址变化同样不能直接归罪于驱动。

资格回归可将 `timing.seconds` 设为 180、`rounds` 设为 3；不是必须逐频道测试。
RTP 位图现支持约 419 万个序号位置，使用 512 KiB 内存；超过范围仍显式报观察
能力不足。变更后必须重新 `--build-probes`，旧探针的源码摘要将被拒绝。
HG 专用内核配置包含 INET_DIAG/UDP_DIAG，只提供按需查询，不添加常驻监控或逐包规则。

入口：[hg-iptv-regression.py](hg-iptv-regression.py)。把 2026-10-15 的实测方法固化为
可重复用例：同一频道，rtp2httpd 分别运行在 HG 和下游 Linux NAS，比较组播卸载开关。
默认只输出本地计划，只有 `--run` 连接设备。它不依赖那次实测目录、临时跳板脚本或
homelab 仓库，也不保存密码。默认 10 个阶段，约 12–20 分钟，实际取决于退组等待。

## 覆盖内容

| 用例 | 内容与判定 |
|---|---|
| 八轮性能对照 | fnOS/HG × 软件/硬件开关各两轮；第二轮反向排序；记录整机/每核/进程 CPU、频率、接口计数、HTTP 码率 |
| 两轮本地与远端同时收流 | 两端 rtp2httpd 同时提供 HTTP，普通 UDP socket 同时检查 HG 本地交付和 fnOS 路由交付 |
| 视频完整性 | MPEG-TS 连续计数、重复、TEI、格式错误、读流中断与码率；不解码或保存视频 |
| 原始 RTP | 接收接口、源 IP/端口、组地址、TTL、完整 DS field；区分区间内缺失、重复和乱序；记录 socket overflow、SSRC 改变和容量上限 |
| 实际卸载证据 | 测量窗口内精确 `(iif,S,sport,G,dport)` 的 BIND 与出口位图覆盖至少 80% 采样点、CPU-copy delivered 增量；软件 TX 下降而物理 TX/媒体持续；只打开开关不能 PASS |
| `--gro-ab` 四轮附加诊断 | 软件/硬件分别搭配 PON `rx-gro-list` 开/关，用于重现软件聚合的 TTL 问题 |

HG 只运行本地 rtp2httpd 时，RTP 转 HTTP 必须经过应用，本次组播路由卸载不适用。
这一阶段的预期是没有目标组播 BIND；不会把没有 BIND 错判成硬件故障。

## fnOS 收流路径与无订阅出口检查

可在本地 JSON 中增加：

```json
"path_checks": {
  "nonmember_ports": ["lan2", "lan3"],
  "max_nonmember_tx_bytes": 65536,
  "leave_seconds": 45
}
```

列出的端口必须真实连接、属于被测 LAN 桥，且没有目标频道订阅。
接收端的物理口仍使用 `hg.port`。脚本通过 `ethtool -S PORT --groups eth-mac`
读取 **OctetsTransmittedOK / MulticastFramesXmittedOK** 硬件计数。DSA 普通
`ethtool -S` 输出中的 `tx_bytes` 是软件统计，不能用于证明硬件出口没有洪泛。
2026-10-16 修正前的路径专项测试误用了后者，旧报告的非订阅端口结论不能沿用。

端口总字节不超过原有的 64 KiB 预算才通过；超过预算判 INCONCLUSIVE，需区分
目标组泄漏与其他业务。硬件组播帧计数作为附加证据保留，不替代总字节判据，也
不以 BIND 位图代替物理出口测量。断链、计数器重置或缺少硬件计数也不能作为
隔离通过。预算覆盖整个阶段及自然退组等待，允许少量控制流量；它不证明单个
报文级别的零泄漏。视频完整性和 RTP 缺包阈值保持不变。

开启该检查后，每阶段在改变卸载开关之前等待目标成员、有效 IPMR 输出与 BIND
自然消失，避免用强制清表掩盖最后一个订阅者退出后的残留转发。

只验证 fnOS 架构可使用：

```sh
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --paths-only --run
```

该模式只运行 fnOS 软件/硬件两阶段。HTTP 播放与 fnOS 原生 RTP 序号观察重叠，
HG 不加入额外的应用接收 socket；同时核对精确的硬件复制位图、非订阅出口总 TX
预算与自然退组。不运行 CPU 对照，也不检查其他架构的吞吐。默认 45 秒视频窗口
之外，另保留原始 RTP 覆盖时间和最多 45 秒自然退组等待。不能和 `--gro-ab` 混用。

CPU 以整机所有核心为 100%；最忙核心和进程 CPU 以单核为 100%。不调整 CPU 频率、
governor 或后台业务。CPU 降幅只报告实测值，不要求每次都达到历史的 34%。
硬件阶段若后台流量让软件 TX 对照无法判断，会返回 INCONCLUSIVE，保留实际计数。

## 一次性准备

1. 两端 rtp2httpd、IPTV 数据入口、IGMPproxy、VLAN 和防火墙已经配置好。
   真实节目来自运营商 PON，脚本不向运营商发伪造的测试视频。
2. **组播后端必须已通过启动配置启用并冻结正确的入口/出口范围。** 脚本读取
   `airoha-mcastctl get` 验证 ifindex；未启用或上游选错时直接报执行环境错误。
   脚本不会写 UCI、自动重启或刷机。准备启用及最终关闭启动策略由操作者另行安排。
   当前 HG 的数据入口是 `iptv_mcast / hgi4000 / VID 4000`，不是 DHCP/FCC 用的
   `iptv / VID 3964`。接口名仍由本地配置决定，不写死在脚本里。
   还必须在 `/proc/net/ip_mr_vif` 中找到 `hg.ingress` 和 `hg.egress` 两个实际
   内核 VIF。预检将原始表保存为 `preflight-mroute-vifs.txt`；缺少任意一端时，
   在获取测试锁、部署探针或发起播放之前停止。仅有完整配置或运行中的进程
   不足以证明组播转发路径已建立，VIF 编号也不要求固定为 0 或 1。
3. NAS 的组播接口确实接在指定 HG 物理出口；脚本检查链路、桥从属关系和 NAS MAC
   在该端口的 FDB。复制位 `ppe_bit` 需按设备核对，不能由端口名称猜测。
4. 测试目标频道没有其他订阅者。控制器会在阶段之间等待退组，超时即停止，避免
   把残留订阅的混合路径当成 HG 本地单独处理。其他频道仍可能在运行，但本轮切换
   会影响整个已启用范围的组播硬件加速，期间软件路径仍须可用。
5. Linux 控制机：Python 3.9+、Go 1.21+、OpenSSH、原生 C 编译器、HG 的 AArch64
   musl 交叉编译器。**只编译临时探针，不编译固件或安装软件包。**
6. HG：root SSH、`ip`、`bridge`、`ethtool`、`jsonfilter`、`airoha-mcastctl` 和 debugfs。
   NAS/HTTP 接收器：Linux SSH、`ip`；原生探针无需 NAS 的 root/sudo 或 Python。

## 配置与构建

```sh
mkdir -p tests/hg-blackbox/local
cp tests/hg-blackbox/configs/iptv.example.json tests/hg-blackbox/local/iptv.json
```

编辑本地 JSON：

- `hg` / `fnos`：SSH 别名、架构、网口、rtp2httpd 实际二进制位置和 HTTP URL。
- `channel`：同一个真实频道的组地址、端口、源 IP/端口、期望入口 TTL/DSCP/ECN。
  示例 IP 属于文档地址，不能原样执行。期望值用于找错误，不会从本次错误输出反推。
- `receiver`：省略时使用 fnOS；也可指定另一台 Linux 主机的 `ssh/arch/address`。
  默认情况下 fnOS 架构的 HTTP 走本机，HG 架构的 HTTP 经过 LAN，报告会注明接收器。
- `binaries`：构建产物路径，相对 JSON 所在目录解析，适合放在被忽略的 `local/bin/`。
- 可选顶层 `ssh_config` 指定 OpenSSH 配置文件；每个主机可指定 `known_hosts`。
  跳板统一通过 OpenSSH 的 ProxyJump/ProxyCommand 管理，保留严格指纹检查。
- 若 SSH 用户无法读取播放器进程的 `/proc/PID/fd`，可在该主机项设置
  `diagnostics_command` 参数数组，例如
  `["sudo", "-n", "-u", "root", "/opt/iptv-probe", "diag"]`。
  管理员需事先部署与本次源码一致的只读探针并限定执行权限；脚本不配置 sudo、
  不保存密码、不自动提权。该命令仅替代 socket/内核计数诊断，不改变收流身份。
  带能力或不可转储的播放器进程，即使切到同一用户也可能无法读取其 fd。
  没有完整 socket 归属证据时仍返回 INCONCLUSIVE。
- `thresholds`：最低码率、最大读流/RTP 间隙、最小样本数和允许乱序比例。
  默认允许最多 0.1% 乱序，**不允许缺包/重复/错误 TTL**。频道源发生合法变化时，先
  核实并更新期望值，不要通过放宽 TTL 判定来掩盖回归。

```sh
# 不连接任何设备
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --dry-run --gro-ab

# 替换为实际构建树；只在控制机编译探针
STAGING_DIR=/path/to/ponwrt/staging_dir \
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --build-probes \
  --hg-cc /path/to/ponwrt/staging_dir/toolchain-aarch64_cortex-a53_gcc-14.4.0_musl/bin/aarch64-openwrt-linux-musl-gcc

# 准备条件满足后运行主回归
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --run

# 需要 GRO 开关诊断时
python3 tests/hg-blackbox/hg-iptv-regression.py \
  --config tests/hg-blackbox/local/iptv.json --run --gro-ab
```

NAS 为 AArch64 时，用 `--peer-cc` 指定对应交叉编译器。构建器及执行前检查 ELF64
架构、源码/二进制 SHA256 清单，避免上传错误架构或沿用过时探针。复制构建产物时
保留同名 `.build.json`，探针源码更新后重新构建。两端 rtp2httpd 的版本会归档，不自动升级任一端。

## 退出码、报告及当前已知问题

| 退出码 | 含义 |
|---|---|
| 0 | 已执行项目 PASS；计划/构建模式的 0 只代表本地操作成功 |
| 1 | FAIL：视频/报文语义、交付、重复、超过阈值的乱序或错误出口 |
| 2 | INCONCLUSIVE：样本/硬件证据不足、探针溢出、SSRC 改变等 |
| 3 | ERROR：运行前置条件、SSH/执行、控制器异常或恢复失败 |

CLI 参数/本地配置错误由 argparse 报错，发生在实机报告创建之前。每次执行写入
`logs/hg-blackbox/iptv/<时间_随机标识>/`：`REPORT.md`、`summary.json`、逐阶段判断、
CPU 原始 JSONL、HTTP/RTP 统计、序号跳变记录、BIND 快照、配置摘要、版本、原始运行
设置和恢复结果，以及本次所用源码副本。目录/文件只允许执行账户访问。

2026-10-15 的固件存在软件 GRO fraglist 成员 TTL 未递减的问题：HG 本地 TTL 为
124，而部分经过 HG 路由的 fnOS 报文仍为 124。主回归的 mixed-sw 就可发现；
`--gro-ab` 进一步对照关闭 GRO 后是否消失。**它始终算 FAIL，不设自动豁免。**
少量 RTP 乱序会保留数量和具体序号，不把三个跳变事件当作三次丢包。

后续 `940-07` 源码修复补齐 IPv4 TTL / IPv6 Hop Limit 传播；实现与本机验证见
[修复说明](https://github.com/pbs05/ponwrt/pull/74)。刷入修复版后仍须运行相同
的 mixed-sw / mixed-hw 和 `--gro-ab` 检查，不改变通过标准、不按固件版本自动豁免。
本机单元测试通过不代表该修复已经在 HG 实机验收。

普通 UDP socket 不证明上游线上完全无丢包；它检查测量区间内序号完整性。没有独立
上游参考时，FAIL 表示这条实测链路不满足期望，不能自动把根因归到 HG。
GRO 开关为 on 也不单独证明每一轮实际形成了 fraglist，分段机制仍有独立内核测试。

## 恢复机制

与其他黑盒回归共用 `/tmp/hg-qos-regression.lock`。先记录暂停状态、桥 snooping/
querier、物理 PON GRO 原值与八个持久配置文件 SHA256，再启动 HG 上的恢复守护脚本。
阶段切换只改变运行时开关，不重启服务，不添加 nft/tc 规则，不做混杂抓包。

正常完成、异常、Ctrl-C 都停止有时间上限的探针并恢复原状态。控制器每次采样更新
设备端单调时钟租约；失联超过 60 秒后，HG 守护进程尝试自动恢复。恢复与设置操作
通过目录锁串行化；先暂停后端再恢复 GRO/桥，确认后端没有故障才恢复原先启用状态。
只删除归本次 token 所有的锁和临时目录。持久配置若被外部修改只报告，不覆盖。

恢复失败返回 ERROR，并在报告中留下 HG 上的恢复命令、状态文件和锁，便于检查；
不会删掉唯一恢复材料。设备自身故障或不可写状态不能保证自动恢复。
测试结束的短暂 IGMP 成员保留会记录；不强行清空全网组播路由。

## 本地维护检查

以下不连接 HG/NAS，也不发送网络报文：

```sh
python3 -m unittest discover -s tests/hg-blackbox/unit -p test_hg_iptv.py -v
go test tests/hg-blackbox/helpers/hg_iptv_stream.go tests/hg-blackbox/helpers/hg_iptv_stream_test.go
```

包括历史 TTL 失败样本、TS 解析、乱序/丢失区分、精确绑定判定、CPU 口径、默认不执行
远程命令，以及在隔离文件模拟环境中执行真实恢复脚本的正常/失联/失败/所有者变化路径。
它们验证脚本自身；新固件仍须单独运行现网回归，不沿用旧固件 PASS。
