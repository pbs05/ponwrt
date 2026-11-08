#!/usr/bin/env python3
"""Black box regression of the deployed HG service-identity QoS policy.

Uses existing fnOS network namespaces and bounded probes. Installs no packet
classification, NAT, shaping or flow-offload rules. Python standard library.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
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
from hg_qos_pcap import tcp_observations

DEFAULT_CONFIG = HERE / "local/scenarios.json"
LOCK = "/tmp/hg-qos-regression.lock"
ROLES = ("normal", "proxy", "background", "fakeip")


def validate(c):
    ipaddress.IPv4Address(c["win_ip"])
    for host in ("hg", "win", "fnos"):
        target = c[host]["ssh"]
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", target) or target.startswith("-"):
            raise ValueError("Use a simple SSH target")
    for name in ("input_device", "output_device"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", c[name]):
            raise ValueError("Invalid HG interface")
    if c["input_device"] == c["output_device"] or not 100 <= c["egress_mbps"] <= 1000:
        raise ValueError("Requires separate HG ports and a 100..1000 Mbps receiver link")
    for role in ROLES:
        e = c["endpoints"][role]
        ipaddress.IPv4Address(e["ip"])
        if e["ip"] == c["win_ip"] or not 0 <= e["dscp"] <= 63:
            raise ValueError("Invalid endpoint identity/DSCP")
        for name in ("interface", "container"):
            if name in e and not re.fullmatch(r"[A-Za-z0-9_.-]+", e[name]):
                raise ValueError("Invalid interface/container")
    if len({c["endpoints"][r]["ip"] for r in ROLES}) != 4:
        raise ValueError("Use four distinct existing endpoint addresses")


def make_cases(c, source=50000, port=46000, congestion=False):
    cases = []

    def add(role, proto="udp", size=128, *, source_port=None, expected=None, label=None, group=0, bulk=False):
        e = c["endpoints"][role]
        i = len(cases) + 1
        cases.append(dict(Id=i, Name=label or f"{role}-{proto}-{size}", Role=role,
                          Address=e["ip"], Port=port,
                          SourcePort=source_port if source_port is not None else source+i,
                          Protocol=proto, Size=size, Group=group, Bulk=bulk,
                          Seconds=2.5 if bulk else 2,
                          RateMbps=.65*c["egress_mbps"] if bulk else size*8*100/1e6,
                          ExpectedDSCP=e["dscp"] if expected is None else expected))

    for role in ROLES:
        add(role)
        add(role, size=1400)
        add(role, proto="tcp", size=1400)
    for role in ("proxy", "background"):
        for proto in ("udp", "tcp"):
            add(role, proto=proto, source_port=53, expected=c["endpoints"]["normal"]["dscp"],
                label=f"{role}-dns-{proto}-exception")
    add("background", source_port=67, expected=c["endpoints"]["normal"]["dscp"], label="background-dhcp-unicast-exception")
    if congestion:
        for group, pair in enumerate((("normal", "proxy"), ("proxy", "background")), 1):
            for role in pair:
                add(role, size=1400, group=group, bulk=True, label=f"contention-{group}-{role}")
    return cases


def worst(statuses):
    return next((s for s in ("ERROR", "FAIL", "INCONCLUSIVE") if s in statuses), "PASS")


def observation(expected, observed, target, *, minimum=100, socket_drops=0):
    """Wrong DSCP is positive evidence, even if the sample is otherwise small."""
    n = observed.get("packets", 0)
    marks = {int(k): int(v) for k, v in observed.get("dscp", {}).items()}
    wrong = {k: v for k, v in marks.items() if k >= 0 and k != expected and v > 0}
    if wrong:
        status, reason = "FAIL", f"expected DSCP {expected}; observed {marks}"
    elif n < minimum or target < minimum or -1 in marks or sum(marks.values()) != n:
        status, reason = "INCONCLUSIVE", "insufficient or incomplete DSCP observations"
    elif socket_drops:
        status, reason = "INCONCLUSIVE", "receiver socket dropped packets"
    elif n < target*.99:
        status, reason = "INCONCLUSIVE", "capture coverage below 99%"
    else:
        status, reason = "PASS", "receiver observed expected DSCP"
    return dict(status=status, reason=reason, expected=expected, packets=n, target=target, dscp=marks)


def assess(cases, sender, endpoints, egress_mbps, link_valid=True, host_drops=0):
    sent = {(r["Case"], r["Phase"]): r for r in sender.get("Sent", [])}
    wire = {(r["Case"], r["Phase"]): {"packets": r["Packets"], "dscp": r["DSCP"]}
            for r in sender.get("Received", [])}
    results = []
    for case in cases:
        rows = {(r["case"], r["phase"]): r for r in (endpoints.get(case["Role"], {}).get("samples") or [])}
        phases = []
        for phase in (0, 1):
            key = case["Id"], phase
            tx, rx = sent.get(key, {}), rows.get(key, {})
            n, replies = tx.get("Packets", 0), tx.get("Replies", 0)
            minimum = 1 if phase == 0 else 100
            # Main bulk traffic intentionally has no replies. Classification is
            # checked on survivors; loss is evaluated only in the contention gate.
            target = rx.get("packets", 0) if case["Bulk"] else n
            forward = observation(case["ExpectedDSCP"], rx, target, minimum=minimum,
                                  socket_drops=rx.get("socket_drops", 0))
            reverse = None
            if not case["Bulk"]:
                reverse = observation(case["ExpectedDSCP"], wire.get(key, {}), replies,
                                      minimum=minimum)
            transport = "PASS"
            if tx.get("Errors", 0):
                transport = "ERROR"
            elif not n:
                transport = "INCONCLUSIVE"
            elif not case["Bulk"] and replies < n*.99:
                transport = "INCONCLUSIVE" if rx.get("socket_drops", 0) else "FAIL"
            timings = sorted(tx.get("RTTms", []))
            phases.append(dict(phase=phase, sent=n, replies=replies, received=rx.get("packets", 0),
                               socket_drops=rx.get("socket_drops", 0),
                               tx_mbps=tx.get("Bytes", 0)*8/case["Seconds"]/1e6 if phase else None,
                               rx_mbps=rx.get("bytes", 0)*8/case["Seconds"]/1e6 if phase else None,
                               delivery=rx.get("packets", 0)/n if n else 0,
                               rtt_p95_ms=timings[min(len(timings)-1, int(len(timings)*.95))] if timings else None,
                               forward=forward, reverse=reverse, transport=transport,
                               status=worst([forward["status"], reverse["status"] if reverse else "PASS", transport])))
        results.append(dict(case=case["Name"], role=case["Role"], group=case["Group"],
                            status=worst([r["status"] for r in phases]), phases=phases))
    contention = []
    for group in sorted({r["group"] for r in results if r["group"]}):
        pair = [r for r in results if r["group"] == group]
        hi, lo = [r["phases"][1] for r in pair]
        capacity = egress_mbps*1400/(1400+66)
        offered = hi["tx_mbps"]+lo["tx_mbps"]
        received = hi["rx_mbps"]+lo["rx_mbps"]
        valid = (link_valid and not host_drops and not any(r["socket_drops"] for r in (hi, lo))
                 and all(r["status"] == "PASS" for r in (hi, lo))
                 and all(.59*egress_mbps <= r["tx_mbps"] <= .70*egress_mbps for r in (hi, lo))
                 and offered > capacity*1.15 and capacity*.90 < received < capacity*1.07)
        passed = hi["delivery"] >= .95 and hi["delivery"]-lo["delivery"] >= .20
        contention.append(dict(group=group, status=("PASS" if passed else "FAIL") if valid else "INCONCLUSIVE",
                               higher=pair[0]["role"], lower=pair[1]["role"], offered_mbps=offered, received_mbps=received,
                               delivery=[hi["delivery"], lo["delivery"]], payload_capacity_mbps=capacity,
                               reason="higher tier protected" if valid and passed else "higher tier not protected" if valid
                               else "requires correct DSCP, balanced sufficient offered load, saturated output and no receiver drops"))
    return results, contention


class Runner:
    def __init__(self, args):
        self.args = args
        self.config_path = Path(args.config).expanduser().resolve()
        self.c = json.loads(self.config_path.read_text())
        validate(self.c)
        self.id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8]
        self.remote = "/tmp/hqs_"+self.id.lower()
        # Keep filter names shorter than pktmon's formatted list column.
        self.capture_filter = "hqs_"+self.id[-8:]
        self.out = Path(args.output or ROOT / "logs/hg-blackbox/scenarios" / self.id).resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.source = 48000+int(uuid.uuid4().hex[:4], 16) % 10000
        self.port = 41000+int(uuid.uuid4().hex[:4], 16) % 5000
        self.nonce = int(uuid.uuid4().hex[:15], 16)
        self.cases = make_cases(self.c, self.source, self.port, args.congestion)
        self.password = None
        self.locked = self.deployed = False
        self.pktmon_setup = False
        self.cleanup_errors = []
        self.results, self.contention = [], []
        self.save("config.json", self.c)
        self.save("cases.json", self.cases)
        (self.out / "runner.py").write_bytes(Path(__file__).read_bytes())
        (self.out / "hg_qos_common.py").write_bytes((HERE / "helpers/hg_qos_common.py").read_bytes())
        (self.out / "hg_qos_pcap.py").write_bytes((HERE / "helpers/hg_qos_pcap.py").read_bytes())
        (self.out / "probe-source").mkdir()
        for p in (HERE / "helpers/hg-qos-scenario-probe").glob("*"):
            if p.is_file():
                (self.out / "probe-source" / p.name).write_bytes(p.read_bytes())

    def save(self, name, obj):
        (self.out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+"\n")

    def ssh(self, host):
        h = self.c[host]
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
        if h.get("known_hosts"):
            p = Path(h["known_hosts"]).expanduser()
            cmd += ["-o", "UserKnownHostsFile="+str((p if p.is_absolute() else self.config_path.parent/p).resolve())]
        return cmd+[h["ssh"]]

    def command(self, cmd, label=None, data=None, timeout=30, check=True):
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        if label:
            self.save(label+".json", dict(command=cmd, returncode=r.returncode, stdout=r.stdout.decode(errors="replace"),
                                          stderr=r.stderr.decode(errors="replace")))
        if check and r.returncode:
            raise RuntimeError(f"{label or cmd[0]} failed: {r.stderr.decode(errors='replace')[:800]}")
        return r

    def shell(self, host, script, label=None, **kwargs):
        return self.command(self.ssh(host)+["sh -c "+shlex.quote(script)], label, **kwargs)

    def sudo(self, script, label=None, **kwargs):
        opt = "-S -p ''" if self.password is not None else "-n"
        # Password is only stdin; never part of command, environment or evidence.
        return self.command(self.ssh("fnos")+[f"sudo {opt} sh -c "+shlex.quote(script)], label,
                            data=self.password, **kwargs)

    def ps(self, script):
        prefix = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        return self.ssh("win")+["powershell.exe -NoProfile -NonInteractive -EncodedCommand "+
                                base64.b64encode((prefix+script).encode("utf-16le")).decode()]

    def snapshot(self, label):
        c = self.c
        return self.shell("hg", f'''ubus call system board
uname -r
cat /sys/class/net/{c['input_device']}/speed /sys/class/net/{c['output_device']}/speed
cat /sys/kernel/debug/ppe/bind 2>/dev/null
ubus call network.interface.wan status
ubus call network.interface.iptv status
''', label, check=False)

    def prepare(self):
        qos = self.shell("hg", "airoha-qosctl get " + shlex.quote(self.c.get("qos_device", "lan1")),
                         "qos-profile", check=False)
        self.qos_profile = check_qos_profile(qos.stdout, self.c.get("expected_qos"))
        self.save("qos-profile-check.json", self.qos_profile)
        if self.args.sudo_password_file:
            p = Path(self.args.sudo_password_file)
            if p.stat().st_mode & 0o077:
                raise ValueError("sudo password file must be mode 0600 or stricter")
            self.password = p.read_bytes().rstrip(b"\r\n")+b"\n"
        elif self.args.ask_sudo_password:
            self.password = (getpass.getpass("fnOS sudo password: ")+"\n").encode()
        self.sudo("test \"$(id -u)\" = 0; command -v nsenter; command -v docker", "sudo-preflight")
        self.snapshot("hg-before")
        chains = json.loads(self.shell("hg", "nft -j list chains", "chains").stdout)
        self.save("hook-order.json", check_hook_order(chains))
        self.shell("hg", "nft -j list ruleset", "rules-before")
        speeds = self.shell("hg", f'cat /sys/class/net/{self.c["input_device"]}/speed /sys/class/net/{self.c["output_device"]}/speed').stdout.split()
        self.link_valid = len(speeds) == 2 and int(speeds[0]) > int(speeds[1]) == self.c["egress_mbps"]
        if self.args.congestion and not self.link_valid:
            raise RuntimeError("Contention requires confirmed faster input and configured output speed")
        self.prefixes = {}
        for role in ROLES:
            e = self.c["endpoints"][role]
            prefix = ""
            if e.get("container"):
                pid = int(self.sudo("docker inspect -f '{{.State.Pid}}' "+e["container"]).stdout)
                if pid <= 1:
                    raise RuntimeError("Endpoint container is not running: "+role)
                prefix = f"nsenter -t {pid} -n "
            addrs = json.loads(self.sudo(prefix+"ip -j -4 addr", "endpoint-"+role).stdout)
            if e["ip"] not in {a["local"] for d in addrs for a in d.get("addr_info", [])}:
                raise RuntimeError("Configured address not already present: "+role)
            self.prefixes[role] = prefix
        win = json.loads(self.command(self.ps("@{temp=$env:TEMP; admin=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)}|ConvertTo-Json -Compress"), "win-preflight").stdout.decode("utf-8-sig"))
        if not win["admin"]:
            raise RuntimeError("Windows raw receive observation requires an elevated SSH account")
        self.win_temp = win["temp"].replace("\\", "/").rstrip("/")
        if not re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9_ ./-]+", self.win_temp):
            raise RuntimeError("Unsupported Windows TEMP")
        self.win_file = Path(self.remote).name+".cs"
        helper = HERE / "helpers/hg-qos-scenario-probe"
        r = subprocess.run(["go", "build", "-trimpath", "-o", str(self.out/"probe-linux"), "."], cwd=helper,
                           env=dict(os.environ, GO111MODULE="off", CGO_ENABLED="0", GOARCH="amd64", GOOS="linux"),
                           capture_output=True, timeout=90)
        if r.returncode:
            raise RuntimeError(r.stderr.decode())
        self.save("hashes.json", {"probe-linux": hashlib.sha256((self.out/"probe-linux").read_bytes()).hexdigest()})
        timer = f'sleep 210; if [ "$(cat {LOCK}/owner 2>/dev/null)" = "{self.id}" ]; then rm -f {LOCK}/owner {LOCK}/watchdog.pid; rmdir {LOCK}; fi'
        self.locked = True
        self.shell("hg", f'''set -e
mkdir {LOCK}
echo {self.id} > {LOCK}/owner
nohup sh -c {shlex.quote(timer)} </dev/null >/dev/null 2>&1 &
echo $! > {LOCK}/watchdog.pid
''', "lock")
        self.deployed = True
        self.shell("fnos", f"mkdir -m 700 {self.remote}", "create-probe-dir")
        self.command(self.ssh("fnos")+[f"cat > {self.remote}/probe && chmod 700 {self.remote}/probe"],
                     "deploy-linux", data=(self.out/"probe-linux").read_bytes())
        self.command(["scp", "-q"]+self.ssh("win")[2:-1]+[str(helper/"client.cs"), self.c["win"]["ssh"]+":"+self.win_temp+"/"+self.win_file], "deploy-win")
        fields = ("Id", "SourcePort", "Port", "Size", "Group", "Name", "Address", "Protocol", "RateMbps", "Seconds", "Bulk")
        self.save("client-cases.json", [{k: c[k] for k in fields} for c in self.cases])
        self.command(["scp", "-q"]+self.ssh("win")[2:-1]+[str(self.out/"client-cases.json"), self.c["win"]["ssh"]+":"+self.win_temp+"/"+self.win_file+".json"], "deploy-win-cases")
        # Each listener is on a unique, high test port and exits at 180 seconds.
        for role in ROLES:
            e = self.c["endpoints"][role]
            cmd = f"{self.prefixes[role]}{self.remote}/probe -addr {e['ip']} -port {self.port} -iface {e['interface']} -nonce {self.nonce} -lifetime 180"
            self.sudo(f"nohup {cmd} </dev/null >{self.remote}/{role}.out 2>{self.remote}/{role}.err & echo $! >{self.remote}/{role}.pid", "start-"+role)
        for role in ROLES:
            ready = False
            for _ in range(20):
                out = self.sudo(f"head -n 1 {self.remote}/{role}.out; cat {self.remote}/{role}.err", check=False).stdout
                if out.strip() == b"READY":
                    ready = True
                    break
                time.sleep(.1)
            if not ready:
                raise RuntimeError("Probe did not bind: "+role+" "+out.decode(errors="replace")[:500])

    def counters(self, label):
        # Each namespace has separate UDP counters; socket counters are also
        # recorded by every endpoint. Shared namespace rows deliberately repeat.
        result = {}
        for role in ROLES:
            e = self.c["endpoints"][role]
            # Netlink follows the selected network namespace. The host sysfs
            # mount does not change with nsenter -n and must not be used here.
            script = f"cat /proc/net/snmp; ip -s -j link show dev {e['interface']}"
            raw = self.sudo(self.prefixes[role]+"sh -c "+shlex.quote(script)).stdout.decode().splitlines()
            link = json.loads(raw[-1])[0]
            stats = link.get("stats64", link.get("stats", {}))["rx"]
            row = {"interface_drops": stats["dropped"]+stats["errors"]}
            for i, line in enumerate(raw[:-1]):
                if line.startswith("Udp:") and "InDatagrams" in line:
                    row.update(dict(zip(line.split()[1:], map(int, raw[i+1].split()[1:]))))
            result[role] = row
        self.save(label+".json", result)
        return result

    def capture_cleanup_ps(self):
        name = Path(self.remote).name
        # Only stop a session whose unique output file proves ownership.
        return rf'''$s=(& pktmon status|Out-String)
if($s.Contains('{name}.etl')){{ & pktmon stop|Out-Null }}
$f=(& pktmon filter list|Out-String)
$foreign=$false
if($f.Contains('{self.capture_filter}')){{
    # This Windows version can only remove ALL filters. Do so only when the
    # sole filter is ours; never erase filters someone else added meanwhile.
    if([regex]::Matches($f,'(?m)^\s*\d+\s+').Count -ne 1){{$foreign=$true}}
    else {{ & pktmon filter remove|Out-Null }}
}}
if(Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue){{Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false}}
if($foreign){{throw 'Other pktmon filters appeared; preserved all filters'}}
'''

    def capture_start(self):
        name = Path(self.remote).name
        cleanup = base64.b64encode(self.capture_cleanup_ps().encode("utf-16le")).decode()
        self.pktmon_setup = True
        # Keep each encoded command below cmd.exe's command-line limit.
        script = rf'''$s=(& pktmon status|Out-String)
if($s -notmatch '(?i)not running|没有运行'){{throw 'pktmon is already running or status is unknown'}}
$f=(& pktmon filter list|Out-String)
if($f -notmatch '(?im)^\s*(None|无)\s*$'){{throw 'pktmon has existing filters; leave them intact'}}
$nic=Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{self.c['win_ip']}'|Get-NetAdapter
$groups=(& pktmon list --json|Out-String)|ConvertFrom-Json
$components=@($groups|ForEach-Object {{$_.Components}}|Where-Object {{$_.Name -eq $nic.InterfaceDescription}})
if($components.Count -gt 1){{
    # NetAdapterCx exposes both a hardware driver and an NDIS wrapper. Capture
    # the adapter's NDIS boundary (ifGuid), where received frames are visible.
    $ndis=@($components|Where-Object {{@($_.Properties|Where-Object {{$_.Name -eq 'ifGuid'}}).Count -eq 1}})
    if($ndis.Count -eq 1){{$components=$ndis}}
}}
if($components.Count -ne 1){{throw 'Cannot identify unique receiving NIC component'}}
@{{component=$components[0].Id;nic=$nic.Name;mac=$nic.MacAddress}}|ConvertTo-Json -Compress
'''
        info = json.loads(self.command(self.ps(script), "pktmon-preflight").stdout.decode("utf-8-sig"))
        script = f'''
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -NonInteractive -EncodedCommand {cleanup}'
$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(180)
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName '{name}' -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest|Out-Null
'''
        self.command(self.ps(script), "pktmon-watchdog")
        script = f'''
& pktmon filter add '{self.capture_filter}' -i '{self.c['win_ip']}' -t TCP -p {self.port}|Out-Null
if($LASTEXITCODE){{throw 'pktmon filter failed'}}
# Keep the normal 512 MiB file limit. A 16 MiB limit on this host (ETW buffer
# size 16 MiB) produced a metadata-only file despite positive packet counters.
& pktmon start --capture --comp {int(info['component'])} --pkt-size 128 --file-size 512 --file-name (Join-Path $env:TEMP '{name}.etl')|Out-Null
if($LASTEXITCODE){{throw 'pktmon capture failed'}}
pktmon status
'''
        self.command(self.ps(script), "pktmon-start")

    def capture_finish(self):
        name = Path(self.remote).name
        script = f'''$s=(& pktmon status|Out-String)
if(-not $s.Contains('{name}.etl')){{throw 'Owned packet capture is missing'}}
pktmon counters
& pktmon stop
if($LASTEXITCODE){{throw 'pktmon stop failed'}}
& pktmon etl2pcap (Join-Path $env:TEMP '{name}.etl') --out (Join-Path $env:TEMP '{name}.pcapng')|Out-Null
if($LASTEXITCODE){{throw 'pktmon conversion failed'}}
& pktmon etl2pcap (Join-Path $env:TEMP '{name}.etl') --drop-only --out (Join-Path $env:TEMP '{name}.drop.pcapng')|Out-Null
if($LASTEXITCODE){{throw 'pktmon drop conversion failed'}}
& pktmon etl2txt (Join-Path $env:TEMP '{name}.etl') --out (Join-Path $env:TEMP '{name}.txt')|Out-Null
{self.capture_cleanup_ps()}
'''
        self.command(self.ps(script), "pktmon-finish")
        for suffix in ("pcapng", "drop.pcapng", "etl", "txt"):
            self.command(["scp", "-q"]+self.ssh("win")[2:-1]+[self.c["win"]["ssh"]+":"+self.win_temp+"/"+name+"."+suffix, str(self.out/("win-tcp."+suffix))], "collect-win-"+suffix)
        rows = tcp_observations((self.out/"win-tcp.pcapng").read_bytes()+(self.out/"win-tcp.drop.pcapng").read_bytes(), self.cases, self.nonce, self.c["win_ip"])
        self.save("tcp-received.json", rows)
        return rows

    def stop_probes(self):
        # Check executable identity before signalling a saved PID.
        self.sudo(f'''set -e
pids=""
for role in {' '.join(ROLES)}; do
    if [ -f {self.remote}/$role.pid ]; then
        p=$(cat {self.remote}/$role.pid)
        if [ "$(readlink /proc/$p/exe 2>/dev/null)" = "{self.remote}/probe" ]; then
            kill -TERM "$p"; pids="$pids $p"
        fi
    fi
done
i=0
while [ "$i" -lt 120 ]; do
    alive=""
    for p in $pids; do
        if [ "$(readlink /proc/$p/exe 2>/dev/null)" = "{self.remote}/probe" ]; then alive="$alive $p"; fi
    done
    [ -n "$alive" ] || exit 0
    sleep 0.1
    i=$((i+1))
done
for p in $alive; do
    if [ "$(readlink /proc/$p/exe 2>/dev/null)" = "{self.remote}/probe" ]; then kill -KILL "$p"; fi
done
echo 'Test probe needed forced cleanup; its result may be incomplete' >&2
exit 1
''', "stop-probes")

    def run(self):
        before = self.counters("receiver-before")
        self.capture_start()
        script = f'''Add-Type -Path (Join-Path $env:TEMP '{self.win_file}');
$items=Get-Content -Raw -LiteralPath (Join-Path $env:TEMP '{self.win_file}.json')|ConvertFrom-Json;
$cases=[HgQoSScenario.Case[]]@($items | ForEach-Object {{ $c=New-Object HgQoSScenario.Case; foreach($p in $_.PSObject.Properties){{$c.($p.Name)=$p.Value}}; $c }});
[HgQoSScenario.Client]::Run('{self.c['win_ip']}',$cases,[uint64]{self.nonce})|ConvertTo-Json -Depth 12 -Compress;exit 0
'''
        print(f"Running {len(self.cases)} production-policy scenarios (bounded to 140 s)", flush=True)
        r = self.command(self.ps(script), "sender", timeout=140)
        sender = json.loads(r.stdout.decode("utf-8-sig"))
        tcp_rows = self.capture_finish()
        tcp_ids = {c["Id"] for c in self.cases if c["Protocol"] == "tcp"}
        sender["Received"] = [r for r in sender["Received"] if r["Case"] not in tcp_ids]+tcp_rows
        self.save("sender-with-tcp-capture.json", sender)
        self.snapshot("hg-after-traffic")
        after = self.counters("receiver-after")
        self.stop_probes()
        endpoints = {}
        for role in ROLES:
            for _ in range(30):
                r = self.sudo(f"cat {self.remote}/{role}.out; cat {self.remote}/{role}.err >&2", "receiver-"+role)
                lines = r.stdout.decode().splitlines()
                if len(lines) >= 2:
                    break
                time.sleep(.1)
            endpoints[role] = json.loads(lines[-1])
        host_drops = sum(max(0, after[role].get(k, 0)-before[role].get(k, 0))
                         for role in ROLES for k in ("InErrors", "RcvbufErrors", "interface_drops"))
        self.results, self.contention = assess(self.cases, sender, endpoints, self.c["egress_mbps"], self.link_valid, host_drops)
        errors = sender.get("Errors", [])+[e for r in endpoints.values() for e in (r.get("errors") or [])]
        if errors:
            raise RuntimeError("Probe errors: "+repr(errors))

    def cleanup(self):
        if self.pktmon_setup:
            try:
                name = Path(self.remote).name
                script = self.capture_cleanup_ps()+f"\nforeach($n in @('{name}.etl','{name}.pcapng','{name}.drop.pcapng','{name}.txt')){{$p=Join-Path $env:TEMP $n;if(Test-Path -LiteralPath $p){{Remove-Item -LiteralPath $p -Force}}}}"
                self.command(self.ps(script), "cleanup-pktmon")
            except Exception as e:
                self.cleanup_errors.append("pktmon: "+str(e)+"; independent scheduled cleanup at 180 s")
        if self.deployed:
            for label, fn in (("probes", self.stop_probes),
                              ("fnos-files", lambda: self.sudo(f"rm -f {self.remote}/*.pid {self.remote}/*.out {self.remote}/*.err {self.remote}/probe; rmdir {self.remote}", "cleanup-fnos")),
                              ("win-file", lambda: self.command(self.ps(f"foreach($n in @('{self.win_file}','{self.win_file}.json')){{$p=Join-Path $env:TEMP $n;if(Test-Path -LiteralPath $p){{Remove-Item -LiteralPath $p -Force}}}};exit 0"), "cleanup-win"))):
                try:
                    fn()
                except Exception as e:
                    self.cleanup_errors.append(label+": "+str(e))
        if self.locked:
            try:
                self.shell("hg", f'''if [ "$(cat {LOCK}/owner 2>/dev/null)" = "{self.id}" ]; then
if [ -f {LOCK}/watchdog.pid ]; then kill "$(cat {LOCK}/watchdog.pid)" 2>/dev/null || true; fi
rm -f {LOCK}/owner {LOCK}/watchdog.pid
rmdir {LOCK}
fi''', "cleanup-hg")
            except Exception as e:
                self.cleanup_errors.append(str(e))
        self.password = None

    def report(self, error=None):
        statuses = [r["status"] for r in self.results+self.contention]
        overall = worst(statuses+["ERROR"] if error or self.cleanup_errors else statuses or ["INCONCLUSIVE"])
        summary = dict(schema_version=1, run=self.id, status=overall, error=error,
                       qos_profile=getattr(self, "qos_profile", None), cleanup_errors=self.cleanup_errors,
                       policy=self.results, contention=self.contention, congestion_requested=self.args.congestion,
                       coverage="IPv4, actual existing identities, one configured LAN path, Win-initiated TCP/UDP and replies; Fake-IP route to local test listener",
                       uncovered=["IPv6", "VLAN/PCP matrix and isolation", "PON/WAN", "AP/WMM", "IPTV/multicast", "application semantics",
                                  "fnOS-initiated connections", "internal skb priority and exact hardware queue"])
        self.save("summary.json", summary)
        lines = [f"# HG production QoS scenarios: {overall}", "", f"Run: `{self.id}`", "",
                 "No classification, NAT, flowtable or shaping rules were added by this suite.", "",
                 "| Case | Result | Cold forward / reverse DSCP | Main forward / reverse DSCP | Main echo replies / sent |", "|---|---|---|---|---|"]
        for r in self.results:
            cold, p = r["phases"]
            cold_marks = f"{cold['forward']['dscp']} / {cold['reverse']['dscp'] if cold['reverse'] else 'one-way'}"
            main_marks = f"{p['forward']['dscp']} / {p['reverse']['dscp'] if p['reverse'] else 'one-way bulk'}"
            lines.append(f"| {r['case']} | {r['status']} | {cold_marks} | {main_marks} | {p['replies']} / {p['sent']} |")
        lines += ["", "Both cold/warmup and steady phases are checked. A passing DSCP observation does not establish queue scheduling."]
        for r in self.contention:
            lines += ["", f"Contention {r['higher']} > {r['lower']}: **{r['status']}** — {r['reason']}; offered {r['offered_mbps']:.1f}, received {r['received_mbps']:.1f} Mbps."]
        if not self.contention:
            lines += ["", "Contention NOT RUN. Queue protection remains unverified by this run."]
        lines += ["", "Uncovered: "+", ".join(summary["uncovered"])+".", ""]
        if error or self.cleanup_errors:
            lines += ["Error: "+str(error), "Cleanup errors: "+repr(self.cleanup_errors), ""]
        (self.out/"REPORT.md").write_text("\n".join(lines))
        print(f"{overall}: {self.out/'REPORT.md'}", flush=True)
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}[overall]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Site JSON; defaults to local/scenarios.json next to this script")
    p.add_argument("--output", help="New evidence directory")
    p.add_argument("--dry-run", action="store_true", help="Write the case plan only; no SSH")
    p.add_argument("--congestion", action="store_true", help="Also run two 2.5 s pairs on the shared physical output; may briefly increase latency")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sudo-password-file", help="0600 file, read only for sudo stdin, never archived")
    g.add_argument("--ask-sudo-password", action="store_true", help="Read sudo password without echo; otherwise use sudo -n")
    args = p.parse_args()
    if not Path(args.config).expanduser().is_file():
        p.error("Missing site configuration: copy configs/scenarios.example.json to local/scenarios.json, edit it, or pass --config PATH")
    r = Runner(args)
    if args.dry_run:
        print(r.out)
        return 0
    error = None
    try:
        r.prepare()
        r.run()
    except (Exception, KeyboardInterrupt) as e:
        error = str(e) or type(e).__name__
    finally:
        r.cleanup()
    return r.report(error)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
