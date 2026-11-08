# IPv4 组播回归

如需用真实运营商频道比较 rtp2httpd 在 HG / fnOS 上的开销与卸载效果，使用
[真实 IPTV 回归](IPTV.md)。本文脚本继续负责可控上游发包、多接收端及隔离边界用例。

`hg-multicast-regression.py` 使用两个可订阅的 Linux 接收端、可选的隔离网络观察端，
以及一个可控的上游 Linux 发包端。它检查实际接收结果，并读取 HG 的绑定记录作为
卸载证据。脚本随 ponwrt 维护，不依赖 homelab 中的地址、凭据和辅助脚本。

本轮只开发脚本，尚未运行端点或 HG 实测。默认执行只打印计划；`--run` 才建立 SSH
连接、发送报文和临时入组。脚本不会启用组播后端或创建 HG 的 VLAN、防火墙、路由。

## 准备条件

- 执行机有 Python 3.9+、OpenSSH。发送/接收端均为 Linux，有 Python 3；使用 root SSH
  或已经配置好的 `sudo -n`，不在配置文件或命令行保存密码。
- HG 有 root SSH、`bridge`、IPMR 与已经配置的组播路由守护进程。硬件用例须事先通过
  开发固件的 UCI 启动配置启用并限定后端范围；后端未启用时脚本返回 INCONCLUSIVE。
- **发送端必须真的位于被测 HG 入接口的上游。** 当前后端要求 PON IPoE，因此需要
  可控的 PON/OLT 实验环境或等效的真实上游发包条件。不能从 HG 本机发包、改成 LAN
  入站或创建软件虚拟接口，然后将结果称为 PON 卸载通过。没有该条件时仍可开发、
  查看计划和审阅脚本，但不能完成当前硬件资格验证。
- 两个可订阅接收端分别直连原生 2.5G 与交换机口；路径不能绕过 HG。每个接收端使用
  独立的数据接口，SSH 管理可以走其他接口。`ppe_bit` 对应驱动复制位图中的物理出口，
  示例 13/4 仅是模板，须按实际接线核对。
- 配置组播查询器、源路由、桥 MDB 与必要放行规则，使普通软件转发可用。测试组须没有
  其他订阅者或静态 MDB；确认退组等待时间足以覆盖查询周期和成员删除。默认等待 10 秒
  只适用于支持及时退组的预备拓扑，普通 IGMP 计时器可能需要更长时间。
- 测试数据均使用 `239.0.0.0/8` 的空闲实验组。禁止以真实节目组运行本脚本，避免它与
  既有接收业务互相影响。发送端 MTU 须容纳指定负载；1400 字节 UDP 负载对应 1428 字节 IP。
- 必须独立确认两个测试组均能抵达 HG 的被测入口，包括未订阅的别名组。例如使用
  已确认的 OLT 实验转发配置或外部入口观察；若上游 IGMP 过滤已丢弃别名组，则其零接收
  只能说明整条路径未交付，不能据此认定 PPE 的组地址查找键正确。

接收探针同时使用 UDP socket 与 AF_PACKET：前者确认应用确实收到每个序号，后者
观察未订阅端口是否收到数据、VLAN 标签、重复和分片。抓包 socket 临时持有网卡混杂
引用，关闭后自动释放；不会执行 `ip link set promisc`。抓包发生在接收主机，接收端
GRO/硬件 VLAN 剥离可能改变所见表示；VLAN 辅助元数据会保留，严格帧级复核应使用
已确认无聚合的接收接口或外部镜像。脚本不修改任何网卡 offload 设置。

## 使用

```sh
cp tests/hg-blackbox/configs/multicast.example.json tests/hg-blackbox/local/multicast.json

# 编辑 SSH 别名、数据地址、接口、复制位及期望 VLAN；先只生成计划
python3 tests/hg-blackbox/hg-multicast-regression.py \
  --config tests/hg-blackbox/local/multicast.json

# 上述拓扑已经独立准备好后，执行实际回归
python3 tests/hg-blackbox/hg-multicast-regression.py \
  --config tests/hg-blackbox/local/multicast.json --run
```

保留严格 SSH 主机密钥检查；每个主机可提供 `known_hosts`，相对路径以配置文件所在
目录为基准。所有本地配置保存在被忽略的 `local/`。`require_hardware: false` 仅用于
软件转发对照，其结果不会验收 PPE。脚本不自动切换这个选项或设备状态。

`member: false` 的端点只观察泄漏，可接 IoT/Guest 或其他不应收到该流量的出口。
这验证的是指定入口到这些出口的交付隔离，**不能证明相同 `(S,G)` 从其他 VLAN/GEM
输入时不会误命中已有表项**，后者需要第二条独立入站路径。

## 用例和判定

