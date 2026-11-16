"""Pure configuration, measurement and decision logic for live IPTV regression."""
import copy
import ipaddress
import json
from pathlib import Path
import re
import statistics
from urllib.parse import urlsplit

EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}
NOT_COVERED = [
    "FCC/channel switching, decoded picture quality and AP/WMM",
    "Unobserved physical outputs, VLAN/GEM key isolation, SSM and IPv6 multicast",
    "TTL=1, varying DSCP/ECN, long-term aging and hardware fault injection",
    "Maximum throughput or a controlled CPU-frequency/background-load benchmark",
]


def worst(values):
    return next((v for v in ("ERROR", "FAIL", "INCONCLUSIVE") if v in values), "PASS")


def check(name, status, reason, **data):
    return dict(name=name, status=status, reason=reason, **data)


def require_mroute_vifs(raw, ingress, egress):
    """Require installed kernel VIFs, independently of daemon/config presence."""
    lines = raw.splitlines()
    header = 'Interface BytesIn PktsIn BytesOut PktsOut Flags Local Remote'.split()
    if not lines or lines[0].split() != header:
        raise ValueError('unrecognized /proc/net/ip_mr_vif header')
    vifs, indices = {}, set()
    for line in lines[1:]:
        if not line.strip():
            continue
        row = line.split()
        if len(row) != 9:
            raise ValueError('malformed /proc/net/ip_mr_vif row')
        index = int(row[0])
        if not 0 <= index < 32 or index in indices or row[1] in vifs:
            raise ValueError('invalid or duplicate multicast VIF')
        vifs[row[1]] = index
        indices.add(index)
    missing = [name for name in (ingress, egress) if name not in vifs]
    if missing:
        raise RuntimeError('multicast VIF missing: ' + ', '.join(missing) +
                           '; check igmpproxy/interface readiness before testing')
    if ingress == egress:
        raise ValueError('distinct upstream/downstream VIFs required')
    return vifs


