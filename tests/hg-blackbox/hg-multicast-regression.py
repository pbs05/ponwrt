#!/usr/bin/env python3
"""Regress prepared IPv4 routed multicast with external Linux endpoints.

No router routes, firewall rules, VLANs or offload controls are changed. The
explicit --run option sends bounded traffic and temporarily joins test groups.
"""
import argparse
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

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROBE = HERE / "helpers/hg_multicast_probe.py"
LOCK = "/tmp/hg-qos-regression.lock"
NOT_COVERED = [
    "PON ingress/VID/GEM lookup isolation (requires a second prepared ingress)",
    "CPU-copy descriptor cause-code/header format beyond application observations",
    "IGMPv3 source-filter transitions, querier expiry and PON reconnect",
    "IPIP/PIM register paths, IPv6 and mixed tagged/untagged replication",
    "Hardware aging, hash reuse, write failure and driver teardown",
    "GRO formation, throughput, CPU cost and congestion scheduling",
]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load_config(path):
    c = json.loads(path.read_text())
    def integer(name, default, low, high):
        value = c.setdefault(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be {low}..{high}")
    integer("port", 55000, 1024, 65535)
    integer("source_port", 55001, 1024, 65535)
    integer("pps", 400, 1, 2000)
    integer("count", 1200, 10, 10000)
    integer("payload", 1400, 64, 8972)
    integer("settle_seconds", 10, 1, 60)
    integer("dscp", 7, 0, 63)
    integer("ecn", 2, 0, 3)
    integer("ttl", 8, 2, 255)
    integer("queue", 4, 0, 7)
    if c["count"] / c["pps"] > 30:
        raise ValueError("each measurement must finish within 30 seconds")
    if c["pps"] * (c["payload"] + 28) * 8 * 2 > 40_000_000:
        raise ValueError("two-stream traffic must remain below 40 Mbit/s")
    group = ipaddress.IPv4Address(c["group"])
    alias = ipaddress.IPv4Address(c["alias_group"])
    if not group.is_multicast or not alias.is_multicast or group == alias:
        raise ValueError("two distinct multicast groups required")
    if int(group) & 0x7FFFFF != int(alias) & 0x7FFFFF:
        raise ValueError("alias_group must share the primary group's Ethernet MAC")
    for group_address in (group, alias):
        if group_address not in ipaddress.IPv4Network("239.0.0.0/8"):
            raise ValueError("use administratively scoped test groups in 239.0.0.0/8")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", c["input_device"]):
        raise ValueError("invalid HG input_device")
    hosts = [c["hg"], c["sender"]] + c["receivers"]
    if not 2 <= len(c["receivers"]) <= 6:
        raise ValueError("two to six receiver interfaces required")
    names = set()
    bits = set()
    for host in hosts:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", host["ssh"]):
            raise ValueError("use a plain SSH alias or user@host")
        if type(host.get("sudo", False)) is not bool or type(host.get("member", True)) is not bool:
            raise ValueError("sudo and member must be booleans")
        if "known_hosts" in host:
            host["known_hosts"] = str((path.parent / host["known_hosts"]).resolve())
        if host is c["hg"]:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", host["interface"]):
            raise ValueError("invalid endpoint interface")
        ip = ipaddress.IPv4Address(host["address"])
        if ip.is_multicast or ip.is_unspecified or ip.is_loopback:
            raise ValueError("endpoint needs a unicast data address")
        if host is c["sender"]:
            continue
        name = host["name"]
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name in names or name == "sender":
            raise ValueError("receiver names must be distinct simple names")
        names.add(name)
        if host.get("member", True):
            bit = host["ppe_bit"]
            if type(bit) is not int or not 0 <= bit <= 15 or bit in bits:
                raise ValueError("member PPE bits must be distinct (one receiver per HG port)")
            bits.add(bit)
        if type(host.get("expected_dscp", c["dscp"])) is not int or not 0 <= host.get("expected_dscp", c["dscp"]) <= 63:
            raise ValueError("expected_dscp must be 0..63")
        if "expected_vlans" in host and (not isinstance(host["expected_vlans"], list) or
                len(host["expected_vlans"]) > 2 or any(type(v) is not int or not 0 <= v <= 4094 for v in host["expected_vlans"])):
            raise ValueError("expected_vlans must be a list of up to two VLAN IDs")
    members = [h for h in c["receivers"] if h.get("member", True)]
    if len(members) != 2:
        raise ValueError("exactly two subscribing receiver ports required")
    c.setdefault("require_hardware", True)
    if type(c["require_hardware"]) is not bool:
        raise ValueError("require_hardware must be boolean")
    if "local_probe" in c and not re.fullmatch(r"/tmp/[A-Za-z0-9_.-]+", c["local_probe"]):
        raise ValueError("local_probe must be a preinstalled executable under /tmp")
    return c


def ssh(host):
    result = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
    if host.get("known_hosts"):
        result += ["-o", "UserKnownHostsFile=" + host["known_hosts"]]
    return result + [host["ssh"]]


class Peer:
    def __init__(self, host, cfg, out):
        self.stderr = (out / (host.get("name", "sender") + "-stderr.txt")).open("wb")
        command = (["sudo", "-n"] if host.get("sudo", False) else []) + [
            "python3", "-u", "-c", PROBE.read_text(), json.dumps(cfg)]
        try:
            self.proc = subprocess.Popen(ssh(host) + [shlex.join(command)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self.stderr)
        except BaseException:
            self.stderr.close()
            raise
        self.buffer, self.serial = b"", 0
        try:
            self.ready = self.receive(20)
            if not self.ready.get("ready"):
                raise RuntimeError("endpoint did not become ready")
            if self.ready.get("carrier") != "1":
                raise RuntimeError("endpoint data interface has no carrier")
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                raise TimeoutError("endpoint response timeout")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("endpoint exited; inspect its stderr artifact")
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise RuntimeError("endpoint response limit exceeded")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def request(self, op, timeout=15, **kwargs):
        self.serial += 1
        self.proc.stdin.write((json.dumps(dict(kwargs, id=self.serial, op=op)) + "\n").encode())
        self.proc.stdin.flush()
        reply = self.receive(timeout)
        if reply.get("id") != self.serial or "error" in reply:
            raise RuntimeError(f"{op}: {reply}")
        return reply

    def close(self):
        try:
            self.proc.stdin.close()  # EOF closes UDP membership and capture sockets.
            self.proc.wait(timeout=8)
            return self.proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            return False
        finally:
            self.stderr.close()


class LocalPeer(Peer):
    """Preinstalled native HG helper; does not put any HG interface in promiscuous mode."""
    def __init__(self, c, token, out):
        self.stderr = (out / "hg-local-stderr.txt").open("wb")
        command = [c["local_probe"], c["input_device"], c["group"], str(c["port"]),
                   c["sender"]["address"], str(c["source_port"]), token]
        self.proc = subprocess.Popen(ssh(c["hg"]) + [shlex.join(command)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr)
        self.buffer, self.serial = b"", 0
        try:
            self.ready = self.receive(20)
            if not self.ready.get("ready"):
                raise RuntimeError("HG native local probe unavailable")
        except BaseException:
            self.close()
            raise

    def request(self, op, timeout=15, **kwargs):
        if op not in ("join", "leave", "clear", "collect"):
            raise ValueError("invalid local command")
        command = op + (" " + str(int(kwargs["phase"])) if op == "collect" else "")
        self.proc.stdin.write((command + "\n").encode())
        self.proc.stdin.flush()
        result = self.receive(timeout)
        if "error" in result:
            raise RuntimeError(str(result))
        return result


def evaluate_local(c, phase, sent, data):
    if data.get("limited") or data.get("udp_drops"):
        return dict(name="hg-local", status="INCONCLUSIVE", detail=data)
    if not phase.get("local", False):
        good = not data.get("packets", 0)
    else:
        good = (data.get("sequences") == list(range(sent["sent"][c["group"]])) and
                data.get("duplicates") == 0 and data.get("invalid_metadata") == 0 and
                set(data.get("ttl", {})) == {str(phase.get("ttl", c["ttl"]))} and
                set(data.get("dscp", {})) == {str(c["dscp"])} and
                set(data.get("ecn", {})) == {str(c["ecn"])} and
                set(data.get("lengths", {})) == {str(c["payload"] + 28)})
    return dict(name="hg-local", status="PASS" if good else "FAIL", detail=data)


def router_command(c, command, timeout=15):
    r = subprocess.run(ssh(c["hg"]) + [command], capture_output=True, timeout=timeout)
    return {"rc": r.returncode, "stdout": r.stdout.decode(errors="replace"),
            "stderr": r.stderr.decode(errors="replace")}


def snapshot(c):
    commands = {"backend": "cat /sys/kernel/debug/ppe/multicast",
                "policy": "airoha-mcastctl get",
                "routes": "cat /proc/net/ip_mr_cache", "members": "bridge -d mdb show",
                "input_index": "cat /sys/class/net/" + c["input_device"] + "/ifindex",
                "ip_stats": "cat /proc/net/snmp"}
    return {key: router_command(c, command) for key, command in commands.items()}


def phases(c):
    a, b = [h["name"] for h in c["receivers"] if h.get("member", True)]
    result = [dict(name="first-member", members=[a]),
            dict(name="both-members", members=[a, b]),
            dict(name="ttl-one", members=[a, b], ttl=1),
            dict(name="first-left", members=[b]),
            dict(name="all-left", members=[])]
    if c.get("local_probe"):
        result += [dict(name="local-and-remote", members=[a, b], local=True),
                   dict(name="local-ttl-one", members=[a, b], local=True, ttl=1),
                   dict(name="local-only", members=[], local=True),
                   dict(name="local-left", members=[], local=False)]
    return result


def evaluate(c, phase, sent, received, state):
    checks = []
    def check(name, result, detail):
        checks.append(dict(name=name, status=result, detail=detail))
    for host in c["receivers"]:
        name = host["name"]
        record = received[name]
        valid = not (record["capture_drops"] or record["udp_drops"] or record["limited"])
        check(name + "/capture", "PASS" if valid else "INCONCLUSIVE", "socket drops and observation limits")
        rows = {(r["group"], r["kind"]): r for r in record["rows"]}
        unexpected = [r for r in record["rows"] if r["group"] not in (c["group"], c["alias_group"])]
        check(name + "/unexpected-group", "FAIL" if unexpected else "PASS" if valid else "INCONCLUSIVE",
              [{"group": r["group"], "kind": r["kind"], "packets": r["packets"]} for r in unexpected])
        for group in (c["group"], c["alias_group"]):
            expected = name in phase["members"] and group == c["group"] and phase.get("ttl", c["ttl"]) > 1
            app, wire = rows.get((group, "app"), {}), rows.get((group, "wire"), {})
            label = name + "/" + group
            if not expected:
                leaked = app.get("packets", 0) + wire.get("packets", 0)
                check(label + "/isolation", "FAIL" if leaked else "PASS" if valid else "INCONCLUSIVE", {"observed": leaked})
                continue
            count = sent["sent"][group]
            seq = app.get("sequences", [])
            good = seq == list(range(count)) and app.get("duplicates", 0) == 0
            check(label + "/delivery", "PASS" if good else "FAIL" if valid else "INCONCLUSIVE",
                  {"sent": count, "received": len(seq), "duplicates": app.get("duplicates", 0)})
            expected_ttl = phase.get("ttl", c["ttl"]) - 1
            expected_tuple = f"{c['sender']['address']}:{c['source_port']}->{group}:{c['port']}"
            marked = (bool(app.get("packets")) and set(app.get("dscp", {})) == {str(host.get("expected_dscp", c["dscp"]))}
                      and set(app.get("ecn", {})) == {str(c["ecn"])} and set(app.get("ttl", {})) == {str(expected_ttl)}
                      and set(app.get("tuples", {})) == {expected_tuple}
                      and set(app.get("lengths", {})) == {str(c["payload"] + 28)})
            check(label + "/headers", "PASS" if marked else "FAIL" if app.get("packets") else "INCONCLUSIVE",
                  {k: app.get(k, {}) for k in ("dscp", "ecn", "ttl", "tuples", "lengths")})
            wire_good = wire.get("packets", 0) > 0 and not wire.get("duplicates", 0) and not wire.get("fragments", 0)
            check(label + "/wire", "PASS" if wire_good else "FAIL" if wire.get("packets") else "INCONCLUSIVE",
                  {k: wire.get(k, 0) for k in ("packets", "duplicates", "fragments")})
            if "expected_vlans" in host:
                vlans = ",".join(str(v) for v in host["expected_vlans"])
                good = set(wire.get("vlans", {})) == {vlans}
                check(label + "/vlan", "PASS" if good else "FAIL" if wire.get("packets") else "INCONCLUSIVE", wire.get("vlans", {}))
    if c["require_hardware"]:
        text = state["backend"]["stdout"]
        running = re.search(r"requested=1 running=1 broken=0\b", text)
        check("backend-state", "PASS" if running else "INCONCLUSIVE", text.splitlines()[:1])
        pattern = (r"^[0-9a-f]+ BIND iif=(\d+) " + re.escape(c["sender"]["address"]) + ":" + str(c["source_port"])
                   + "->" + re.escape(c["group"]) + ":" + str(c["port"]) + r" outputs=([0-9a-f]+).* queue=(\d+)")
        matches = re.findall(pattern, text, re.M)
        mask = sum(1 << h["ppe_bit"] for h in c["receivers"] if h["name"] in phase["members"])
        expected = (state["input_index"]["stdout"].strip(), mask, c["queue"])
        actual = [(i, int(m, 16), int(q)) for i, m, q in matches]
        if phase["members"]:
            check("hardware-binding", "PASS" if actual == [expected] else "FAIL" if actual else "INCONCLUSIVE",
                  {"expected": expected, "observed": actual})
        else:
            check("hardware-release", "FAIL" if actual else "PASS" if running else "INCONCLUSIVE", {"remaining": actual})
    statuses = {check["status"] for check in checks}
    return {"status": "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS", "checks": checks}


def run(c, out):
    token = uuid.uuid4().hex
    results, peers, cleanup = [], {}, {}
    report = {"status": "ERROR", "phases": results, "cleanup": cleanup,
              "not_covered": NOT_COVERED, "hardware_qualification": "incomplete",
              "require_hardware": c["require_hardware"], "token": token}
    locked = False
    try:
        before = snapshot(c)
        write_json(out / "before.json", before)
        if any(before[key]["rc"] for key in ("routes", "members", "input_index", "ip_stats")):
            raise RuntimeError("router observation prerequisites are unavailable; inspect before.json")
        if c["require_hardware"] and not re.search(r"requested=1 running=1 broken=0\b", before["backend"]["stdout"]):
            report.update(status="INCONCLUSIVE", reason="experimental backend is not running; this tool never enables it")
            return report
        lock = router_command(c, "mkdir " + LOCK + " && printf '%s' " + shlex.quote(token) + " > " + LOCK + "/owner")
        if lock["rc"]:
            raise RuntimeError("HG regression lock unavailable")
        locked = True
        common = {"token": token, "source": c["sender"]["address"], "port": c["port"],
                  "source_port": c["source_port"], "deadline": 900}
        for host in c["receivers"]:
            peers[host["name"]] = Peer(host, dict(common, role="receiver", **{k: host[k] for k in ("address", "interface")}), out)
        peers["sender"] = Peer(c["sender"], dict(common, role="sender", interface=c["sender"]["interface"]), out)
        write_json(out / "endpoints.json", {name: p.ready for name, p in peers.items()})
        if int(peers["sender"].ready["mtu"]) < c["payload"] + 28:
            raise RuntimeError("sender MTU is too small for the configured DF datagrams")
        if c.get("local_probe"):
            peers["hg-local"] = LocalPeer(c, token, out)
        else:
            report["not_covered"] = NOT_COVERED + ["HG application delivery (local_probe not configured)"]
        joined = set()
        local_joined = False
        for index, phase in enumerate(phases(c), 1):
            print(phase["name"], flush=True)
            for name in joined - set(phase["members"]):
                peers[name].request("leave", group=c["group"])
            for name in set(phase["members"]) - joined:
                peers[name].request("join", group=c["group"])
            joined = set(phase["members"])
            if c.get("local_probe") and phase.get("local", False) != local_joined:
                peers["hg-local"].request("join" if phase.get("local", False) else "leave")
                local_joined = phase.get("local", False)
            request = {"groups": [c["group"], c["alias_group"]], "payload": c["payload"],
                       "pps": c["pps"], "dscp": c["dscp"], "ecn": c["ecn"], "df": True}
            # Continue traffic throughout membership settling. Distinct phase IDs
            # exclude late warmup packets without requiring synchronized clocks.
            remaining = c["pps"] * c["settle_seconds"]
            while remaining:
                count = min(remaining, c["pps"] * 5)
                peers["sender"].request("send", timeout=15, phase=index * 2, count=count, ttl=c["ttl"], **request)
                remaining -= count
            for host in c["receivers"]:
                peers[host["name"]].request("clear")
            if c.get("local_probe"):
                peers["hg-local"].request("clear")
            sent = peers["sender"].request("send", timeout=40, phase=index * 2 + 1, count=c["count"],
                                             ttl=phase.get("ttl", c["ttl"]), **request)
            time.sleep(1)
            received = {h["name"]: peers[h["name"]].request("collect", phase=index * 2 + 1) for h in c["receivers"]}
            state = snapshot(c)
            if state["input_index"]["rc"]:
                raise RuntimeError("HG input interface disappeared during the measurement")
            result = dict(phase, sent=sent, received=received, router=state, **evaluate(c, phase, sent, received, state))
            if c.get("local_probe"):
                local = peers["hg-local"].request("collect", phase=index * 2 + 1)
                check = evaluate_local(c, phase, sent, local)
                result["checks"].append(check)
                if check["status"] == "FAIL" or result["status"] == "PASS":
                    result["status"] = check["status"]
            results.append(result)
            write_json(out / (phase["name"] + ".json"), result)
        statuses = {r["status"] for r in results}
        report["status"] = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="ERROR", error=str(exc) or type(exc).__name__)
    finally:
        for name, peer in peers.items():
            cleanup[name] = peer.close()
        if locked:
            command = ('[ "$(cat ' + LOCK + '/owner)" = ' + shlex.quote(token) + ' ] && rm ' + LOCK + '/owner && rmdir ' + LOCK)
            try:
                cleanup["lock"] = router_command(c, command)["rc"] == 0
            except (OSError, subprocess.TimeoutExpired):
                cleanup["lock"] = False
        if any(not ok for ok in cleanup.values()):
            report["status"] = "ERROR"
            report["cleanup_note"] = "Check SSH endpoint processes and HG regression lock; endpoint watchdog is 900 seconds."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="connect, join and send traffic; default only prints the plan")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        c = load_config(args.config.resolve())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if not args.run:
        print(json.dumps({"phases": phases(c), "groups": [c["group"], c["alias_group"]],
                          "require_hardware": c["require_hardware"], "not_covered": NOT_COVERED,
                          "note": "Plan only; no connection or traffic. Prepare the documented topology before --run."}, indent=2))
        return 0
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.output or ROOT / "logs/hg-blackbox/multicast" / (stamp + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(out / "config.json", c)
    (out / PROBE.name).write_bytes(PROBE.read_bytes())
    (out / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    report = run(c, out)
    report["sources"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), PROBE)}
    write_json(out / "summary.json", report)
    lines = ["# HG multicast regression", "", "Status: **" + report["status"] + "**", "",
             "This result covers only the phases below. Hardware qualification remains incomplete.", "",
             "| Phase | Result |", "| --- | --- |"]
    lines += [f"| {r['name']} | {r['status']} |" for r in report["phases"]]
    lines += ["", "## Not covered", ""] + ["- " + item for item in report["not_covered"]]
    if report.get("error") or report.get("reason"):
        lines += ["", str(report.get("error") or report.get("reason"))]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(str(out.resolve()))
    print(report["status"])
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
