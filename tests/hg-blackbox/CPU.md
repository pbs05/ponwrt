# HG CPU 调度黑盒回归

入口 `hg-cpu-regression.py` 默认只生成本地计划。源码与运行时策略见
[INGRESS_CPU.md](https://github.com/pbs05/ponwrt/pull/90)。本轮开发没有执行 HG 实机测试。

```sh
cp tests/hg-blackbox/configs/cpu.example.json tests/hg-blackbox/local/cpu.json
python3 tests/hg-blackbox/hg-cpu-regression.py --config tests/hg-blackbox/local/cpu.json
# 已授权测试窗口、正确接线和受控 iperf3 服务端具备后：
python3 tests/hg-blackbox/hg-cpu-regression.py --config tests/hg-blackbox/local/cpu.json --run
```

示例地址仅为文档地址，必须替换为实际值。流量由 HG 下游的 Linux 客户端发起，
到 WAN 外侧用户控制的 `iperf3` 服务端；不启动 HG 本机 iperf 服务，不把本机
吞吐量当成转发能力。脚本检查客户端路由的下一跳为声明的 HG 网关，接线与
WAN 端点归属仍需操作者确认。SSH 保留严格主机指纹校验。

`traffic.server/source/gateway` 可使用 IPv4 或 IPv6，三者须属同一地址族。
IPv6 的 `gateway` 填客户端实际路由中的下一跳地址，可为不带 `%接口` 的链路本地地址；
脚本会选择对应的 `ip -4/-6`、iperf3 与 ping 地址族。客户端需有可用 ICMP 权限。

每轮为基线、network、services、full、复测基线，各自执行上下行流量；第二轮
逆序执行三个策略，默认三轮、每项 180 秒。每五秒保存完整 IRQ/NAPI 对应表、
请求及有效掩码、CPU/softirq/softnet/内存计数。每个阶段使用 180 秒租约，并在
运行期间续约；退出、异常和信号都尝试恢复。设备端守护独立恢复过期试用，
共享测试锁也有独立超时清理，不依赖控制端保持连接。

只有 UCI `enabled=0` 且没有其他活动 CPU 策略时才可运行。脚本不启用持久配置，
不重启、不刷机，不修改频率、QoS、PPE 或网络配置；结束后核对配置摘要。
CPU、QoS、组播和 IPTV 脚本共用测试锁，不能并发运行。

验收口径：

- IRQ 请求和有效掩码、NAPI 允许 CPU 必须与本次试用相符。
- 相对本轮前后基线均值，吞吐量不少于 95%，P99 RTT 增量不超过
  `max(10%, 0.5 ms)`，不新增 ping 丢包。
- 前后基线吞吐量漂移超过 10%，或 P99 漂移超过 `max(20%, 1 ms)`，记为
  INCONCLUSIVE，避免把外网变化直接判成 CPU 策略缺陷。
- 删除配置中的 `traffic` 可仅检查绑核和回滚；总结果会明确标记 WAN 性能未验证。
- 计数和亲和性不能证明 NAPI 排队延迟或 PPE 命中。线程被允许运行于一个核，
  不等于所有相关内核工作都被隔离。

IPTV 另用 [IPTV.md](IPTV.md) 的真实频道回归，分别测 HG/fnOS 架构。可在单独
限时 CPU 试用窗口内运行，并由控制端持续 `renew TOKEN`；保留每种策略对应的
报告。IPTV 脚本不会自行启用 CPU 策略。实际验收时要覆盖组播/HTTP 与 WAN 负载
共存的窗口；单独的 WAN iperf 或单独看视频都不能代表全部竞争场景。

退出码与其他黑盒脚本相同：0 PASS、1 FAIL、2 INCONCLUSIVE、3 ERROR。
本地单元测试：`python3 -m unittest discover -s tests/hg-blackbox/unit -p 'test_hg_cpu.py' -v`。
