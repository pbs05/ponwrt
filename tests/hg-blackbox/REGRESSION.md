# QoS 路径对照与故障定位

入口：[hg-qos-regression.py](hg-qos-regression.py)。先完成
[业务身份验收](SCENARIOS.md)，需要区分软件路径、PPE 与配置问题时使用本套。
配置准备和输出路径见 [README](README.md)。

## 拓扑和规则

[示例配置](configs/regression.example.json) 为 Win 经 HG 到 fnOS，指定不同的
HG 输入/输出物理口。默认三档依次为 DSCP `7/3/8`、内部优先级 `4/2/0`；
目的 UDP 端口为 `45101～45103`，须空闲。

路由测试 VIP 必须是 `198.19.0.0/16` 内未使用地址，不能与设备地址相同。
Win 去往该 VIP 的测试报文须经 HG；脚本不会替 Win 配置路由。只有指定端点和
三个测试端口才被临时 DNAT/SNAT，原有生产分类和持久配置保持原样。

HG 需要 nft、conntrack 和可信 root SSH；fnOS 接收程序无需 root/sudo。
Go 接收器在执行机交叉编译为 Linux amd64，C# 发包器在 Win PowerShell 编译。

## 用例矩阵

| 用例 | 测试设置 | 接收预期 |
| --- | --- | --- |
| production-bridge | 不加分类规则，直接发往 fnOS | 配置的 fnOS 整体档位，默认 DSCP 8 |
| bridge-slow-control | 仅测试流 notrack，并作三档临时分类 | DSCP 7/3/8 |
| bridge-auto | 同一桥接路径，允许生产 flowtable/PPE | DSCP 7/3/8 |
| routed-software | VIP 的 NAT，专用软件 flowtable | DSCP 7/3/8；观察到测试 PPE 绑定则报环境错误 |
| routed-auto | 同一路由路径，使用生产加速策略 | DSCP 7/3/8，另记录是否观察到 PPE 绑定 |
| congestion-0/1 | 路由加速路径，两轮端口/档位轮换 | 普通、代理相对后台得到保护 |

默认低流量主测每档约 4 Mbps、1.5 秒；此前预热尝试建立加速。拥塞主测每档
请求出口速率的 45%、持续 2.5 秒。每个用例换源端口，避免前一用例尚未老化的
PPE 元组串入；接收器只计入本轮随机标识的报文。

桥接临时分类优先级是 `-205`，位于现行生产分类 `-210` 和 fw4 建表 `-200`
之间；inet 测试分类为 `-150`。运行前检查 nft JSON 中的实际数字顺序。
分类晚于建表或测试覆盖先于生产时报 ERROR，不把环境错误算成固件 DSCP 失败。

## 命令

```sh
python3 tests/hg-blackbox/hg-qos-regression.py --dry-run
python3 tests/hg-blackbox/hg-qos-regression.py
python3 tests/hg-blackbox/hg-qos-regression.py --congestion
```

默认 Windows UDP batch 为 1；`--udp-batch 1..32` 可选择发送端 UDP 分段批次，
每个实际数据报仍为 1400 字节。高流量发包使用 CPU 忙等来减小定时误差。

### 单独选择的 TC 出口限速诊断

```sh
python3 tests/hg-blackbox/hg-qos-regression.py --congestion --only-congestion --egress-cap-mbps 400
```

此模式要求输出端原本为 `noqueue`，HG 具备 tc、sch_tbf 和相应支持。临时 TBF
请求限定该出口，**会影响共享出口上的现有业务**；每轮通常覆盖预热尾部和主测，
发包结束立即删除。另有 15 秒独立恢复定时器，只删除本轮 handle。

这只是负载不足时的对照工具，不是生产 QoS 配置，也不作为新固件 SDK 硬件调度
的验收依据。`tc` 接受命令不证明 PPE 流已受限；必须看实际收发速率。报告分别
记录物理速率和请求上限，不能把 400 Mbps 结果写成自然 1G 出口竞争结果。

## 判定

接收端报文才是 DSCP 断言的依据。低流量主测每条流至少 100 个收发样本，任何
实际错误标记均 FAIL，丢包超过 1% 均 FAIL；缺少 TOS 或样本不足为 INCONCLUSIVE。
这套路径对照主要断言稳定阶段；冷启动、TCP 与反向标记由业务身份套件检查。

拥塞判定另要求：输入快于输出、输出速率符合配置、三条流均有测试 PPE 绑定、
每档实际发速处于配置出口速率的 36%～48%、总发送超过净荷容量的 115%、
总接收处于容量的 90%～107%、DSCP 正确且接收端无已知 UDP/套接字丢包。
否则结果为 INCONCLUSIVE。共享主机计数可能包含其他业务，会保守地使结果无法判定。

有效测量下，普通交付率至少 95%、代理至少 90%，二者都比后台高至少 20 个
百分点才通过。两条高档流各自低于出口一半，因此该套件不单独证明普通高于
代理；相邻两档竞争由业务身份套件完成。

`/sys/kernel/debug/ppe/bind` 的元组匹配仅为加速旁证。如果新固件改变该接口或
文本格式，需要更新辅助识别；缺少绑定证据时不能宣称已完成硬件拥塞验收。
低流量标记通过本身也不证明该流已卸载。

## 清理与边界

使用唯一 nft 表名、HG 互斥锁和每用例不同元组。正常结束或可处理终止信号时
删除本轮表与精确测试连接，停止有限寿命收发器并删除临时程序。HG 另有
240 秒 watchdog；执行端被强杀或失联后，按报告检查其清理结果和临时文件。
不清空系统连接表，也不通过重启网络清理。

覆盖 IPv4 UDP、Win→HG→fnOS 正向和配置的一个出口；不直接覆盖反向、TCP、
IPv6、VLAN/PCP 矩阵、PON、IPTV 或 AP/WMM。标记、硬件绑定、内部队列与最终
调度是不同证据，报告不会仅凭表项 DSCP 判断端到端成功。
本次迁入未执行这套脚本，历史失败也未被改成通过基线。

## 固件运行策略前置检查

两套脚本均记录 `airoha-qosctl get` 的模式、完整 DSCP 表和默认 priority 表。
新版示例包含 `qos_device: "lan1"` 与 `expected_qos`，要求运行在 DSCP 模式、
映射 7→4、3→2、8→0 且未报告 broken。配置不符或状态不可读时，在发包前
报 ERROR；不会根据实际结果改写测试期望。
旧配置可省略 `expected_qos`，此时记录状态而不强制新接口，缺失状态明确为
unknown。抓包通过仍不能证明内部 skb priority 或精确硬件队列；回归规则中的
`meta priority` 是注入的测试输入，并非对驱动改写该字段的要求。