| 阶段 | 预期接收 | 主要判定 |
| --- | --- | --- |
| `first-member` | 第一个接收端 | 未订阅端口不收到数据 |
| `both-members` | 两个接收端 | 每个序号各收到一次；复制位图与两个出口一致 |
| `ttl-one` | 均不接收 | 已建立流的 TTL=1 报文不被继续路由 |
| `first-left` | 第二个接收端 | 第一个出口退出复制；第二个持续收流 |
| `all-left` | 均不接收 | 数据不再转发，对应绑定撤销 |

每个阶段还发送一个与主组映射到相同组播 MAC 的未订阅组。两组使用独立的目的 IP，
共享源地址和端口；它不能因 MAC 别名被复制到主组订阅者。正常交付同时校验源地址、
UDP 端口、负载长度、DSCP、ECN、TTL 减一和可选 VLAN 标签。

报文含随机运行 token、阶段编号、单调序号；预热和测量使用不同阶段编号，判定不依赖
主机时钟同步。成员变化后的预热持续发流，避免仅因发送停止使旧表项老化而误判。
当前默认双流总 IP 速率约 9.14 Mbit/s，硬件学习速率不足时可在限定范围内调整；
上限为 40 Mbit/s，不应把该脚本当成吞吐压测。

缺少绑定、抓包丢包或达到观测上限返回 INCONCLUSIVE；确定的越权交付、重复、错误标记
或完整捕获条件下的交付缺失返回 FAIL。发送失败、SSH 错误或清理异常返回 ERROR。
非订阅端口的零接收依赖上述已确认拓扑；未接线、错误接口或观察端选错位置不能作为
隔离通过的证据。

报告在 `logs/hg-blackbox/multicast/<UTC时间>_<随机标识>/`，含 `REPORT.md`、
`summary.json`、逐阶段端点统计、HG 快照、配置和脚本副本。退出码 0/1/2/3 分别为
PASS/FAIL/INCONCLUSIVE/ERROR；计划模式的 0 仅表示计划生成。

脚本与既有 QoS 回归共用 HG 的 `/tmp/hg-qos-regression.lock`。正常结束、异常或中断
都会关闭探针 stdin，使 UDP 入组和 AF_PACKET 引用释放，然后核对所有者再移除锁。
探针还有 900 秒退出上限；SSH 失联或强制杀进程时清理可能无法确认，报告会提示检查。

## 尚未覆盖

当前脚本不是完整硬件验收：CPU 副本描述符/报头原始格式、不同入口/VID/GEM、IGMPv3 源过滤、查询器
过期、PON 重连、IPIP/PIM、IPv6、混合标签复制、老化/哈希复用/写失败、驱动卸载、
吞吐与 CPU 成本仍需独立用例。固定大 UDP 报文的收发通过也不能证明已形成 GRO，
所以不能单独作为 `940-06` 的内核分段行为验收。

所有报告固定保留 `hardware_qualification: incomplete` 和未覆盖清单，有限用例的
PASS 不会自动打开生产配置开关。

## HG 本机应用接收（2026-10-15）

新增 `helpers/hg_mcast_local.c`，用固件相同的 AArch64 musl 工具链编译为
`hg-mcast-local`。这是临时测试工具，不加入固件包，也不要求 HG 安装 Python。
把它显式放入 HG 的 `/tmp/hg-mcast-local` 后，在测试 JSON 增加：

```json
"local_probe": "/tmp/hg-mcast-local"
```

只有 `--run` 会启动它。探针用 UDP socket 入组，设置 `SO_BINDTODEVICE`、
`IP_PKTINFO`、`IP_RECVTTL`、`IP_RECVTOS` 和溢出计数；**不在 HG 上开启混杂抓包**，
避免把诊断保护触发成新的卸载阻断。关闭 stdin/SSH 或 900 秒超时会关闭 socket。

追加阶段：

| 阶段 | 远端 | HG 应用 |
| --- | --- | --- |
| `local-and-remote` | 两个端口均接收、各一次 | 同一序号一次，入口 TTL 与 DSCP/ECN |
| `local-ttl-one` | 均不接收 | TTL=1 的报文仍应本机交付 |
| `local-only` | 均不接收，撤销硬件复制 | 软件本机交付继续 |
| `local-left` | 均不接收 | socket 已退组 |

本机检查包括完整序号、重复、UDP 源端口/源 IP、入口 ifindex、目的组、长度、DSCP、
ECN 和**未经路由递减的 TTL**。缺失控制消息或源接口错误判 FAIL；探针接收队列溢出
判 INCONCLUSIVE。没有配置 `local_probe` 时，报告明确记录本机应用交付未覆盖。

每次快照还保存 `airoha-mcastctl get` 的启动策略及原因计数。应用正确收包不能独立证明
CPU 副本来自哪个硬件原因码；需结合绑定条目、`delivered` 增量及下一轮受控硬件观察。
这轮只运行探针序列化和判定逻辑的主机测试，没有连接 HG 或发送组播。
