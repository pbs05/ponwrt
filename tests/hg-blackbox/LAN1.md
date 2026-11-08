# HG LAN1 / HSGMII 回归观察

入口 `hg-lan1-regression.py`，只支持已核对地址映射的 Nokia XG-040G-MD UBI。
默认 60 秒、每 5 秒经 SSH 读取一次快照；不写寄存器、切换 LED 模式、重置链路、
重启服务或安装设备端常驻程序。正常采样不注入流量。SSH 使用严格主机密钥检查。

```sh
# 先在已有 SSH 配置中定义 hg-test-hg，保留可信 known_hosts。
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg

# 正常运行观察一小时；流量活动可使 LED 闪烁，这不是失败条件。
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg --duration 3600

# 显式重启 HG；会中断全网，不可在未经允许的维护窗口外执行。
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg \
  --event reboot --duration 240 --warmup 150

# 启动观察后，在前 30 秒内人工拔插 LAN1，或者重启其对端。
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg \
  --event link-change --duration 120
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg \
  --event peer-restart --duration 180 --warmup 120
```

管理路径应从其他 LAN 口进入 HG，避免拔插被测口同时切断观察路径。
`--ssh-config FILE`、`--known-hosts FILE` 可使用仓库外的既有连接配置；示例不含凭据。
以 1G/100M 对端连接 LAN1 时，分别指定 `--expect-speed 1000/100`。

转发/视频负载在独立轮次显式运行；`--traffic-command` 后面全部内容是本地 argv，
不经过 shell。应使用自身有超时和远程清理机制的已有回归脚本。观察结束会终止仍在
运行的本地进程组；无法保证任意第三方命令自行创建的远程任务也被回收。

```sh
python3 tests/hg-blackbox/hg-lan1-regression.py --ssh hg-test-hg --duration 180 \
  --traffic-command python3 tests/hg-blackbox/hg-qos-scenarios.py --config LOCAL_CONFIG
```

脚本记录 boot ID、carrier 及其变化、协商速率、物理错误与丢弃计数、PCS RX 同步、
FreqDet 锁定、MAC 开停位、LED 硬件控制状态和相关内核日志。
只有显式 `--event reboot` 才执行 HG 重启；其他 event 由人工在 warmup 内完成。
不会自动改变端口速率、执行 PMA 故障注入、刷机或修改配置。

## 判定与限制

- `PASS / 0`：本轮可观察的健康条件通过，硬件 LED 保持控制；不代表已复现并修复失步。
- `FAIL / 1`：新增物理收发/CRC 错误、普通轮次非预期 carrier/boot 变化、持续 PCS
  异常，或 LED 退出硬件控制。显式事件 warmup 内允许预期断链。
- `INCONCLUSIVE / 2`：SSH 观察缺口、样本不足、计数重置或没有观察到指定事件。
  跳板/SSH 不通不能直接判为 HG 故障。
- `ERROR / 3`：前置快照、执行或负载命令失败；CLI 参数错误由 argparse 返回 2。

历史错误计数不会直接判 FAIL；只判本轮新增错误。丢弃计数原样保留，不能在缺少
发包/收包序号及硬件丢弃计数对应关系时直接称为链路缺包。

输出位于 `logs/hg-blackbox/lan1/<UTC时间>/`，或 `--output` 指定的新目录，包含
`REPORT.md`、`summary.json`、原始快照及可选负载日志。没有复现同步丢失并验证
端到端收发时，`physical_recovery_proven` 始终为 false。
默认 SSH 观察自身也有开销，不能据此精确测量新增驱动定时器的 CPU 消耗。

综合验收还须运行既有 QoS/IPTV 回归和 2.5G→1G 有限突发传输，核对序号完整性、
硬件丢弃计数及 PPE/conntrack 卸载状态。此工具不把正常速率过订阅的丢弃当作
驱动缺陷，也不以单次 ping 正常替代吞吐、时延或恢复能力验收。

本地解析/判定单测，不连接设备：

```sh
python3 -m unittest discover -s tests/hg-blackbox/unit -p 'test_hg_lan1.py' -v
```
