# HG 黑盒回归

[LAN1 / HSGMII 观察](LAN1.md) 的入口为 `hg-lan1-regression.py`：默认只读，
检查 carrier、内部同步、物理错误及 LED 硬件控制；重启和负载须显式指定。
它不注入 PCS 故障，也不能单独证明硬件卸载吞吐或历史失联已修复。

[CPU 调度回归](CPU.md) 通过默认关闭的 `airoha-cpu` 限时试用，分阶段检查 IRQ/NAPI、
普通服务和 unbound 工作队列策略，并比较 WAN 吞吐量与 P99 RTT；不刷机、不改持久配置。

[真实 IPTV 回归](IPTV.md) 的入口为 `hg-iptv-regression.py`，使用现有运营商频道，
比较 rtp2httpd 在 HG / fnOS 上的两种架构及卸载开关，记录 CPU、码率、TS/RTP 完整性
与 TTL；可选 GRO 对照。它不要求可控 OLT 发包端，默认只生成计划。

新增的 [IPv4 组播回归](MULTICAST.md) 使用独立的 Linux 上游/接收端拓扑，入口为
`hg-multicast-regression.py`。它与下面两套 QoS 脚本共用执行锁，默认只生成计划。
组播后端仍需独立硬件验收；这次新增脚本没有执行实机用例。

这两套脚本随 ponwrt 源码维护，用于升级固件或修改业务分级后检查端到端表现。
脚本、Go/C# 探针、公共解析模块及已有单元测试均在本目录，运行不依赖 homelab
仓库。配套 KUnit/controller 用例随各功能 PR 审阅；这里的脚本用于端到端观察。

| 入口 | 用途 | 对 HG 的临时操作 |
| --- | --- | --- |
| [hg-iptv-regression.py](hg-iptv-regression.py) | 真实频道的两种 rtp2httpd 架构、软件/硬件对照、本地交付、TTL 与可选 GRO 诊断 | 暂停/恢复已启用的组播后端、桥 snooping/querier；`--gro-ab` 临时切换 PON GRO；设备端超时恢复 |
| [hg-qos-scenarios.py](hg-qos-scenarios.py) | 首选业务验收：普通、Mihomo、fnOS、Fake-IP；TCP/UDP 双向标记；冷启动与稳定阶段 | 查询状态、互斥锁；不安装分类、NAT、flowtable 或整形规则 |
| [hg-qos-regression.py](hg-qos-regression.py) | 定位工具：生产桥接、notrack、桥接加速、软件 flowtable、路由加速对照 | 专用测试流的临时 nft 表、NAT 和精确连接清理；可选 TC 限速 |

先运行业务验收，出现问题再用路径对照定位。两套脚本共用 HG 上的
`/tmp/hg-qos-regression.lock`，应串行执行。默认低流量测试不验收拥塞调度；
两套脚本的 `--congestion` 均会短暂与现有出口业务竞争。

## 1. 准备本地配置

从 ponwrt 仓库根目录执行：

```sh
mkdir -p tests/hg-blackbox/local
cp tests/hg-blackbox/configs/scenarios.example.json tests/hg-blackbox/local/scenarios.json
cp tests/hg-blackbox/configs/regression.example.json tests/hg-blackbox/local/regression.json
```

编辑两份配置中的地址、接口、端口速率和容器名。示例中的 `lan1 → lan4`、
1G 出口、地址与容器名是模板，不代表当前接线已经确认。端点必须位于所选 HG
两端口之间的实际转发路径，不能经另一台交换机直接互通后把结果当成 HG 验收。
无需新建测试 VLAN；这两套现有用例只覆盖所配置的一条 IPv4 LAN 路径。

连接配置使用 `hg-test-hg`、`hg-test-win`、`hg-test-fnos` 三个 SSH 别名，
在执行账户的 `~/.ssh/config` 中配置实际 HostName、User、密钥和需要的
ProxyJump。也可以把 JSON 的 `ssh` 改成现有别名或 `user@host`。
测试报文地址与 SSH 管理地址可以不同。

SSH 保留 `BatchMode=yes` 和严格主机密钥检查。每个主机对象可设置可选
`known_hosts` 路径；相对路径以该 JSON 所在目录解析。未设置时使用 OpenSSH
现有配置。实际配置、主机记录和凭据放在被忽略的 `local/` 或仓库外；示例中
不含用户名、密码、密钥或现网主机公钥。

任意工作目录均可通过脚本绝对路径运行，配置也可以通过 `--config PATH` 指定。
缺少本地配置时会报错，不会自动使用示例连接设备。

## 2. 依赖与当前固件

| 执行位置 | 依赖 |
| --- | --- |
| Linux 执行机 | Python 3.9+、Go 1.21+、OpenSSH 的 ssh/scp；Python/Go 仅使用标准库 |
| HG | root SSH、nft、现行业务分类与 flowtable；路径对照另需 conntrack；debugfs PPE 绑定用于辅助判断 |
| Win | 64 位 Windows、OpenSSH/SFTP、PowerShell；业务验收还需管理员 SSH、pktmon 与任务计划服务 |
| fnOS | x86_64 Linux、SSH；路径对照需 Python 3，无需 sudo；业务验收需 sudo、Docker、nsenter、ip 和已存在的端点命名空间 |