def config(path):
    path = Path(path).expanduser().resolve()
    c = json.loads(path.read_text())

    def device(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", value):
            raise ValueError("invalid interface name")

    def address(value):
        a = ipaddress.IPv4Address(value)
        if a.is_multicast or a.is_loopback or a.is_unspecified or int(a) == 0xffffffff:
            raise ValueError("a unicast IPv4 address is required")

    def number(obj, key, default, low, high, integer=True):
        value = obj.setdefault(key, default)
        if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
            raise ValueError(f"{key} must be {low}..{high}")

    c.setdefault("receiver", copy.deepcopy(c["fnos"]))
    for name in ("hg", "fnos", "receiver"):
        h = c[name]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", h["ssh"]):
            raise ValueError("use an SSH alias or user@host, not SSH options")
        if h["arch"] not in ("aarch64", "x86_64"):
            raise ValueError("native helpers support aarch64 and x86_64 Linux")
        if "known_hosts" in h:
            h["known_hosts"] = str((path.parent / Path(h["known_hosts"]).expanduser()).resolve())
        if "diagnostics_command" in h:
            command = h["diagnostics_command"]
            if (not isinstance(command, list) or not command or len(command) > 16 or
                    any(not isinstance(v, str) or not v or any(x in v for x in "\n\r\0") for v in command)):
                raise ValueError("diagnostics_command must be a nonempty argv list without control characters")
    if "ssh_config" in c:
        c["ssh_config"] = str((path.parent / Path(c["ssh_config"]).expanduser()).resolve())
    for key in ("bridge", "ingress", "egress", "wire", "port", "gro_device"):
        device(c["hg"][key])
    if "path_checks" in c:
        p = c["path_checks"]
        if not isinstance(p, dict):
            raise ValueError('path_checks must be an object')
        ports = p.get("nonmember_ports")
        if not isinstance(ports, list) or not 1 <= len(ports) <= 16:
            raise ValueError("path_checks requires a nonempty nonmember_ports list")
        for port in ports:
            device(port)
            if port in (c['hg'][k] for k in ('bridge', 'ingress', 'egress', 'wire', 'port')):
                raise ValueError("nonmember ports must be distinct physical bridge outputs")
        if len(set(ports)) != len(ports):
            raise ValueError("duplicate nonmember port")
        number(p, 'max_nonmember_tx_bytes', 65536, 0, 10485760)
        number(p, 'leave_seconds', 45, 5, 240)
    for key in ("interface", "wire"):
        device(c["fnos"][key])
    if c["hg"]["ingress"] == c["hg"]["egress"]:
        raise ValueError("distinct HG ingress/egress required")
    address(c["fnos"]["address"])
    address(c["receiver"]["address"])
    number(c["hg"], "ppe_bit", 4, 0, 15)
    number(c['hg'], 'udp_rcvbuf_request', 2097152, 1, 16777216)
    for name in ("hg", "fnos"):
        binary = c[name]["rtp2httpd_binary"]
        if not isinstance(binary, str) or not binary.startswith("/") or any(x in binary for x in "\n\r\0"):
            raise ValueError("rtp2httpd_binary must be an absolute executable path")
    ch = c["channel"]
    group = ipaddress.IPv4Address(ch["group"])
    if group not in ipaddress.IPv4Network("239.0.0.0/8"):
        raise ValueError("select an existing IPTV channel in 239.0.0.0/8")
    address(ch["source"])
    for key, default, low, high in (("port", 2000, 1024, 65535), ("source_port", 8046, 1, 65535),
                                   ("ingress_ttl", 124, 2, 255), ("dscp", 0, 0, 63), ("ecn", 0, 0, 3)):
        number(ch, key, default, low, high)
    for name in ("hg", "fnos"):
        u = urlsplit(c[name]["url"])
        suffix = f"/rtp/{ch['group']}:{ch['port']}"
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or not u.path.endswith(suffix):
            raise ValueError("use direct RTP HTTP URLs for the same group/port; no credentials, FCC query or redirects")
    t = c.setdefault("timing", {})
    for key, default, low, high in (("seconds", 45, 15, 180), ("warmup", 10, 5, 30),
                                   ("rounds", 2, 2, 4), ("probe_seconds", 15, 10, 60),
                                   ("probe_warmup", 3, 1, 10), ("settle_seconds", 90, 10, 240)):
        number(t, key, default, low, high)
    thresholds = c.setdefault("thresholds", {})
    for key, default, low, high in (("min_mbps", 1, .01, 1000), ("max_read_gap_seconds", .5, .01, 10),
                                   ("max_reordered_ratio", .001, 0, 1), ("min_bound_sample_ratio", .8, .1, 1)):
        number(thresholds, key, default, low, high, integer=False)
    number(thresholds, "min_rtp_packets", 1000, 100, 1000000)
    for key in ("hg", "fnos", "stream"):
        c["binaries"][key] = str((path.parent / Path(c["binaries"][key]).expanduser()).resolve())
    r = c.setdefault('reliability', {})
    for key, default, low, high in (('rounds', 3, 1, 10), ('seconds', 45, 15, 180),
                                   ('warmup', 10, 5, 30), ('join_rounds', 5, 1, 20),
                                   ('join_seconds', 15, 10, 60), ('leave_seconds', 90, 10, 240),
                                   ('first_timeout', 5, 2, 30)):
        number(r, key, default, low, high)
    number(r, 'max_join_seconds', 1, .01, 5, integer=False)
    if r['max_join_seconds'] >= r['first_timeout']:
        raise ValueError('join limit must be below first-packet timeout')
    if 'background_channel' in c:
        b = c['background_channel']
        if ipaddress.IPv4Address(b['group']) not in ipaddress.IPv4Network('239.0.0.0/8'):
            raise ValueError('background_channel requires an IPTV group')
        address(b['source'])
        for key in ('port', 'source_port', 'ingress_vlan'):
            if key not in b:
                raise ValueError('background_channel requires an explicit tuple and ingress_vlan')
            number(b, key, 0, 1 if key != 'port' else 1024, 4094 if key == 'ingress_vlan' else 65535)
        # Optional externally observed, per-tuple ingress counters. Never infer
        # the VLAN of an UNB entry from uninitialized rewrite fields.
        if 'evidence_file' in b:
            b['evidence_file'] = str((path.parent / Path(b['evidence_file']).expanduser()).resolve())
    for peer in c.get('pse_stress',{}).get('receivers',[]):
        if 'known_hosts' in peer:
            peer['known_hosts']=str((path.parent/Path(peer['known_hosts']).expanduser()).resolve())
    return c


def plan(c, gro_ab=False, paths_only=False):
    if paths_only:
        if not c.get('path_checks') or gro_ab:
            raise ValueError("--paths-only requires path_checks and cannot be combined with --gro-ab")
        return [dict(name='fnos-path-' + mode, kind='path', placement='fnos', offload=on)
                for mode, on in (('sw', False), ('hw', True))]
    base = [("fnos", False), ("fnos", True), ("hg", False), ("hg", True)]
    phases = []
    for r in range(c["timing"]["rounds"]):
        for host, on in base if r % 2 == 0 else reversed(base):
            phases.append(dict(name=f"{host}-{'hw' if on else 'sw'}-{r+1}", kind="performance", placement=host, offload=on))
    phases += [dict(name="mixed-" + mode, kind="mixed", offload=on) for mode, on in (("sw", False), ("hw", True))]
    if gro_ab:
        phases += [dict(name=f"ttl-{'hw' if on else 'sw'}-gro-{gro}", kind="rtp", offload=on, gro=gro)
                   for on, gro in ((False, "on"), (False, "off"), (True, "off"), (True, "on"))]
    return phases


def assess_stream(c, data, seconds):
    if data.get("error") or ("http" in data and data["http"] != 200):
        return check("stream", "FAIL", "HTTP connection/request failed")
    required = ("http", "bytes", "seconds", "mbps", "max_read_gap_seconds", "ts_packets", "ts_cc_errors",
                "ts_duplicates", "ts_tei", "ts_invalid_headers", "ts_resync_bytes", "read_error")
    if any(k not in data for k in required):
        return check("stream", "ERROR", "incomplete HTTP probe result")
    if data["http"] != 200 or data["read_error"] or data["bytes"] == 0:
        return check("stream", "FAIL", "HTTP stream interrupted or empty")
    errors = {k: data[k] for k in ("ts_cc_errors", "ts_duplicates", "ts_tei", "ts_invalid_headers") if data[k]}
    # A warmup may end within one TS packet; more than 187 discarded bytes is not alignment.
    if data["ts_resync_bytes"] > 187:
        errors["ts_resync_bytes"] = data["ts_resync_bytes"]
    if errors:
        return check("stream", "FAIL", "MPEG-TS continuity/format failure", observed=errors)
    if data["seconds"] < seconds * .95 or data["ts_packets"] < 1000 or data["mbps"] < c["thresholds"]["min_mbps"]:
        return check("stream", "INCONCLUSIVE", "insufficient duration/media rate/TS sample")
    if data["max_read_gap_seconds"] > c["thresholds"]["max_read_gap_seconds"]:
        return check("stream", "FAIL", "HTTP read gap exceeds configured limit")
    return check("stream", "PASS", "continuous MPEG-TS within configured thresholds")


def assess_rtp(c, data, host):
    required = ("packets", "seconds", "max_gap_seconds", "rtp_packets", "non_rtp", "invalid_metadata", "socket_drops", "ttl", "tos",
                "sequence_missing", "sequence_duplicates", "sequence_reordered", "sequence_overflow", "ssrc_changes")
    if any(k not in data for k in required):
        return check("rtp-" + host, "ERROR", "incomplete RTP probe result")
    ch = c["channel"]
    if data.get('source_mismatch') or (data.get("source") and data["source"] != ch["source"]):
        return check("rtp-" + host, "INCONCLUSIVE", "upstream source changed; inspect capture before attributing firmware loss")
    ttl = ch["ingress_ttl"] - (host == "fnos")
    tos = ch["dscp"] * 4 + ch["ecn"]
    wrong = {key: {k: v for k, v in data[key].items() if v and int(k) != expected}
             for key, expected in (("ttl", ttl), ("tos", tos))}
    if any(wrong.values()) or data["invalid_metadata"] or data["non_rtp"]:
        return check("rtp-" + host, "FAIL", "wrong ingress/source/group/TTL/DS field or malformed RTP", wrong=wrong)
    n = data["packets"]
    if data["socket_drops"] or data["sequence_overflow"] or data["ssrc_changes"]:
        return check("rtp-" + host, "INCONCLUSIVE", "receiver overflow, sequence limit or SSRC change")
    if data["sequence_missing"] or data["sequence_duplicates"]:
        return check("rtp-" + host, "FAIL", "RTP interval contains missing/duplicate sequence numbers")
    if data["max_gap_seconds"] > c["thresholds"]["max_read_gap_seconds"]:
        return check("rtp-" + host, "FAIL", "RTP receive gap exceeds configured limit")
    if (n < c["thresholds"]["min_rtp_packets"] or data["rtp_packets"] != n or
            data["seconds"] < c["timing"]["probe_seconds"] * .95 or
            any(sum(data[key].values()) != n for key in ("ttl", "tos"))):
        return check("rtp-" + host, "INCONCLUSIVE", "insufficient or incomplete RTP observations")
    ratio = data["sequence_reordered"] / n
    if ratio > c["thresholds"]["max_reordered_ratio"]:
        return check("rtp-" + host, "FAIL", "reordering exceeds configured limit", reordered_ratio=ratio)
    return check("rtp-" + host, "PASS", "headers correct; no missing/duplicate sequence numbers",
                 reordered=data["sequence_reordered"], reordered_ratio=ratio)


def covered_window(probes, clients, warmup, seconds):
    """Conservative cross-host bounds: no synchronized remote clocks required."""
    lo = max(j['ready_observed'] for j in probes) + warmup
    hi = min(j['started'] for j in probes) + warmup + seconds
    start = min(j['started'] for j in clients)
    end = max(j['finished'] for j in clients)
    good = lo <= start and end <= hi
    return check('observer-window', 'PASS' if good else 'INCONCLUSIVE',
                 'RTP observation encompasses HTTP jobs' if good else 'RTP observation did not encompass all HTTP jobs',
                 observer=[lo, hi], http=[start, end])


def assess_diagnostics(c, samples, host, active):
    if len(samples) < 2 or len({s.get('boot_id') for s in samples}) != 1:
        return check('drops-' + host, 'INCONCLUSIVE', 'missing snapshots or reboot during measurement')
    def udp(s):
        lines = (s.get('snmp') or '').splitlines()
        for i, line in enumerate(lines[:-1]):
            if line.startswith('Udp:'):
                return dict(zip(line.split()[1:], map(int, lines[i+1].split()[1:])))
        return {}
    a, z = udp(samples[0]), udp(samples[-1])
    delta = {k: z[k]-a[k] for k in ('InErrors', 'RcvbufErrors', 'InCsumErrors') if k in a and k in z}
    cdm = [s.get('cdm2_rx_cpu_drop') for s in samples]
    cdm_delta = cdm[-1]-cdm[0] if cdm[0] is not None and cdm[-1] is not None else None
    sockets = {}
    for sample in samples:
        owned = {f['inode'] for f in sample.get('rtp2httpd_fds', [])}
        for sk in sample.get('udp_sockets', []):
            if sk['inode'] in owned and sk['port'] == c['channel']['port'] and sk['address'] in ('0.0.0.0', c['channel']['group']):
                sockets.setdefault(sk['inode'], []).append(sk)
    details = dict(udp_delta=delta, cdm2_drop_delta=cdm_delta, sockets=sockets)
    if any(n < 0 for n in delta.values()) or (cdm_delta is not None and cdm_delta < 0):
        return check('drops-' + host, 'INCONCLUSIVE', 'counter reset/wrap; no negative drop result', **details)
    before = {s['inode']: s.get('drops', 0) for s in samples[0].get('udp_sockets', [])}
    for inode, values in sockets.items():
        if any(s.get('drops', 0) < before.get(inode, 0) for s in values):
            return check('drops-' + host, 'INCONCLUSIVE', 'socket counter reset or inode reuse', **details)
        if any(s.get('drops', 0) > before.get(inode, 0) for s in values):
            return check('drops-' + host, 'FAIL', 'rtp2httpd target UDP socket reports receive drops', **details)
    if active and (not sockets or any('rcvbuf_bytes' not in s for values in sockets.values() for s in values)):
        return check('drops-' + host, 'INCONCLUSIVE', 'target socket capacity/drop evidence unavailable; check UDP_DIAG and /proc permissions', **details)
    if active and host == 'hg' and any(s['rcvbuf_bytes'] < 2*c['hg']['udp_rcvbuf_request'] for values in sockets.values() for s in values):
        return check('drops-' + host, 'FAIL', 'actual HG receive-buffer limit is below the deployment request (Linux accounting doubles it)', **details)
    if any(delta.values()) or cdm_delta:
        return check('drops-' + host, 'INCONCLUSIVE', 'shared device/UDP counters rose; correlate with RTP/TS before attributing to this channel', **details)
    return check('drops-' + host, 'PASS', 'no observed target socket drops; shared counters recorded', **details)


def cpu_summary(lines, warmup, seconds):
    """Monotonic windows. Missing counters/process restart must not look like 0% CPU."""
    values = [json.loads(line) for line in lines.splitlines() if line.strip()]
    meta, rows = values[0], values[1:]
    chosen = [r for r in rows if warmup + 3 <= r["time"] - rows[0]["time"] <= warmup + seconds - 2]
    if len(chosen) < 5 or not meta.get("meta"):
        raise ValueError("insufficient stable CPU samples")
    a, z = chosen[0], chosen[-1]
    elapsed, total = z["time"] - a["time"], z["total"] - a["total"]
    if elapsed <= 0 or total <= 0 or any(len(r["cores"]) != meta["cpus"] for r in chosen):
        raise ValueError("CPU clock/counter/topology changed")
    def delta(key):
        value = z[key] - a[key]
        if value < 0:
            raise ValueError("counter reset: " + key)
        return value
    cores = []
    core_total = 0
    for i in range(meta["cpus"]):
        dt, di = (z["cores"][i][j] - a["cores"][i][j] for j in (0, 1))
        if dt <= 0 or not 0 <= di <= dt:
            raise ValueError("invalid per-core counters")
        core_total += dt
        cores.append(100 * (dt - di) / dt)
    if abs(core_total - total) > max(32 * meta['cpus'], total * .05):
        raise ValueError("per-core counters do not cover the whole-host aggregate")
    if not 0 <= delta("idle") <= total:
        raise ValueError("invalid idle counter")
    out = dict(seconds=elapsed, cpu_total_pct=100 * (total - delta("idle")) / total,
               cpu_softirq_pct=100 * delta("softirq") / total, cores_pct=cores,
               max_core_pct=max(cores), rtp_cpu_one_core_pct=None, net={})
    if len({r["rtp_identity"] for r in chosen}) == 1 and a["rtp_identity"]:
        out["rtp_cpu_one_core_pct"] = 100 * delta("rtp_ticks") / meta["hz"] / elapsed
    frequencies = [r["freq_khz"] / 1000 for r in chosen if r["freq_khz"] is not None]
    out["mean_freq_mhz"] = statistics.mean(frequencies) if frequencies else None
    for dev in a["net"]:
        out["net"][dev] = {}
        for axis in ("rx", "tx"):
            change = z["net"][dev][axis] - a["net"][dev][axis]
            if change < 0:
                raise ValueError("interface counter reset")
            out["net"][dev][axis + "_mbps"] = change * 8 / elapsed / 1e6
    return out


def binding(c, text, ingress):
    ch = c["channel"]
    pattern = (r"\bBIND iif=" + str(ingress) + " " + re.escape(ch["source"]) + ":" + str(ch["source_port"]) +
               "->" + re.escape(ch["group"]) + ":" + str(ch["port"]) + r" outputs=([0-9a-fA-F]+)\b")
    return [int(m, 16) for m in re.findall(pattern, text)]


def port_counters(text, ports):
    """Parse physical ethtool counters, never substitute software netdev TX."""
    result = {}
    for port in ports:
        match = re.search(r'^PORT ' + re.escape(port) + r'\n(.*?)(?=^PORT |\Z)', text, re.M | re.S)
        if not match:
            raise ValueError('missing physical port counters: ' + port)
        block = match[1]
        carrier = re.search(r'^carrier ([01])$', block, re.M)
        tx = re.search(r'^\s*eth-mac-OctetsTransmittedOK:\s*(\d+)\s*$', block, re.M)
        multicast = re.search(r'^\s*eth-mac-MulticastFramesXmittedOK:\s*(\d+)\s*$', block, re.M)
        if not carrier or not tx or not multicast:
            raise ValueError('standard hardware MAC counters/carrier unavailable: ' + port)
        result[port] = dict(carrier=int(carrier[1]), tx_bytes=int(tx[1]), tx_multicast=int(multicast[1]))
    return result


def assess_ports(c, before, after):
    budget = c['path_checks']['max_nonmember_tx_bytes']
    deltas, multicast = {}, {}
    for port in c['path_checks']['nonmember_ports']:
        a, z = before['ports'][port], after['ports'][port]
        if (not a['carrier'] or not z['carrier'] or z['tx_bytes'] < a['tx_bytes'] or
                z['tx_multicast'] < a['tx_multicast']):
            return check('nonmember-ports', 'INCONCLUSIVE', 'port disconnected or counter reset', port=port)
        deltas[port] = z['tx_bytes'] - a['tx_bytes']
        multicast[port] = z['tx_multicast'] - a['tx_multicast']
    busy = [p for p, value in deltas.items() if value > budget]
    return check('nonmember-ports', 'INCONCLUSIVE' if busy else 'PASS',
                 'unexpected physical TX; distinguish unrelated traffic from multicast leakage' if busy else
                 'hardware MAC counters stayed within the total TX byte budget',
                 tx_bytes=deltas, tx_multicast=multicast, max_tx_bytes=budget,
                 seconds=after['monotonic'] - before['monotonic'])


def ipmr_forwarding(c, text):
    group = ipaddress.IPv4Address(c['channel']['group']).packed[::-1].hex().upper()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[0].upper() == group and int(fields[2]) >= 0:
            if any(re.fullmatch(r'\d+:[1-9]\d*', value) for value in fields[6:]):
                return True
    return False


def assess_backend(c, phase, samples, ingress):
    if not samples:
        return check("backend", "INCONCLUSIVE", "no backend samples")
    states = [s["policy"] for s in samples]
    if any(s.get("broken") or s.get("ppe_stopped") for s in states):
        return check("backend", "FAIL", "hardware backend reports a fault")
    for key in ("errors", "bad_copies"):
        if states[-1][key] > states[0][key]:
            return check("backend", "FAIL", "new " + key)
    observations = [binding(c, s["detail"], ingress) for s in samples]
    masks = [mask for found in observations for mask in found]
    if not phase["offload"] or (phase["kind"] == "performance" and phase["placement"] == "hg"):
        return check("backend", "FAIL" if masks else "PASS", "local-only/software phase must have no target BIND")
    if any(mask != 1 << c["hg"]["ppe_bit"] for mask in masks):
        return check("backend", "FAIL", "target flow has an unexpected hardware output mask", masks=masks)
    coverage = sum(bool(found) for found in observations) / len(samples)
    if len(masks) < 2 or coverage < c["thresholds"]["min_bound_sample_ratio"] or not all(s["enabled"] and s["frozen"] and not s["paused"] for s in states):
        return check("backend", "INCONCLUSIVE", "target hardware forwarding not established in repeated samples")
    # Local socket/IGMP membership in these live phases requires CPU copies.
    if states[-1]["delivered"] <= states[0]["delivered"]:
        return check("backend", "INCONCLUSIVE", "BIND present but no delivered CPU-copy evidence")
    return check("backend", "PASS", "exact flow/output BIND and CPU-copy delivery observed", bind_samples=len(masks), bound_sample_ratio=coverage)


def assess_bypass(c, phases):
    """Cross-check BIND with observed software/physical TX and delivered media."""
    values = {}
    try:
        for on in (False, True):
            rows = [p for p in phases if p['kind']=='performance' and p['placement']=='fnos' and p['offload']==on]
            if len(rows) != c['timing']['rounds'] or any(not p.get('cpu',{}).get('hg') for p in rows):
                raise ValueError('missing comparable CPU/interface samples')
            values[str(on)] = {
                'software_tx_mbps': statistics.mean(p['cpu']['hg']['net'][c['hg']['egress']]['tx_mbps'] for p in rows),
                'wire_tx_mbps': statistics.mean(p['cpu']['hg']['net'][c['hg']['wire']]['tx_mbps'] for p in rows),
                'media_mbps': statistics.mean(p['clients']['fnos']['mbps'] for p in rows),
            }
        sw, hw = values['False'], values['True']
        good = (sw['media_mbps'] >= c['thresholds']['min_mbps'] and hw['media_mbps'] >= c['thresholds']['min_mbps']
                and sw['software_tx_mbps'] >= .8 * sw['media_mbps']
                and hw['wire_tx_mbps'] >= .9 * hw['media_mbps']
                and hw['software_tx_mbps'] <= .25 * sw['software_tx_mbps'])
        return check('fnos-hardware-bypass', 'PASS' if good else 'INCONCLUSIVE',
                     'software TX falls while physical TX and media continue' if good else
                     'background traffic/counters do not establish a clear forwarding bypass', measurements=values)
    except (KeyError, ValueError, TypeError) as e:
        return check('fnos-hardware-bypass', 'INCONCLUSIVE', str(e))


def aggregate(phases):
    result = {}
    for host in ("fnos", "hg"):
        for enabled in (False, True):
            selected = [p for p in phases if p["kind"] == "performance" and p["placement"] == host and p["offload"] == enabled]
            valid = [p for p in selected if p.get("cpu", {}).get("hg") and
                     p.get("clients", {}).get(host, {}).get("mbps", 0) > 0 and
                     not any(x['name'].startswith('cpu-') and x['status'] != 'PASS' for x in p.get('checks', []))]
            if not valid:
                continue
            cpu = [p["cpu"]["hg"]["cpu_total_pct"] for p in valid]
            result[f"{host}-{'hw' if enabled else 'sw'}"] = dict(
                rounds=len(valid), status=worst([p.get("status", "ERROR") for p in valid]),
                cpu_mean=statistics.mean(cpu), cpu_min=min(cpu), cpu_max=max(cpu),
                max_core_mean=statistics.mean(p["cpu"]["hg"]["max_core_pct"] for p in valid),
                softirq_mean=statistics.mean(p["cpu"]["hg"]["cpu_softirq_pct"] for p in valid),
                mbps_mean=statistics.mean(p["clients"][host]["mbps"] for p in valid))
    return result
