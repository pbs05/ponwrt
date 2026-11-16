#!/usr/bin/env python3
"""Bounded Win -> HG -> fnOS packet-level QoS regression (Python stdlib)."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE / "helpers"))
from hg_qos_common import check_hook_order, check_qos_profile

DEFAULT_CONFIG = HERE / "local/regression.json"
LOCK = "/tmp/hg-qos-regression.lock"


def validate(c):
    for name in ("win_ip", "fnos_ip", "hg_ip", "test_vip"):
        ipaddress.IPv4Address(c[name])
    if c["test_vip"] in {c[x] for x in ("win_ip", "fnos_ip", "hg_ip")}:
        raise ValueError("test_vip must be a separate, unused address")
    if ipaddress.IPv4Address(c["test_vip"]) not in ipaddress.ip_network("198.19.0.0/16"):
        raise ValueError("Use an unused test_vip in reserved benchmark range 198.19.0.0/16")
    for name in ("input_device", "output_device", "fnos_device"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", c[name]):
            raise ValueError("Invalid interface name")
    if c["input_device"] == c["output_device"]:
        raise ValueError("Input/output must be separate HG ports")
    for host in ("hg", "win", "fnos"):
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", c[host]["ssh"]) or c[host]["ssh"].startswith("-"):
            raise ValueError("Use a simple SSH target or SSH config alias")
    tiers = c["tiers"]
    if len(tiers) != 3 or len({t["port"] for t in tiers}) != 3:
        raise ValueError("Three distinct test ports required")
    if [t["name"] for t in tiers] != ["normal", "proxy", "background"]:
        raise ValueError("Tiers must be ordered normal, proxy, background")
    for t in tiers:
        if not (1024 <= t["port"] <= 65535 and 0 <= t["dscp"] <= 63 and 0 <= t["priority"] <= 7):
            raise ValueError("Invalid tier")
    if not (100 <= c["egress_mbps"] <= 2500 and 0 <= c["fnos_dscp"] <= 63):
        raise ValueError("Invalid link rate or fnOS DSCP")


def make_rules(c, table, case, rotate=0):
    """The production case installs nothing. Others affect three scoped UDP flows."""
    if case == "production-bridge":
        return "", ""
    routed = case.startswith("routed") or case == "congestion"
    family = "inet" if routed else "bridge"
    ports = ", ".join(str(t["port"]) for t in c["tiers"])
    lines = [f"table {family} {table} {{"]
    if case == "routed-software":
        lines.append('flowtable sw { hook ingress priority -10; devices = { "%s", "%s" }; counter; }'
                     % (c["input_device"], c["output_device"]))
    if routed:
        lines += ["chain pre_nat { type nat hook prerouting priority -101; policy accept;",
                  f'ip saddr {c["win_ip"]} ip daddr {c["test_vip"]} udp dport {{ {ports} }} counter dnat ip to {c["fnos_ip"]}',
                  "}", "chain post_nat { type nat hook postrouting priority 99; policy accept;",
                  f'ct original ip daddr {c["test_vip"]} ip saddr {c["win_ip"]} ip daddr {c["fnos_ip"]} udp dport {{ {ports} }} counter snat ip to {c["hg_ip"]}', "}"]
        scope = f'ct original ip saddr {c["win_ip"]} ct original ip daddr {c["test_vip"]}'
    else:
        scope = f'ip saddr {{ {c["win_ip"]}, {c["fnos_ip"]} }} ip daddr {{ {c["win_ip"]}, {c["fnos_ip"]} }}'
    if case == "bridge-slow-control":
        lines += ["chain raw { type filter hook prerouting priority -300; policy accept;"]
        for f in ("sport", "dport"):
            lines.append(f"{scope} udp {f} {{ {ports} }} counter notrack")
        lines.append("}")
    # The bridge family's symbolic filter priority is -200, unlike inet's 0.
    # Override production after -210 but BEFORE fw4 snapshots the flow at -200.
    priority = -150 if routed else -205
    lines.append(f"chain classify {{ type filter hook forward priority {priority}; policy accept;")
    for i, port_tier in enumerate(c["tiers"]):
        tier = c["tiers"][(i + rotate) % 3]
        for f in ("sport", "dport"):
            lines.append(f'{scope} udp {f} {port_tier["port"]} ip dscp set {tier["dscp"]} meta priority set {tier["priority"]} counter comment "hqv_{tier["name"]}_{f}"')
    lines.append("}")
    if case == "routed-software":
        lines.append("chain addflow { type filter hook forward priority -140; policy accept;")
        for f in ("sport", "dport"):
            lines.append(f"{scope} udp {f} {{ {ports} }} counter flow add @sw")
        lines.append("}")
    lines.append("}")
    return family, "\n".join(lines) + "\n"


def bound_ports(c, diagnostic, source, destination):
    """Match each exact test tuple, including the actual bridge destination."""
    return [t["port"] for i, t in enumerate(c["tiers"]) if any(
        "BND IPv4" in line and f'orig={c["win_ip"]}:{source+i}->{destination}:{t["port"]} ' in line
        for line in diagnostic.splitlines())]


def assess(c, case, sent, received, *, rotate=0, seconds=1.5, congestion=False,
           udp_errors=0, hardware_bound=None, link_valid=True):
    """A valid packet mismatch is FAIL. Missing/limited measurements are INCONCLUSIVE."""
    rows, by_tier = [], {}
    for i, tier in enumerate(c["tiers"]):
        expected = c["fnos_dscp"] if case == "production-bridge" else c["tiers"][(i + rotate) % 3]["dscp"]
        tx = next((x for x in sent if x["port"] == tier["port"] and x["phase"] == 1), {})
        rx = next((x for x in received if x["port"] == tier["port"] and x["phase"] == 1), {})
        n, offered = rx.get("packets", 0), tx.get("packets", 0)
        marks = {int(k): v for k, v in rx.get("dscp", {}).items()}
        if any(k >= 0 and k != expected and v > 0 for k, v in marks.items()):
            status, reason = "FAIL", f"expected DSCP {expected}, received {marks}"
        elif n < 100 or offered < 100 or tx.get("errors", 0) or -1 in marks:
            status, reason = "INCONCLUSIVE", "too few packets, send errors, or unavailable receive TOS"
        elif sum(marks.values()) != n:
            status, reason = "INCONCLUSIVE", "incomplete TOS observations"
        elif not congestion and n / offered < .99:
            status, reason = "FAIL", "more than 1% packet loss during low-rate test"
        else:
            status, reason = "PASS", "DSCP retained at receiver"
        row = dict(port=tier["port"], tier="background" if case == "production-bridge" else c["tiers"][(i + rotate) % 3]["name"],
                   expected_dscp=expected, sent=offered, received=n, dscp=marks,
                   tx_mbps=tx.get("bytes", 0) * 8 / seconds / 1e6,
                   rx_mbps=rx.get("bytes", 0) * 8 / seconds / 1e6,
                   delivery=(n / offered if offered else 0),
                   socket_drops=rx.get("socket_drops", 0), status=status, reason=reason)
        rows.append(row)
        by_tier[row["tier"]] = row
    priority = None
    if congestion:
        tx_sum = sum(x["tx_mbps"] for x in rows)
        rx_sum = sum(x["rx_mbps"] for x in rows)
        # 1400-byte UDP payload, IPv4 + UDP + Ethernet + FCS + preamble + IFG.
        payload_capacity = c["egress_mbps"] * 1400 / (1400 + 66)
        valid = (link_valid and hardware_bound is True and udp_errors == 0
                 and not any(x["socket_drops"] for x in rows)
                 and tx_sum > payload_capacity * 1.15
                 and rx_sum > payload_capacity * .9
                 and rx_sum < payload_capacity * 1.07
                 and all(x["status"] == "PASS" for x in rows)
                 and all(.36 * c["egress_mbps"] <= x["tx_mbps"] <= .48 * c["egress_mbps"] for x in rows))
        if not valid:
            priority = dict(status="INCONCLUSIVE", reason="requires verified PPE binding, equal sustained offered load, saturated egress, correct markings and no receiver drops",
                            payload_capacity_mbps=payload_capacity, offered_mbps=tx_sum,
                            received_mbps=rx_sum, hardware_bound=hardware_bound, udp_errors=udp_errors)
        else:
            normal, proxy, background = [by_tier[k]["delivery"] for k in ("normal", "proxy", "background")]
            # Each high tier offers < half of the link; both must be served before background.
            passed = normal >= .95 and proxy >= .90 and min(normal, proxy) - background >= .20
            priority = dict(status="PASS" if passed else "FAIL",
                            reason="ordinary/proxy protected over background" if passed else "egress does not protect the two higher tiers over background",
                            delivery={k: by_tier[k]["delivery"] for k in by_tier},
                            payload_capacity_mbps=payload_capacity)
    statuses = [r["status"] for r in rows] + ([priority["status"]] if priority else [])
    overall = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    return dict(status=overall, packets=rows, priority=priority)


class Runner:
    def __init__(self, args):
        self.args = args
        self.config_path = Path(args.config).expanduser().resolve()
        self.c = json.loads(self.config_path.read_text())
        validate(self.c)
        self.id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        self.out = Path(args.output or ROOT / "logs/hg-blackbox/regression" / self.id).resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.table = "hqv_" + self.id.replace("T", "t").replace("Z", "z")
        self.remote = "/tmp/" + self.table
        self.exe_name = self.table + ".cs"
        self.locked = False
        self.results = []
        self.cleanup_errors = []
        self.save("config.json", self.c)
        (self.out / "runner.py").write_bytes(Path(__file__).read_bytes())
        (self.out / "hg_qos_common.py").write_bytes((HERE / "helpers/hg_qos_common.py").read_bytes())
        probe_sources = self.out / "probe-source"
        probe_sources.mkdir()
        for source_file in (HERE / "helpers/hg-qos-probe").glob("*"):
            if source_file.is_file():
                (probe_sources / source_file.name).write_bytes(source_file.read_bytes())

    def save(self, name, value):
        (self.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def ssh_args(self, host):
        h = self.c[host]
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
        if h.get("known_hosts"):
            path = Path(h["known_hosts"]).expanduser()
            if not path.is_absolute():
                path = self.config_path.parent / path
            cmd += ["-o", "UserKnownHostsFile=" + str(path.resolve())]
        return cmd + [h["ssh"]]

    def command(self, cmd, *, label=None, data=None, timeout=30, check=True):
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        if label:
            self.save(label + ".json", dict(command=cmd, returncode=r.returncode,
                                           stdout=r.stdout.decode(errors="replace"), stderr=r.stderr.decode(errors="replace")))
        if check and r.returncode:
            raise RuntimeError(f"{label or cmd[0]} failed: {r.stderr.decode(errors='replace')[:1500]}")
        return r

    def shell(self, host, script, label=None, check=True):
        return self.command(self.ssh_args(host) + ["sh", "-s"], data=script.encode(), label=label, check=check)

    def ps_cmd(self, script):
        script = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';" + script
        encoded = base64.b64encode(script.encode("utf-16le")).decode()
        return self.ssh_args("win") + ["powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded]

    def cleanup_script(self, all_cases=False):
        c = self.c
        lines = [f'nft delete table inet {self.table} 2>/dev/null || true',
                 f'nft delete table bridge {self.table} 2>/dev/null || true']
        if self.args.egress_cap_mbps:
            lines.insert(0, self.unshape_command())
        # Delete only this run's exact test tuples, never flush existing connections.
        sources = [self.source+i for i in range(3)]
        if all_cases:
            sources = [self.source_base+case*10+i for case in range(7) for i in range(3)]
        for source in sources:
            for dest in (c["fnos_ip"], c["test_vip"]):
                for tier in c["tiers"]:
                    lines.append(f'conntrack -D -p udp -s {c["win_ip"]} -d {dest} --sport {source} --dport {tier["port"]} >/dev/null 2>&1 || true')
        return "\n".join(lines) + "\n"

    def unshape_command(self):
        dev = self.c["output_device"]
        handle = f"{self.source_base:x}:"
        return f"if tc qdisc show dev {dev} | grep -q '^qdisc tbf {handle} root '; then tc qdisc del dev {dev} root handle {handle}; fi"

    def unshape(self, label):
        if not self.args.egress_cap_mbps:
            return
        self.shell("hg", self.unshape_command() + f'\nif [ -f {self.remote}/cap-watchdog.pid ]; then kill "$(cat {self.remote}/cap-watchdog.pid)" 2>/dev/null || true; rm -f {self.remote}/cap-watchdog.pid; fi\ntc qdisc show dev {self.c["output_device"]}\n', label)

    def prepare(self):
        qos = self.shell("hg", "airoha-qosctl get " + shlex.quote(self.c.get("qos_device", "lan1")),
                         "qos-profile", check=False)
        self.qos_profile = check_qos_profile(qos.stdout, self.c.get("expected_qos"))
        self.save("qos-profile-check.json", self.qos_profile)
        self.source = 47000 + int(uuid.uuid4().hex[:4], 16) % 12000
        self.source_base = self.source
        self.case_index = 0
        c = self.c
        chains = json.loads(self.shell("hg", "nft -j list chains\n", "preflight-chains").stdout)
        self.save("hook-order.json", check_hook_order(chains, test_override=True))
        probe = "ubus call system board\nuname -r\nnft list flowtables\n"
        for dev in (c["input_device"], c["output_device"]):
            probe += f'printf "{dev} "; cat /sys/class/net/{dev}/speed /sys/class/net/{dev}/carrier\n'
        self.shell("hg", probe, "preflight-hg")
        speed = self.shell("hg", f'cat /sys/class/net/{c["output_device"]}/speed\ncat /sys/class/net/{c["input_device"]}/speed\n').stdout.decode().split()
        self.link_valid = len(speed) == 2 and int(speed[0]) == c["egress_mbps"] and int(speed[1]) > int(speed[0])
        if self.args.egress_cap_mbps:
            if self.args.egress_cap_mbps >= c["egress_mbps"]:
                raise RuntimeError("Test egress cap must be below physical link speed")
            qdisc = self.shell("hg", f'tc qdisc show dev {c["output_device"]}\n', "preflight-qdisc").stdout.decode()
            if not re.search(r"^qdisc noqueue 0: root", qdisc, re.M):
                raise RuntimeError("Refusing temporary shaping: output already has a root qdisc")
        self.shell("fnos", "command -v python3\nuname -m\n", "preflight-fnos")
        win = self.command(self.ps_cmd("@{amd64=[Environment]::Is64BitOperatingSystem;temp=$env:TEMP} | ConvertTo-Json -Compress"), label="preflight-win")
        win_info = json.loads(win.stdout.decode("utf-8-sig"))
        if not win_info["amd64"]:
            raise RuntimeError("The packet helper requires 64-bit Windows")
        win_temp = win_info["temp"].replace("\\", "/").rstrip("/")
        if not re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9_ ./-]+", win_temp):
            raise RuntimeError("Unsupported Windows TEMP path")
        helper = HERE / "helpers/hg-qos-probe"
        env = dict(os.environ, CGO_ENABLED="0", GO111MODULE="off", GOARCH="amd64")
        for target, name in (("linux", "probe-linux"),):
            r = subprocess.run(["go", "build", "-trimpath", "-o", str(self.out / name), "."], cwd=helper,
                               env=dict(env, GOOS=target), capture_output=True, timeout=90)
            if r.returncode:
                raise RuntimeError(r.stderr.decode())
        self.save("probe-hashes.json", {name: hashlib.sha256((self.out / name).read_bytes()).hexdigest() for name in ("probe-linux",)})
        # The lock is released only by its owner. A detached local watchdog also owns cleanup.
        script = f'''set -e
mkdir {LOCK} || {{ echo 'Another regression run owns {LOCK}' >&2; exit 1; }}
echo {self.id} > {LOCK}/owner
mkdir {self.remote}
cat > {self.remote}/cleanup.sh <<'HQVEOF'
{self.cleanup_script(all_cases=True)}HQVEOF
cat > {self.remote}/watchdog.sh <<'HQVEOF'
sleep 240
sh {self.remote}/cleanup.sh
if [ "$(cat {LOCK}/owner 2>/dev/null)" = "{self.id}" ]; then rm -f {LOCK}/owner; rmdir {LOCK}; fi
HQVEOF
nohup sh {self.remote}/watchdog.sh </dev/null >{self.remote}/watchdog.log 2>&1 &
echo $! > {self.remote}/watchdog.pid
'''
        # If an SSH response is lost after mkdir, the owner token permits safe recovery.
        self.locked = True
        self.shell("hg", script, "lock-and-watchdog")
        self.command(self.ssh_args("fnos") + [f"cat > {self.remote}-probe && chmod 700 {self.remote}-probe"],
                     data=(self.out / "probe-linux").read_bytes(), label="deploy-fnos")
        # Transfer a small C# sender through SFTP, then compile inside PowerShell.
        # No prebuilt executable or additional Windows network driver is installed.
        self.command(["scp", "-q"] + self.ssh_args("win")[2:-1] +
                     [str(helper / "sender.cs"), self.c["win"]["ssh"] + ":" + win_temp + "/" + self.exe_name],
                     label="deploy-win", timeout=45)

    def fnos_counters(self, label):
        dev = self.c["fnos_device"]
        script = f'''python3 - <<'PY'
import json
from pathlib import Path
lines=Path('/proc/net/snmp').read_text().splitlines(); result={{}}
for i,line in enumerate(lines[:-1]):
 if line.startswith('Udp:') and not line.split()[1].isdigit(): result['udp']=dict(zip(line.split()[1:],map(int,lines[i+1].split()[1:])))
result['interface']={{x:int(Path('/sys/class/net/{dev}/statistics/'+x).read_text()) for x in ('rx_dropped','rx_errors','rx_packets','rx_bytes')}}
print(json.dumps(result))
PY
'''
        r = self.shell("fnos", script, label)
        return json.loads(r.stdout)

    def collect(self, label):
        r = self.shell("hg", f'date -Iseconds\ncat /proc/loadavg\ncat /sys/kernel/debug/ppe/bind 2>/dev/null\nnft -j list table inet {self.table} 2>/dev/null\nnft -j list table bridge {self.table} 2>/dev/null\ntrue\n', label)
        return r.stdout.decode()

    def one_case(self, case, rotate=0):
        c = self.c
        # PPE cleanup may lag conntrack deletion. Never recycle a tuple across
        # different path/classification controls in the same run.
        self.source = self.source_base + self.case_index * 10
        self.case_index += 1
        label = case + (f"-{rotate}" if case == "congestion" else "")
        congestion = case == "congestion"
        seconds = 2.5 if congestion else 1.5
        warm = 6 if congestion else 4
        effective = dict(self.c)
        if congestion and self.args.egress_cap_mbps:
            effective["egress_mbps"] = self.args.egress_cap_mbps
        rate = .45 * effective["egress_mbps"] if congestion else 4
        lifetime = warm + seconds + 5
        family, rules = make_rules(self.c, self.table, case, rotate)
        (self.out / (label + ".nft")).write_text(rules)
        server = client = sampler = None
        print(f"{label}: starting ({'2.5 s egress contention' if congestion else 'low-rate'})", flush=True)
        try:
            if rules:
                precheck = "nft -c -f - <<'HQVEOF'\n" + rules + "HQVEOF\n"
                self.shell("hg", precheck, label + "-precheck")
                self.shell("hg", "nft -f - <<'HQVEOF'\n" + rules + "HQVEOF\n", label + "-setup")
            before = self.fnos_counters(label + "-fnos-before")
            nonce = int(uuid.uuid4().hex[:15], 16)
            ports = ",".join(str(t["port"]) for t in self.c["tiers"])
            server_args = [self.remote + "-probe", "-mode", "server", "-addr", self.c["fnos_ip"],
                           "-ports", ports, "-nonce", str(nonce), "-lifetime", str(lifetime)]
            server = subprocess.Popen(self.ssh_args("fnos") + [shlex.join(server_args)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if not select.select([server.stdout], [], [], 12)[0] or server.stdout.readline().strip() != b"READY":
                raise RuntimeError("fnOS UDP receiver did not become ready")
            dest = self.c["test_vip"] if family == "inet" else self.c["fnos_ip"]
            if congestion:
                sample_script = f'''i=0
while [ "$i" -lt 15 ]; do
    read -r uptime idle < /proc/uptime
    read -r rx < /sys/class/net/{c['input_device']}/statistics/rx_bytes
    read -r tx < /sys/class/net/{c['output_device']}/statistics/tx_bytes
    read -r dropped < /sys/class/net/{c['output_device']}/statistics/tx_dropped
    printf '%s %s %s %s\\n' "$uptime" "$rx" "$tx" "$dropped"
    i=$((i+1)); sleep 1
done
'''
                sampler = subprocess.Popen(self.ssh_args("hg") + ["sh", "-s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                sampler.stdin.write(sample_script.encode()); sampler.stdin.close(); sampler.stdin=None
            cmd = f"Add-Type -Path (Join-Path $env:TEMP '{self.exe_name}'); [HgQoS.Probe]::RunBatch('{dest}',[int[]]@({ports}),{self.source},{rate},{seconds},{warm},[uint64]{nonce},{self.args.udp_batch}) | ConvertTo-Json -Compress; exit 0"
            client = subprocess.Popen(self.ps_cmd(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(3)
            diagnostic = self.collect(label + "-during")
            if congestion and self.args.egress_cap_mbps:
                time.sleep(2)
                dev = self.c["output_device"]
                handle = f"{self.source_base:x}:"
                # Install a separate 15 s restoration timer before creating the cap.
                timer = "sleep 15; " + self.unshape_command()
                self.shell("hg", f'set -e\nnohup sh -c {shlex.quote(timer)} </dev/null >/dev/null 2>&1 &\necho $! >{self.remote}/cap-watchdog.pid\ntc qdisc add dev {dev} root handle {handle} tbf rate {self.args.egress_cap_mbps}mbit burst 32kb latency 10ms\ntc -s qdisc show dev {dev}\n', label + "-shape")
            out, err = client.communicate(timeout=warm + seconds + 8)
            self.save(label + "-sender.json", dict(returncode=client.returncode, stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace")))
            if client.returncode:
                raise RuntimeError("Win packet sender failed")
            sent = json.loads(out.decode("utf-8-sig"))
            self.unshape(label + "-unshape")
            # PowerShell startup/compilation can make the first snapshot too
            # early for asynchronous PPE binding. Sample again before cleanup.
            diagnostic += "\n" + self.collect(label + "-after-sender")
            out, err = server.communicate(timeout=lifetime + 3)
            self.save(label + "-receiver.json", dict(returncode=server.returncode, stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace")))
            if server.returncode:
                raise RuntimeError("fnOS packet receiver failed")
            received = json.loads(out)
            if sampler:
                sample_out,sample_err=sampler.communicate(timeout=12)
                self.save(label+"-wire-samples.json",dict(rc=sampler.returncode,stdout=sample_out.decode(),stderr=sample_err.decode()))
            after = self.fnos_counters(label + "-fnos-after")
            errors = sum(max(0, after["udp"].get(k, 0) - before["udp"].get(k, 0)) for k in ("RcvbufErrors", "InErrors"))
            bindings = bound_ports(self.c, diagnostic, self.source, dest)
            result = assess(effective, case, sent, received, rotate=rotate, seconds=seconds,
                            congestion=congestion, udp_errors=errors, hardware_bound=len(bindings) == 3,
                            link_valid=self.link_valid)
            if case == "routed-software" and bindings:
                result["status"] = "ERROR"
                result["setup_error"] = "PPE binding found in a requested software-only test"
            result.update(case=label, source_ports=[self.source+i for i in range(3)], hardware_bound_ports=bindings, fnos_udp_errors=errors,
                          physical_egress_mbps=self.c["egress_mbps"], test_egress_mbps=effective["egress_mbps"],
                          scope="IPv4 UDP, Win -> HG -> fnOS, configured egress; no reverse-direction/IPv6/WMM claim")
            self.results.append(result)
            self.save(label + "-assessment.json", result)
            print(f"{label}: {result['status']}", flush=True)
        finally:
            for p in (client, server, sampler):
                if p is not None and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()
            self.unshape(label + "-unshape-finally")
            self.shell("hg", self.cleanup_script(), label + "-cleanup")

    def cleanup(self):
        if not self.locked:
            return
        try:
            script = f'''if [ "$(cat {LOCK}/owner 2>/dev/null)" = "{self.id}" ]; then
sh {self.remote}/cleanup.sh
if [ -f {self.remote}/watchdog.pid ]; then kill "$(cat {self.remote}/watchdog.pid)" 2>/dev/null || true; fi
if [ -f {self.remote}/cap-watchdog.pid ]; then kill "$(cat {self.remote}/cap-watchdog.pid)" 2>/dev/null || true; fi
rm -f {self.remote}/cleanup.sh {self.remote}/watchdog.sh {self.remote}/watchdog.pid {self.remote}/watchdog.log {self.remote}/cap-watchdog.pid
rmdir {self.remote}
rm -f {LOCK}/owner
rmdir {LOCK}
fi
nft list tables
'''
            r = self.shell("hg", script, "final-cleanup-hg")
            if self.table in r.stdout.decode():
                raise RuntimeError("Temporary nft table remains")
        except Exception as e:
            self.cleanup_errors.append(str(e) + "; remote watchdog scheduled at 240 s")
        for host, cmd in (("fnos", self.ssh_args("fnos") + [f"rm -f {self.remote}-probe"]),
                          ("win", self.ps_cmd(f"$p=Join-Path $env:TEMP '{self.exe_name}';if(Test-Path -LiteralPath $p){{Remove-Item -LiteralPath $p -Force}};exit 0"))):
            try:
                self.command(cmd, label="final-cleanup-" + host)
            except Exception as e:
                self.cleanup_errors.append(str(e))

    def report(self, error=None):
        overall = "ERROR" if error or self.cleanup_errors or any(r["status"] == "ERROR" for r in self.results) else "FAIL" if any(r["status"] == "FAIL" for r in self.results) else "INCONCLUSIVE" if not self.results or any(r["status"] == "INCONCLUSIVE" for r in self.results) else "PASS"
        summary = dict(schema_version=1, run=self.id, status=overall, error=error,
                       qos_profile=getattr(self, "qos_profile", None),
                       cleanup_errors=self.cleanup_errors, results=self.results,
                       congestion_requested=self.args.congestion, egress_cap_mbps=self.args.egress_cap_mbps)
        self.save("summary.json", summary)
        lines = [f"# HG QoS regression: {overall}", "", f"Run: `{self.id}`", "",
                 "| Case | Result |", "|---|---|"]
        lines += [f"| {r['case']} | {r['status']} |" for r in self.results]
        for r in self.results:
            lines += ["", f"## {r['case']}", "", "| Tier/port | DSCP expected/received | Sent/received packets | Tx/Rx Mbps | Result |", "|---|---|---|---|---|"]
            for p in r["packets"]:
                lines.append(f"| {p['tier']}/{p['port']} | {p['expected_dscp']}/{p['dscp']} | {p['sent']}/{p['received']} | {p['tx_mbps']:.1f}/{p['rx_mbps']:.1f} | {p['status']} |")
            if r["priority"]:
                lines += ["", r["priority"]["reason"], "", f"Physical egress: {r['physical_egress_mbps']} Mbps; tested egress: {r['test_egress_mbps']} Mbps."]
        lines += ["", "Results cover IPv4 UDP in this direction and topology. DSCP success alone does not prove skb priority, hardware queue selection, or scheduling. Default runs do not test congestion. Hardware binding is diagnostic evidence; missing evidence makes the congestion verdict inconclusive.", ""]
        if error:
            lines += ["Error: " + error, ""]
        if self.cleanup_errors:
            lines += ["Cleanup errors: " + repr(self.cleanup_errors), ""]
        (self.out / "REPORT.md").write_text("\n".join(lines))
        print(f"{overall}: {self.out / 'REPORT.md'}", flush=True)
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}[overall]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Site JSON; defaults to local/regression.json next to this script")
    p.add_argument("--output", help="New evidence directory; must not exist")
    p.add_argument("--congestion", action="store_true", help="Also run two 2.5-second shared egress contention trials")
    p.add_argument("--only-congestion", action="store_true", help="With --congestion, skip the low-rate cases")
    p.add_argument("--udp-batch", type=int, default=1, choices=range(1,33), help="Windows UDP segmentation batch; each datagram remains 1400 bytes")
    p.add_argument("--egress-cap-mbps", type=int, help="Opt-in temporary TC egress cap, 100..800 Mbps, with a 15 s independent restoration timer")
    p.add_argument("--dry-run", action="store_true", help="Render test rules locally; no SSH or traffic")
    args = p.parse_args()
    if args.only_congestion and not args.congestion:
        p.error("--only-congestion requires --congestion")
    if args.egress_cap_mbps is not None and (not args.congestion or not 100 <= args.egress_cap_mbps <= 800):
        p.error("--egress-cap-mbps requires --congestion and a value in 100..800")
    if not Path(args.config).expanduser().is_file():
        p.error("Missing site configuration: copy configs/regression.example.json to local/regression.json, edit it, or pass --config PATH")
    r = Runner(args)
    cases = ["production-bridge", "bridge-slow-control", "bridge-auto", "routed-software", "routed-auto"]
    if args.only_congestion:
        cases = []
    if args.dry_run:
        for case in cases:
            _, rules = make_rules(r.c, r.table, case)
            (r.out / (case + ".nft")).write_text(rules)
        if args.congestion:
            for rotate in (0, 1):
                _, rules = make_rules(r.c, r.table, "congestion", rotate)
                (r.out / f"congestion-{rotate}.nft").write_text(rules)
        print(r.out)
        return 0
    error = None
    try:
        r.prepare()
        for case in cases:
            r.one_case(case)
        if args.congestion:
            if not r.link_valid:
                raise RuntimeError("Congestion test requires actual output speed == configured speed and faster input")
            for rotate in (0, 1):
                r.one_case("congestion", rotate)
    except (Exception, KeyboardInterrupt) as e:
        error = str(e) or type(e).__name__
    finally:
        r.cleanup()
    return r.report(error)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