固件可以使用源码实现的 `airoha-qos`。脚本检查现行分类效果，不要求恢复旧
TC/GRO 规避配置，不会切换 QoS 模式或 GRO。它们仍要求现行策略中的
`bridge/inet hg_qos forward` 和 `bridge/inet fw4 forward` 链；运行前检查其
数字优先级。分类必须早于建表，错误顺序报执行环境错误，不算固件回归。

`tc` 只用于路径对照显式选择的 `--egress-cap-mbps` 诊断模式，常规运行不需要。
人工限速的结果不能代替新固件在物理出口上的硬件调度验收。

## 3. 运行顺序

以下为使用命令，本次迁入没有执行它们。

```sh
# 本地生成计划，不连接设备；仍需存在配置文件
python3 tests/hg-blackbox/hg-qos-scenarios.py --dry-run
python3 tests/hg-blackbox/hg-qos-regression.py --dry-run

# 业务策略低流量验收；sudo 密码仅交互输入
python3 tests/hg-blackbox/hg-qos-scenarios.py --ask-sudo-password

# 根据业务验收结果，按需做路径对照
python3 tests/hg-blackbox/hg-qos-regression.py

# 有合适的快入口/慢出口和发包能力时，验收两档竞争效果
python3 tests/hg-blackbox/hg-qos-scenarios.py --ask-sudo-password --congestion
```

已有 `sudo -n` 授权时省略 `--ask-sudo-password`。自动执行可指定
`--sudo-password-file /run/受限临时文件`，权限须为 `0600` 或更严格；内容仅送入
sudo 标准输入，不复制到报告。使用后由创建者删除文件。

详细用例、判定与清理机制见 [SCENARIOS.md](SCENARIOS.md) 和
[REGRESSION.md](REGRESSION.md)。

## 4. 报告与覆盖范围

默认输出到 ponwrt 的 `logs/hg-blackbox/{scenarios,regression}/<UTC时间>_<随机标识>/`，
也可用 `--output` 指定尚不存在的目录。报告包含 `REPORT.md`、`summary.json`、
固件/端口诊断、端点统计、清理结果，以及实际使用的脚本、辅助模块和探针快照。
业务验收还保存 TCP 捕获证据。`logs/` 已由仓库忽略。

| 退出码 | 判定 |
| --- | --- |
| 0 | 已运行项目全部 PASS；不代表未运行的拥塞或其他链路通过 |
| 1 | FAIL，有确定的标记、交付或有效竞争失败 |
| 2 | INCONCLUSIVE，样本、抓包、绑定或有效负载证据不足 |
| 3 | ERROR，执行、前置条件或清理错误 |

以上退出码针对已进入运行和报告阶段的测试；CLI 参数或本地配置缺失由
argparse 报错退出。配置解析/校验失败也发生在报告创建前，不能作为固件判定。
`--dry-run` 的 0 仅表示计划生成完成。

这两套 QoS 脚本不能直接验收全部物理口、VLAN/PCP 与安全隔离、IPv6、PON/WAN、
IPTV 组播、AP/WMM 或真实应用体验。DSCP 正确也不等于内部 skb 优先级、
最终硬件队列或拥塞调度正确。每次升级须按实际固件功能及拓扑确定剩余项目，不能沿用旧固件的 PASS 或 FAIL 作为新结果。

## 5. 脚本自身的已有单元测试

已有测试一起迁入 `unit/`，改为读取仓库内的示例配置，不依赖站点文件或 SSH。

```sh
python3 -m unittest discover -s tests/hg-blackbox/unit -p 'test_hg_qos*.py' -v
```

覆盖原有的规则范围、hook 顺序、元组识别、DSCP/丢包判定、竞争有效性和
pcapng 解析。它们不执行 Go/C# 探针，也不代替实机回归。本次没有添加新的
测试用例或运行这些测试。

## 来源与投稿边界

源文件保留自 PonWrt `hg-current` / `e801b682c1` 的六个入口及配套 helper、
既有用例。精确导出前 blob/SHA256 与早期 QoS 迁入历史见 [SOURCE.json](SOURCE.json)。
示例、helper 默认地址及对应 fixture 一致改为 TEST-NET；实际地址、凭据与
报告不投稿。本次只整理源码和说明，没有运行脚本、测试、编译或连接设备。

业务身份、DSCP 7/3/8、预期队列 4/2/0 是示例站点策略，不是 PonWrt 默认值，
也不由这些脚本安装。现有 QoS 业务套件要求已有 `hg_qos` 规则链、Windows
客户端及 Linux 容器命名空间；它不是可用于任意家庭的零配置验收器。
用户须根据实际分类规则和 QoS 表修改配置。其他入口的依赖与覆盖范围分别
见 CPU、LAN1、MULTICAST、IPTV 说明。接口缺失/证据不足不得宣称通过。
