#!/usr/bin/env python3
"""Live IPTV black-box regression: rtp2httpd on HG and a downstream Linux NAS.

Default: local plan only. --run requires a prepared, boot-enabled multicast
backend; changes runtime pause/snooping and optionally GRO, never UCI/firmware.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HELPERS = HERE / "helpers"
sys.path.insert(0, str(HELPERS))
from hg_iptv_common import (EXIT, NOT_COVERED, aggregate, assess_backend, assess_bypass, assess_rtp,
                            assess_stream, assess_diagnostics, assess_ports, binding, covered_window,
                            check, config, cpu_summary, ipmr_forwarding, plan, port_counters,
                            require_mroute_vifs, worst)
from hg_iptv_reliability import ReliabilityMixin, reliability_plan
from hg_iptv_pse import PseStressMixin, stress_plan

LOCK = "/tmp/hg-qos-regression.lock"
CONFIG_FILES = ["network", "pon", "dhcp", "firewall", "igmpproxy", "rtp2httpd", "airoha_qos", "airoha_multicast"]
HASH_COMMAND = "sha256sum " + " ".join("/etc/config/" + f for f in CONFIG_FILES)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def elf_arch(path):
    data = Path(path).read_bytes()[:20]
    if len(data) != 20 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("expected a little-endian ELF64 executable: " + str(path))
    return {62: "x86_64", 183: "aarch64"}.get(int.from_bytes(data[18:20], "little"), "unknown")


def source_hashes(role):
    names = ["hg_iptv_stream.go"] if role == "stream" else ["hg_iptv_" + s + ".c" for s in ("native", "sample", "rtp", "diag", "pse")]
    return {name: hashlib.sha256((HELPERS / name).read_bytes()).hexdigest() for name in names}


def manifest(path, role, verify=False):
    path = Path(path)
    actual = dict(binary_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  arch=elf_arch(path), sources=source_hashes(role))
    sidecar = path.with_name(path.name + ".build.json")
    if verify:
        if not sidecar.exists() or json.loads(sidecar.read_text()) != actual:
            raise ValueError("probe/source manifest mismatch; run --build-probes: " + str(path))
    else:
        write_json(sidecar, actual)
    return actual


def build(c, args):
    """Local-only build. Existing site compiler environment (STAGING_DIR) is inherited."""
    sources = [str(HELPERS / ("hg_iptv_" + s + ".c")) for s in ("native", "sample", "rtp", "diag", "pse")]
    for role, compiler in (("hg", args.hg_cc), ("fnos", args.peer_cc)):
        if not compiler:
            raise ValueError("--hg-cc is required for --build-probes")
        dest = Path(c["binaries"][role]); dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.name + ".building-" + uuid.uuid4().hex[:8])
        try:
            subprocess.run([compiler, "-std=c11", "-static", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-o", str(temp)] + sources, check=True)
            if elf_arch(temp) != c[role]["arch"]:
                raise ValueError("compiler output architecture does not match " + role)
            os.replace(temp, dest)
            manifest(dest, role)
        finally:
            temp.unlink(missing_ok=True)
    dest = Path(c["binaries"]["stream"]); dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".building-" + uuid.uuid4().hex[:8])
    env = dict(os.environ, GOOS="linux", GOARCH={"aarch64": "arm64", "x86_64": "amd64"}[c["receiver"]["arch"]], CGO_ENABLED="0")
    try:
        subprocess.run(["go", "build", "-trimpath", "-o", str(temp), str(HELPERS / "hg_iptv_stream.go")], env=env, check=True)
        if elf_arch(temp) != c["receiver"]["arch"]:
            raise ValueError("stream helper architecture mismatch")
        os.replace(temp, dest)
        manifest(dest, "stream")
    finally:
        temp.unlink(missing_ok=True)
    print("Built local probes; no SSH connection or traffic was started.")


class Runner:
    def __init__(self, c, args):
        self.c, self.args = c, args
        self.token = "hiptv-" + uuid.uuid4().hex
        self.directory = "/tmp/hiptv_" + uuid.uuid4().hex[:16]
        self.dirs = dict(hg=self.directory, fnos=self.directory + "f", receiver=self.directory + "c")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.out = Path(args.output or ROOT / "logs/hg-blackbox/iptv" / (stamp + "_" + self.token[-8:])).resolve()
        self.out.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.out.chmod(0o700)  # Also constrain inherited default ACL masks.
        self.jobs, self.deployed, self.locked, self.armed = [], [], False, False
        self.report = dict(status="RUNNING", phases=[], idle={}, cleanup={}, hardware_qualification="incomplete",
                           not_covered=NOT_COVERED, plan=plan(c, args.gro_ab, args.paths_only), config=c,
                           started=dt.datetime.now(dt.timezone.utc).isoformat())
        self.expected = None
        for src in [Path(__file__)] + [p for p in HELPERS.glob("hg_iptv_*") if p.is_file()]:
            (self.out / src.name).write_bytes(src.read_bytes())
        write_json(self.out / "config.json", c)

    def ssh(self, role):
        host = self.c[role]
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
        if self.c.get("ssh_config"):
            command += ["-F", self.c["ssh_config"]]
        if host.get("known_hosts"):
            command += ["-o", "UserKnownHostsFile=" + host["known_hosts"]]
        return command + [host["ssh"]]

    def remote(self, role, command, data=None, timeout=25):
        p = subprocess.run(self.ssh(role) + [command], input=data, capture_output=True, timeout=timeout)
        if p.returncode:
            raise RuntimeError(f"{role} SSH/command failed ({p.returncode}): {p.stderr.decode(errors='replace')[-1200:]}")
        return p.stdout.decode()

    def upload(self, role, name, data):
        path = self.dirs[role] + "/" + name
        actual = self.remote(role, "umask 077; cat > " + shlex.quote(path) + " && sha256sum " + shlex.quote(path), data.encode() if isinstance(data, str) else data)
        digest = hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()
        if actual.split()[0] != digest:
            raise RuntimeError("helper upload checksum mismatch")
        return path

    def guard(self, action, *args):
        return self.remote("hg", shlex.join(["/bin/sh", self.directory + "/guard.sh", self.directory, self.token, action] + list(args)))

    def pulse(self):
        raw = self.guard("pulse")
        first, _, detail = raw.partition("\n")
        policy = json.loads(first)
        if policy["broken"] or policy["ppe_stopped"] or policy["scope_lost"]:
            raise RuntimeError("multicast backend fault/scope lost; stopping traffic")
        if not policy["enabled"] or not policy["frozen"] or policy["ingress"] != self.ingress or policy["egress"] != self.egress:
            raise RuntimeError("boot multicast scope changed during test")
        if self.expected is not None and bool(policy["paused"]) == self.expected:
            raise RuntimeError("requested runtime offload state was not applied")
        for key in ("errors", "bad_copies"):
            if policy[key] > self.original["policy"][key]:
                raise RuntimeError("backend reported new " + key)
        return dict(policy=policy, detail=detail, observed=time.time(), monotonic=time.monotonic())

    def members(self):
        return self.remote("hg", "bridge mdb show; cat /proc/net/igmp")

    def physical_ports(self, name):
        ports = [self.c['hg']['port']] + self.c['path_checks']['nonmember_ports']
        commands = []
        for port in ports:
            commands += ["printf 'PORT %s\\n' " + shlex.quote(port),
                         "printf 'carrier '; cat " + shlex.quote('/sys/class/net/' + port + '/carrier'),
                         'ethtool -S ' + shlex.quote(port) + ' --groups eth-mac']
        started = time.monotonic()
        raw = self.remote('hg', 'set -e\n' + '\n'.join(commands))
        (self.out / (name + '-physical-ports.txt')).write_text(raw)
        return dict(monotonic=(started + time.monotonic()) / 2, ports=port_counters(raw, ports))

    def retired(self, name):
        """Observe natural retirement before any pause/flush changes the result."""
        end = time.monotonic() + self.c['path_checks']['leave_seconds']
        while True:
            sample = self.pulse()
            members = self.members()
            routes = self.remote('hg', 'cat /proc/net/ip_mr_cache')
            present = self.target_present(members)
            masks = binding(self.c, sample['detail'], self.ingress)
            forwarding = ipmr_forwarding(self.c, routes)
            if not present and not masks and not forwarding:
                verdict = check('natural-leave', 'PASS', 'members, forwarding route and target BIND retired without a forced flush')
                break
            if time.monotonic() >= end:
                verdict = check('natural-leave', 'INCONCLUSIVE' if present else 'FAIL',
                                'target membership remains' if present else 'forwarding survived the last membership',
                                masks=masks, forwarding=forwarding)
                break
            time.sleep(2)
        (self.out / (name + '-leave-members.txt')).write_text(members)
        (self.out / (name + '-leave-routes.txt')).write_text(routes)
        write_json(self.out / (name + '-leave-backend.json'), sample)
        return verdict

    def target_present(self, raw):
        import socket
        encoded = socket.inet_aton(self.c["channel"]["group"])[::-1].hex().upper()
        return self.c["channel"]["group"] in raw or encoded in raw.upper()

    def mode(self, on, gro="keep"):
        self.guard("mode", "1" if on else "0", gro)
        self.expected = on
        return self.pulse()

    def quiesce(self):
        self.mode(False)
        end = time.monotonic() + self.c["timing"]["settle_seconds"]
        while time.monotonic() < end:
            self.pulse()
            raw = self.members()
            if not self.target_present(raw):
                return
            time.sleep(2)
        (self.out / "quiesce-timeout.txt").write_text(raw)
        raise RuntimeError("target channel remained subscribed; wait for IGMP expiry or stop another viewer")

    def preflight(self):
        builds = {key: manifest(self.c["binaries"][key], key, verify=True) for key in ("hg", "fnos", "stream")}
        write_json(self.out / "probe-build-manifests.json", builds)
        for role, binary in (("hg", "hg"), ("fnos", "fnos"), ("receiver", "stream")):
            if elf_arch(self.c["binaries"][binary]) != self.c[role]["arch"]:
                raise ValueError("build a matching helper for " + role)
            arch = self.remote(role, "uname -m").strip()
            if arch != self.c[role]["arch"]:
                raise ValueError("remote architecture differs from config: " + role)
        hg = self.c["hg"]
        self.remote("hg", "test \"$(id -u)\" = 0 && command -v airoha-mcastctl >/dev/null && command -v jsonfilter >/dev/null && command -v bridge >/dev/null && command -v ethtool >/dev/null && test -w /sys/kernel/debug/ppe/multicast_enable")
        policy = json.loads(self.remote("hg", "airoha-mcastctl get"))
        self.ingress = int(self.remote("hg", "cat " + shlex.quote("/sys/class/net/" + hg["ingress"] + "/ifindex")))
        self.egress = int(self.remote("hg", "cat " + shlex.quote("/sys/class/net/" + hg["egress"] + "/ifindex")))
        if not policy["enabled"] or not policy["frozen"]:
            raise RuntimeError("backend must already be boot-enabled with the intended scope; this script never edits UCI or reboots")
        if policy["ingress"] != self.ingress or policy["egress"] != self.egress or any(policy[k] for k in ("broken", "ppe_stopped", "scope_lost", "errors", "bad_copies")):
            raise RuntimeError("backend scope/fault preflight failed")
        vifs = self.remote('hg', 'cat /proc/net/ip_mr_vif')
        (self.out / 'preflight-mroute-vifs.txt').write_text(vifs)
        require_mroute_vifs(vifs, hg['ingress'], hg['egress'])
        if self.remote("hg", "cat " + shlex.quote("/sys/class/net/" + hg["port"] + "/carrier")).strip() != "1":
            raise RuntimeError("selected physical HG output has no carrier")
        self.remote("hg", "test -d " + shlex.quote("/sys/class/net/" + hg["bridge"] + "/brif/" + hg["port"]))
        if self.c.get('path_checks'):
            for port in self.c['path_checks']['nonmember_ports']:
                self.remote('hg', 'test -d ' + shlex.quote('/sys/class/net/' + hg['bridge'] + '/brif/' + port))
            ports = self.physical_ports('preflight')
            if not all(p['carrier'] for p in ports['ports'].values()):
                raise RuntimeError('path checks require connected physical outputs')
        for role in ("fnos", "receiver"):
            addresses = self.remote(role, "ip -4 address show")
            if not any(line.strip().startswith("inet " + self.c[role]["address"] + "/") for line in addresses.splitlines()):
                raise RuntimeError("configured data address is not local to " + role)
        mac = self.remote("fnos", "cat " + shlex.quote("/sys/class/net/" + self.c["fnos"]["interface"] + "/address")).strip().lower()
        fdb = self.remote("hg", "bridge fdb show")
        (self.out / "preflight-fdb.txt").write_text(fdb)
        if not any(line.lower().startswith(mac + " ") and (" dev " + hg["port"] + " ") in (line + " ") for line in fdb.splitlines()):
            raise RuntimeError("fnOS interface MAC is not learned on the selected HG port; verify wiring/FDB")
        if self.target_present(self.members()):
            raise RuntimeError("target IPTV channel already has a subscriber; stop it before running")
        self.remote("hg", "umask 077; mkdir " + LOCK + " && printf '%s\\n' " + shlex.quote(self.token) + " > " + LOCK + "/owner")
        self.locked = True
        # Re-read original state after acquiring the shared lock.
        policy = json.loads(self.remote("hg", "airoha-mcastctl get"))
        features = self.remote("hg", "ethtool -k " + shlex.quote(hg["gro_device"]))
        import re
        match = re.search(r"^rx-gro-list: (on|off)(.*)$", features, re.M)
        if not match or (self.args.gro_ab and "[fixed]" in match[2]):
            raise RuntimeError("GRO-list capability unavailable or not changeable for A/B")
        bridge = self.remote("hg", "cat " + " ".join(shlex.quote("/sys/class/net/" + hg["bridge"] + "/bridge/" + k) for k in ("multicast_snooping", "multicast_querier"))).split()
        if len(bridge) != 2 or any(v not in ("0", "1") for v in bridge):
            raise RuntimeError("invalid bridge state")
        self.original = dict(policy=policy, gro=match[1], bridge=bridge, config_hashes=self.remote("hg", HASH_COMMAND))
        write_json(self.out / "original.json", self.original)
        (self.out / "original-features.txt").write_text(features)
        (self.out / "board.json").write_text(self.remote("hg", "ubus call system board"))
        for role in ("hg", "fnos"):
            output = self.remote(role, shlex.quote(self.c[role]["rtp2httpd_binary"]) + " -h 2>&1 | head -8")
            (self.out / (role + "-rtp2httpd-version.txt")).write_text(output)

    def deploy(self):
        for role in ("hg", "fnos", "receiver"):
            self.remote(role, "umask 077; mkdir -m 700 " + shlex.quote(self.dirs[role]))
            self.deployed.append(role)
            self.upload(role, "owner", self.token + "\n")
            binary = "stream" if role == "receiver" else role
            path = self.upload(role, "probe", Path(self.c["binaries"][binary]).read_bytes())
            self.remote(role, "chmod 700 " + shlex.quote(path))
        hg, orig = self.c["hg"], self.original
        self.upload("hg", "original", " ".join([str(orig["policy"]["paused"])] + orig["bridge"] + [orig["gro"], hg["bridge"], hg["gro_device"]]) + "\n")
        self.upload("hg", "guard.sh", (HELPERS / "hg_iptv_guard.sh").read_bytes())
        self.armed = True
        self.guard("pulse")  # Establish lease before starting the independent watchdog.
        command = shlex.join(["/bin/sh", self.directory + "/guard.sh", self.directory, self.token, "watchdog"])
        self.remote("hg", "(trap '' HUP; " + command + ") </dev/null >" + shlex.quote(self.directory + "/watchdog.log") + " 2>&1 &")
        for _ in range(5):
            if self.remote("hg", "test ! -f " + shlex.quote(self.directory + "/watchdog.ready") + " || cat " + shlex.quote(self.directory + "/watchdog.ready")).strip():
                break
            time.sleep(1)
        else:
            raise RuntimeError("watchdog did not become ready")
        self.guard("setup")
        self.expected = False

    def launch(self, role, label, arguments):
        out = (self.out / (label + ".jsonl")).open("wb")
        err = (self.out / (label + ".stderr")).open("wb")
        started = time.monotonic()
        try:
            proc = subprocess.Popen(self.ssh(role) + [shlex.join([self.dirs[role] + "/probe"] + arguments)], stdout=out, stderr=err)
        except BaseException:
            out.close(); err.close(); raise
        job = dict(proc=proc, out=out, err=err, label=label, path=self.out / (label + ".jsonl"), started=started)
        self.jobs.append(job)
        return job

    def wait_jobs(self, jobs, timeout, samples, observe=None):
        end = time.monotonic() + timeout
        while any(j["proc"].poll() is None for j in jobs):
            if time.monotonic() > end:
                raise TimeoutError("bounded probe did not exit")
            samples.append(self.pulse())
            for job in jobs:
                if job["proc"].poll() is not None:
                    job.setdefault("finished", time.monotonic())
            if observe:
                observe()
            time.sleep(3)
        for job in jobs:
            job.setdefault("finished", time.monotonic())
            job["out"].close(); job["err"].close()

    def rtp(self, phase, host, seconds=None):
        c, t = self.c["channel"], self.c["timing"]
        interface = self.c["hg"]["ingress"] if host == "hg" else self.c["fnos"]["interface"]
        return self.launch(host, phase + "-rtp-" + host, ["rtp", interface, c["group"], str(c["port"]), c["source"], str(c["source_port"]), str(seconds or t["probe_seconds"]), str(t["probe_warmup"])])

    def ready(self, probes):
        deadline = time.monotonic() + 15
        pending = list(probes)
        while pending:
            self.pulse()
            for job in pending[:]:
                if "RTP_READY " in (self.out / (job["label"] + ".stderr")).read_text():
                    job["ready_observed"] = time.monotonic()
                    pending.remove(job)
                elif job["proc"].poll() is not None:
                    raise RuntimeError("RTP observer exited before joining")
            if time.monotonic() >= deadline:
                raise TimeoutError("RTP observers did not become ready")
            if pending:
                time.sleep(.2)

    def diagnostics(self, name):
        values = {}
        for host in ("hg", "fnos"):
            command = self.c[host].get("diagnostics_command", [self.dirs[host] + "/probe", "diag"])
            values[host] = json.loads(self.remote(host, shlex.join(command)))
        # Read exactly one known board counter; no counter reset/register write.
        raw = self.remote("hg", "case $(cat /tmp/sysinfo/board_name) in nokia,xg-040g-md-*) devmem 0x1fb515a0 32;; *) printf unavailable;; esac")
        try:
            values["hg"]["cdm2_rx_cpu_drop"] = int(raw.strip(), 0)
        except ValueError:
            values["hg"]["cdm2_rx_cpu_drop"] = None
        write_json(self.out / (name + "-diagnostics.json"), values)
        return values

    def client(self, phase, placement, seconds, warmup):
        return self.launch("receiver", phase + "-client-" + placement, ["-url", self.c[placement]["url"], "-source", self.c["receiver"]["address"], "-seconds", str(seconds), "-warmup", str(warmup)])

    def sampler(self, phase, host, seconds):
        h = self.c[host]
        devices = [h["ingress"], h["egress"], h["wire"]] if host == "hg" else [h["interface"], h["wire"], "lo"]
        return self.launch(host, phase + "-cpu-" + host, ["sample", str(seconds)] + list(dict.fromkeys(devices)))

    def idle(self, name):
        self.quiesce()
        jobs = {host: self.sampler(name, host, 20) for host in ("hg", "fnos")}
        self.wait_jobs(list(jobs.values()), 35, [])
        self.report["idle"][name] = {host: cpu_summary(j["path"].read_text(), 0, 20) for host, j in jobs.items()}

    def phase(self, p):
        print("START " + p["name"], flush=True)
        self.quiesce()
        t = self.c["timing"]
        result = dict(p, samples=[self.mode(p["offload"], p.get("gro", "keep"))], checks=[], clients={}, rtp={}, cpu={})
        self.report["phases"].append(result)
        result["diagnostics"] = [self.diagnostics(p["name"] + "-before")]
        if self.c.get('path_checks'):
            result['physical_before'] = self.physical_ports(p['name'] + '-before')
        clients, probes, samplers = {}, {}, {}
        seconds, warmup = t["seconds"], t["warmup"]
        if p["kind"] == "performance":
            samplers = {host: self.sampler(p["name"], host, warmup + seconds + 12) for host in ("hg", "fnos")}
            clients[p["placement"]] = self.client(p["name"], p["placement"], seconds, warmup)
        elif p["kind"] in ("mixed", "path"):
            # Observers must encompass the complete HTTP job, including connect
            # time, warmup and measurement. Start/join both observers first.
            probe_seconds = seconds + warmup + 40
            hosts = ('fnos',) if p['kind'] == 'path' else ('hg', 'fnos')
            probes = {host: self.rtp(p["name"], host, probe_seconds) for host in hosts}
            self.ready(list(probes.values()))
            time.sleep(t["probe_warmup"] + 1)
            clients = {host: self.client(p["name"], host, seconds, warmup) for host in hosts}
        if p["kind"] == "rtp":
            probes = {host: self.rtp(p["name"], host) for host in ("hg", "fnos")}
            self.ready(list(probes.values()))
        # Record actual rtp2httpd socket ownership, capacity and drops while
        # sockets exist. Identical bounded reads are used in all comparisons;
        # their observer overhead is included in the measured CPU window.
        observed = set()
        phase_started = time.monotonic()
        def observe():
            elapsed = time.monotonic() - phase_started
            for label, at in (("active-early", warmup + 1), ("active-late", warmup + seconds - 5)):
                if label not in observed and elapsed >= at:
                    result["diagnostics"].append(self.diagnostics(p["name"] + "-" + label))
                    observed.add(label)
        jobs = list(clients.values()) + list(probes.values()) + list(samplers.values())
        self.wait_jobs(jobs, seconds + warmup + 65, result["samples"], observe)
        result["diagnostics"].append(self.diagnostics(p["name"] + "-after"))
        result["samples"].append(self.pulse())
        (self.out / (p["name"] + "-members.txt")).write_text(self.members())
        for host, job in clients.items():
            data = json.loads(job["path"].read_text())
            result["clients"][host] = data
            result["checks"].append(dict(assess_stream(self.c, data, seconds), name="stream-" + host))
        for host, job in probes.items():
            data = json.loads(job["path"].read_text()); result["rtp"][host] = data
            result["checks"].append(assess_rtp(self.c, data, host))
        if p["kind"] in ("mixed", "path"):
            result["checks"].append(covered_window(list(probes.values()), list(clients.values()), t["probe_warmup"], probe_seconds))
        for host in ("hg", "fnos"):
            active = p["kind"] == "mixed" or p.get("placement") == host
            result["checks"].append(assess_diagnostics(self.c, [s[host] for s in result["diagnostics"]], host, active))
        for host, job in samplers.items():
            try:
                client = next(iter(clients.values()))
                offset = max(0, client["started"] - job["started"]) + result["clients"][p["placement"]].get("ttfb_seconds", 0)
                result["cpu"][host] = cpu_summary(job["path"].read_text(), warmup + offset, seconds)
                if result["cpu"][host]["seconds"] < (seconds - 5) * .8:
                    raise ValueError("short stable CPU window")
            except (ValueError, KeyError, IndexError) as e:
                result["checks"].append(check("cpu-" + host, "INCONCLUSIVE", str(e)))
        if p["kind"] == "performance":
            start = next(iter(clients.values()))["started"] + result["clients"][p["placement"]].get("ttfb_seconds", 0) + warmup
            lo, hi = start + 2, start + seconds - 1
        else:
            lo = max(j["ready_observed"] for j in probes.values()) + t["probe_warmup"] + 2
            hi = min(j["started"] for j in probes.values()) + t["probe_warmup"] + (probe_seconds if p["kind"] in ("mixed", "path") else t["probe_seconds"]) - 1
            if clients:
                lo = max(lo, max(j["started"] for j in clients.values()) + warmup + 22)
                hi = min(hi, min(j["finished"] for j in clients.values()) - 4)
        live_samples = [s for s in result["samples"] if lo <= s["monotonic"] <= hi]
        result["backend_window"] = dict(start=lo, end=hi, samples=len(live_samples))
        result["checks"].append(assess_backend(self.c, p, live_samples, self.ingress))
        result["returncodes"] = {j["label"]: j["proc"].returncode for j in jobs}
        if any(j["proc"].returncode not in (0, 1) for j in jobs) or any(j["proc"].returncode for j in samplers.values()):
            result["checks"].append(check("probe-exit", "ERROR", "probe exited nonzero; inspect stderr"))
        if self.c.get('path_checks'):
            result['checks'].append(self.retired(p['name']))
            result['physical_after'] = self.physical_ports(p['name'] + '-after')
            result['checks'].append(assess_ports(self.c, result['physical_before'], result['physical_after']))
        result["status"] = worst([v["status"] for v in result["checks"]])
        write_json(self.out / (p["name"] + "-summary.json"), result)
        print(p["name"] + ": " + result["status"], flush=True)
        self.save()

    def stop_jobs(self):
        for job in self.jobs:
            if job["proc"].poll() is None:
                job["proc"].terminate()
                try:
                    job["proc"].wait(timeout=3)
                except subprocess.TimeoutExpired:
                    job["proc"].kill(); job["proc"].wait()
            job["out"].close(); job["err"].close()

    def cleanup(self):
        self.stop_jobs()
        errors = []
        if self.armed:
            try:
                self.guard("restore")
                self.locked = False
                self.report["cleanup"]["runtime_restored"] = True
                for name in ("restored.policy", "watchdog.log"):
                    (self.out / name).write_text(self.remote("hg", "cat " + shlex.quote(self.directory + "/" + name)))
                remaining = self.members()
                (self.out / "cleanup-members.txt").write_text(remaining)
                self.report["cleanup"]["target_membership_lingering"] = self.target_present(remaining)
            except Exception as e:
                errors.append(str(e))
                self.report["cleanup"]["recovery_command"] = shlex.join(["/bin/sh", self.directory + "/guard.sh", self.directory, self.token, "restore"])
        if hasattr(self, "original"):
            try:
                hashes = self.remote("hg", HASH_COMMAND)
                (self.out / "restored-config-hashes.txt").write_text(hashes)
                if hashes != self.original["config_hashes"]:
                    raise RuntimeError("persistent configuration changed during run; not overwriting external changes")
                self.report["cleanup"]["config_hashes_match"] = True
            except Exception as e:
                errors.append(str(e))
        if self.locked and not self.armed:
            try:
                self.remote("hg", "test \"$(cat " + LOCK + "/owner)\" = " + shlex.quote(self.token) + " && rm " + LOCK + "/owner && rmdir " + LOCK)
                self.locked = False
            except Exception as e:
                errors.append(str(e))
        for role in reversed(self.deployed):
            if role == "hg" and self.armed and not self.report["cleanup"].get("runtime_restored"):
                continue  # Retain the watchdog and original settings for recovery.
            try:
                directory = shlex.quote(self.dirs[role])
                self.remote(role, "test \"$(cat " + directory + "/owner)\" = " + shlex.quote(self.token) + " && rm -rf -- " + directory)
            except Exception as e:
                errors.append(str(e))
        self.report["cleanup"].update(status="ERROR" if errors else "PASS", errors=errors)

    def save(self):
        self.report["aggregate"] = aggregate(self.report["phases"])
        write_json(self.out / "summary.json", self.report)
        lines = ["# HG 真实 IPTV 黑盒回归", "", "结果：**" + self.report["status"] + "**", "",
                 "HG 总 CPU 以全部核心为 100%；最忙核心以一个核心为 100%。频率和背景负载未固定。", "",
                 "| 架构 / 卸载 | HG 总 CPU | 最忙核心 | 码率 Mbps | 轮数 |", "|---|---:|---:|---:|---:|"]
        if self.args.paths_only:
            lines = ['# HG / fnOS IPTV 路径回归', '', '结果：**' + self.report['status'] + '**', '',
                     '本轮检查 fnOS 软件/硬件收流、非订阅出口硬件 MAC 字节/组播帧计数和自然退组，不进行 CPU 基准。']
        for name, row in self.report["aggregate"].items():
            lines.append(f"| {name} | {row['cpu_mean']:.2f}% | {row['max_core_mean']:.2f}% | {row['mbps_mean']:.2f} | {row['rounds']} |")
        lines += ["", "## 分项判定", ""]
        for item in self.report.get("checks", []):
            lines.append("- " + item["name"] + ": " + item["status"] + " — " + item["reason"])
        for phase in self.report["phases"]:
            lines.append("- " + phase["name"] + ": **" + phase.get("status", "ERROR") + "**")
            for item in phase["checks"]:
                lines.append("  - " + item["name"] + ": " + item["status"] + " — " + item["reason"])
                if 'tx_bytes' in item:
                    lines.append('    - 非订阅出口 TX 增量：' + json.dumps(item['tx_bytes']) + ' 字节。')
                if 'tx_multicast' in item:
                    lines.append('    - 非订阅出口硬件组播帧增量：' + json.dumps(item['tx_multicast']) + '。')
            for host, rtp in phase.get("rtp", {}).items():
                lines.append(f"  - {host}: TTL={rtp.get('ttl')}, missing={rtp.get('sequence_missing')}, reordered={rtp.get('sequence_reordered')}, duplicates={rtp.get('sequence_duplicates')}")
        lines += ["", "## 恢复", "", "```json", json.dumps(self.report["cleanup"], ensure_ascii=False, indent=2), "```",
                  "", "## 范围", "", "硬件资格验证仍不完整；未覆盖以下项目：", ""]
        lines += ["- " + item for item in NOT_COVERED]
        lines += ["", "接收器：" + self.c["receiver"]["ssh"] + "；实际软件版本见两端 version 文件。",
                  "已知 TTL 错误仍会报 FAIL；端口总 TX 不能代替任意业务负载下的逐组抓包。"]
        if not self.args.paths_only:
            lines += ['CPU 对照仅作现场测量，不以固定下降比例作为 PASS 条件。']
        if self.report.get("error"):
            lines += ["", "执行异常：" + self.report["error"]]
        (self.out / "REPORT.md").write_text("\n".join(lines) + "\n")

    def run(self):
        try:
            self.preflight(); self.deploy()
            if not self.args.paths_only:
                self.idle("idle-before")
            for phase in self.report["plan"]:
                self.phase(phase)
            if self.args.gro_ab:
                self.mode(False, self.original["gro"])
            if not self.args.paths_only:
                self.idle("idle-after")
            self.report["checks"] = [] if self.args.paths_only else [assess_bypass(self.c, self.report["phases"])]
            self.report["status"] = worst([p["status"] for p in self.report["phases"]] + [v["status"] for v in self.report["checks"]])
        except (Exception, KeyboardInterrupt) as e:
            self.report.update(status="ERROR", error=type(e).__name__ + ": " + str(e))
        finally:
            # A second Ctrl-C must not prevent attempting runtime restoration.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            self.cleanup()
            self.report["status"] = worst([self.report["status"], self.report["cleanup"]["status"]])
            self.report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.save()
        print(self.report["status"] + " " + str(self.out), flush=True)
        return EXIT[self.report["status"]]


class ReliabilityRunner(ReliabilityMixin, Runner):
    pass


class PseStressRunner(PseStressMixin, ReliabilityMixin, Runner):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "local/iptv.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="run traffic and temporary runtime controls")
    mode.add_argument("--build-probes", action="store_true", help="only compile local native/HTTP helpers")
    mode.add_argument("--dry-run", action="store_true", help="print local plan (default)")
    parser.add_argument("--gro-ab", action="store_true", help="append runtime GRO on/off diagnostic comparisons")
    parser.add_argument("--profile", choices=("legacy", "reliability", "pse-stress"), default="legacy",
                        help="reliability observes current policy; never pauses offload or changes bridge/GRO")
    parser.add_argument("--paths-only", action="store_true", help="check fnOS SW/HW delivery, connected nonmember outputs and natural leave; no CPU benchmark")
    parser.add_argument("--hg-cc", help="AArch64 musl compiler executable for --build-probes")
    parser.add_argument("--peer-cc", default="cc", help="fnOS native compiler executable")
    parser.add_argument("--output", help="new report directory")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        c = config(args.config)
        if args.profile != 'legacy' and (args.gro_ab or args.paths_only):
            raise ValueError('observation profiles cannot be combined with --gro-ab/--paths-only')
        stress = stress_plan(c, require_channels=args.run) if args.profile == 'pse-stress' else None
        if args.build_probes:
            build(c, args); return 0
        if not args.run:
            reliable = args.profile != 'legacy'
            print(json.dumps(dict(mode="PLAN ONLY: no SSH or device changes", profile=args.profile,
                                  phases=stress if stress else reliability_plan(c) if reliable else plan(c, args.gro_ab, args.paths_only),
                                  prerequisites="prepared IPTV routing and boot-enabled multicast scope",
                                  runtime_changes=[] if reliable else ["multicast pause/resume", "bridge snooping/querier", "GRO-list only with --gro-ab"],
                                  receiver=c["receiver"]["ssh"], hardware_qualification="incomplete"), indent=2))
            return 0
        def interrupted(signum, frame):
            raise KeyboardInterrupt("signal " + str(signum))
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        cls = {'legacy': Runner, 'reliability': ReliabilityRunner, 'pse-stress': PseStressRunner}[args.profile]
        return cls(c, args).run()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
        parser.error(str(e))


if __name__ == "__main__":
    sys.exit(main())
