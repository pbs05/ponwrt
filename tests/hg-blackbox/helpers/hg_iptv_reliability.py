"""Non-mutating IPTV reliability profile; shares transport/builds with the legacy runner."""
import datetime as dt
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time

from hg_iptv_common import (EXIT, assess_backend, assess_diagnostics, assess_ports,
                            assess_rtp, assess_stream, binding, check, cpu_summary,
                            ipmr_forwarding, worst)

IFC = 'cat /sys/kernel/debug/airoha_eth-*/pon_multicast_filter'
STATE = """cat /proc/sys/kernel/random/boot_id
sha256sum /etc/config/network /etc/config/pon /etc/config/dhcp /etc/config/firewall /etc/config/igmpproxy /etc/config/rtp2httpd /etc/config/airoha_*
for f in /proc/irq/*/smp_affinity_list /sys/class/net/*/queues/rx-*/rps_cpus /sys/devices/system/cpu/cpufreq/policy*/scaling_governor /sys/devices/system/cpu/cpufreq/policy*/scaling_min_freq /sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq; do
 [ ! -r "$f" ] || { printf '%s ' "$f"; cat "$f"; }
done
"""


def reliability_plan(c):
    r = c['reliability']
    phases = []
    order = [(host, load) for host in ('fnos', 'hg') for load in ('http', 'rtp', 'both')]
    for n in range(r['rounds']):
        for host, load in order if n % 2 == 0 else reversed(order):
            phases.append(dict(name=f'{host}-{load}-{n+1}', kind='reliability',
                               placement=host, load=load, offload='unchanged'))
    for host in ('hg', 'fnos'):
        for n in range(r['join_rounds']):
            phases.append(dict(name=f'join-{host}-{n+1}', kind='join', placement=host))
    phases += [dict(name='leave-first-' + host, kind='overlap', first_leave=host)
               for host in ('hg', 'fnos')]
    return phases


def parse_ifc(raw):
    return dict(re.findall(r'\b([a-z_]+)=([^\s]+)', raw))


def assess_join(data, r):
    delay = data.get('first_packet_delay_seconds')
    if delay is None:
        return check('join-latency', 'FAIL', 'no valid RTP before first-packet timeout')
    if not isinstance(delay, (int, float)) or delay < 0:
        return check('join-latency', 'ERROR', 'invalid same-host monotonic timestamp')
    return check('join-latency', 'PASS' if delay <= r['max_join_seconds'] else 'FAIL',
                 'membership completion to first matching RTP, measured on receiver', seconds=delay)


def assess_probe(data, c, host):
    checks = [assess_rtp(c, data, host)]
    drops = data.get('socket_drops', 0)
    checks.append(check('observer-buffer-' + host, 'FAIL' if drops else 'PASS',
                        'probe socket overflow; wire loss attribution is inconclusive' if drops else 'no measured probe socket overflow',
                        socket_drops=drops, rcvbuf_bytes=data.get('rcvbuf_bytes'), inode=data.get('socket_inode')))
    return checks


def background_proof(channel, evidence, boot, now):
    """Accept fresh per-tuple ingress observations, never UNB rewrite fields.

    Evidence is optional input from an independent passive upstream observer.
    An IFC-wide MIB or UNB timestamp alone cannot prove this specific VID/flow.
    """
    keys = ('group', 'source', 'port', 'source_port', 'ingress_vlan')
    if any(evidence.get(k) != channel[k] for k in keys):
        return False
    return (evidence.get('boot_id') == boot.strip() and
            type(evidence.get('packets')) is int and evidence['packets'] >= 100 and
            isinstance(evidence.get('seconds'), (int, float)) and evidence['seconds'] >= 5 and
            isinstance(evidence.get('observed_at'), (int, float)) and 0 <= now-evidence['observed_at'] <= 120 and
            evidence.get('unsubscribed') is True and evidence.get('filter_active') is True)


class ReliabilityMixin:
    """Use with Runner as second base. Mutating legacy entry points are blocked."""
    def mode(self, *args, **kwargs):
        raise RuntimeError('reliability profile cannot change offload/GRO')

    def quiesce(self):
        raise RuntimeError('reliability profile cannot flush multicast state')

    def guard(self, *args):
        raise RuntimeError('reliability profile cannot invoke the legacy mutation guard')

    def session(self, role, action, *args):
        return self.remote(role, shlex.join(['/bin/sh', self.dirs[role] + '/observe.sh',
                                            self.dirs[role], self.token, action, *args]))

    def preflight(self):
        super().preflight()
        self.report['plan'] = reliability_plan(self.c)
        self.report['profile'] = 'reliability'
        self.original['state'] = self.remote('hg', STATE)
        self.original['wan'] = json.loads(self.remote('hg', 'ubus call network.interface.wan status'))
        if not self.original['wan'].get('up') or self.original['policy']['paused']:
            raise RuntimeError('WAN and multicast offload must already be active')
        self.remote('hg', 'command -v timeout >/dev/null')
        for role in ('fnos', 'receiver'):
            self.remote(role, 'command -v timeout >/dev/null')
        self.last_pulse = 0
        self.report['original_state'] = self.original['state']

    def deploy(self):
        helpers = Path(__file__).parent
        for role in ('hg', 'fnos', 'receiver'):
            directory = shlex.quote(self.dirs[role])
            self.remote(role, 'umask 077; mkdir -m 700 ' + directory)
            self.deployed.append(role)
            self.upload(role, 'owner', self.token + '\n')
            binary = 'stream' if role == 'receiver' else role
            path = self.upload(role, 'probe', Path(self.c['binaries'][binary]).read_bytes())
            self.upload(role, 'observe.sh', (helpers / 'hg_iptv_observe.sh').read_bytes())
            self.remote(role, 'chmod 700 ' + shlex.quote(path))
            if role == 'hg':
                self.upload(role, 'coordinator', '1\n')
            self.session(role, 'pulse')
            command = shlex.join(['/bin/sh', self.dirs[role] + '/observe.sh', self.dirs[role], self.token, 'watchdog'])
            self.remote(role, "(trap '' HUP; " + command + ') </dev/null >' + directory + '/watchdog.log 2>&1 &')
            for _ in range(5):
                if self.remote(role, 'test ! -e ' + directory + '/watchdog.ready || cat ' + directory + '/watchdog.ready').strip() == 'ready':
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('temporary watchdog did not start on ' + role)
        self.pulse()

    def pulse(self):
        for role in self.deployed:
            self.session(role, 'pulse')
        policy = json.loads(self.remote('hg', 'airoha-mcastctl get'))
        if any(policy[k] for k in ('broken', 'ppe_stopped', 'scope_lost')):
            raise RuntimeError('multicast backend fault during reliability test')
        for key in ('enabled', 'frozen', 'paused', 'ingress', 'egress'):
            if policy[key] != self.original['policy'][key]:
                raise RuntimeError('external runtime policy change: ' + key)
        detail = self.remote('hg', 'cat /sys/kernel/debug/ppe/multicast')
        raw = self.remote('hg', IFC)
        self.last_pulse = time.monotonic()
        return dict(policy=policy, detail=detail, ifc=parse_ifc(raw), ifc_raw=raw,
                    observed=time.time(), monotonic=self.last_pulse)

    def launch(self, role, label, arguments):
        out = (self.out / (label + '.jsonl')).open('wb')
        err = (self.out / (label + '.stderr')).open('wb')
        command = ['/bin/sh', self.dirs[role] + '/observe.sh', self.dirs[role], self.token, 'run', '300', *arguments]
        started = time.monotonic()
        try:
            proc = subprocess.Popen(self.ssh(role) + [shlex.join(command)], stdout=out, stderr=err)
        except BaseException:
            out.close(); err.close(); raise
        job = dict(proc=proc, out=out, err=err, role=role, label=label,
                   path=self.out / (label + '.jsonl'), started=started)
        self.jobs.append(job)
        return job

    def wait_jobs(self, jobs, timeout, samples, observe=None):
        end = time.monotonic() + timeout
        while any(j['proc'].poll() is None for j in jobs):
            if time.monotonic() > end:
                raise TimeoutError('bounded reliability probe did not exit')
            if time.monotonic() - self.last_pulse >= 10:
                samples.append(self.pulse())
            for job in jobs:
                if job['proc'].poll() is not None:
                    job.setdefault('finished', time.monotonic())
            if observe:
                observe()
            time.sleep(.2)
        for job in jobs:
            job.setdefault('finished', time.monotonic())
            job['out'].close(); job['err'].close()

    def diagnostics(self, name):
        start = time.monotonic()
        result = super().diagnostics(name)
        self.report.setdefault('diagnostic_intervals', []).append(
            dict(name=name, start=start, end=time.monotonic()))
        return result

    def native(self, name, host, seconds, warmup, first_timeout=0):
        ch = self.c['channel']
        interface = self.c['hg']['ingress'] if host == 'hg' else self.c['fnos']['interface']
        return self.launch(host, name + '-rtp-' + host, ['rtp', interface, ch['group'], str(ch['port']),
                           ch['source'], str(ch['source_port']), str(seconds), str(warmup), str(first_timeout)])

    def natural_leave(self, name):
        deadline = time.monotonic() + self.c['reliability']['leave_seconds']
        while True:
            sample = self.pulse()
            members = self.members() + self.remote('fnos', 'cat /proc/net/igmp')
            routes = self.remote('hg', 'cat /proc/net/ip_mr_cache')
            present = self.target_present(members)
            masks = binding(self.c, sample['detail'], self.ingress)
            forwarding = ipmr_forwarding(self.c, routes)
            filtered = sample['ifc'].get('active') == '1' and sample['ifc'].get('broken') == '0'
            if not present and not masks and not forwarding and filtered:
                result = check('natural-leave', 'PASS', 'members, positive route and target BIND retired; IFC active')
                break
            if time.monotonic() >= deadline:
                result = check('natural-leave', 'INCONCLUSIVE' if present else 'FAIL',
                               'membership still present; no forced flush' if present else 'route/BIND/IFC did not recover',
                               forwarding=forwarding, masks=masks, ifc=sample['ifc'])
                break
            time.sleep(2)
        (self.out / (name + '-leave.txt')).write_text(members + '\n' + routes + '\n' + sample['ifc_raw'])
        return result

    def background(self):
        b = self.c.get('background_channel')
        if not b:
            return check('background-to-subscribed', 'INCONCLUSIVE', 'background tuple not configured; ordinary channel tests are separate')
        original = self.c['channel']
        try:
            self.c['channel'] = dict(original, **b)
            if self.target_present(self.members() + self.remote('fnos', 'cat /proc/net/igmp')):
                return check('background-to-subscribed', 'INCONCLUSIVE', 'background group already subscribed')
            first = self.pulse()
            # Full PPE table is evidence only, outside every traffic measurement.
            raw = self.remote('hg', 'cat /sys/kernel/debug/ppe/entries')
            (self.out / 'background-ppe.txt').write_text(raw)
            time.sleep(5)
            last = self.pulse()
            evidence = json.loads(Path(b['evidence_file']).read_text()) if b.get('evidence_file') else {}
            (self.out / 'background-evidence.json').write_text(json.dumps(evidence, indent=2))
            boot = self.original['state'].splitlines()[0]
            active = all(v['ifc'].get('active') == '1' and v['ifc'].get('broken') == '0' for v in (first, last))
            rising = int(last['ifc'].get('hardware_drops', '-1')) > int(first['ifc'].get('hardware_drops', '-1')) >= 0
            valid = active and rising and background_proof(b, evidence, boot, time.time())
            return check('background-to-subscribed', 'PASS' if valid else 'INCONCLUSIVE',
                         'fresh per-tuple/VID background evidence accepted' if valid else
                         'IFC total drops/UNB alone do not prove sustained traffic for this tuple and ingress VLAN')
        finally:
            self.c['channel'] = original

    def phase(self, p):
        print('START ' + p['name'], flush=True)
        before_leave = self.natural_leave(p['name'] + '-before')
        if before_leave['status'] != 'PASS':
            raise RuntimeError('cannot isolate target before ' + p['name'] + ': ' + before_leave['reason'])
        r = self.c['reliability']
        result = dict(p, checks=[], samples=[], clients={}, rtp={}, cpu={})
        self.report['phases'].append(result)
        ports = self.physical_ports(p['name'] + '-before') if self.c.get('path_checks') else None
        diagnostics = [self.diagnostics(p['name'] + '-before')]
        samplers, probes, clients = {}, {}, {}
        warmup, seconds = r['warmup'], r['seconds']
        if p['kind'] == 'reliability':
            host = p['placement']
            samplers = {h: self.sampler(p['name'], h, warmup + seconds + 12) for h in ('hg', 'fnos')}
            if p['load'] in ('rtp', 'both'):
                probes[host] = self.native(p['name'], host, seconds + (5 if p['load'] == 'both' else 0), warmup)
                self.ready(list(probes.values()))
            if p['load'] in ('http', 'both'):
                clients[host] = self.client(p['name'], host, seconds, warmup)
        else:
            warmup, seconds = 0, r['join_seconds']
            hosts = ('hg', 'fnos') if p['kind'] == 'overlap' else (p['placement'],)
            for host in hosts:
                duration = seconds if p.get('first_leave', host) == host else 2 * seconds + 5
                probes[host] = self.native(p['name'], host, duration, 0, r['first_timeout'])
            self.ready(list(probes.values()))
        started = time.monotonic()
        observed = set()
        def observe():
            for label, at in (('active-early', warmup + 1), ('active-late', warmup + seconds - 3)):
                if label not in observed and time.monotonic()-started >= at:
                    diagnostics.append(self.diagnostics(p['name'] + '-' + label))
                    observed.add(label)
        jobs = list(probes.values()) + list(clients.values()) + list(samplers.values())
        self.wait_jobs(jobs, warmup + 2*seconds + 35, result['samples'], observe)
        diagnostics.append(self.diagnostics(p['name'] + '-after'))
        result['diagnostics'] = diagnostics
        for host, job in clients.items():
            data = json.loads(job['path'].read_text()); result['clients'][host] = data
            result['checks'].append(assess_stream(self.c, data, seconds))
        for host, job in probes.items():
            data = json.loads(job['path'].read_text()); result['rtp'][host] = data
            result['checks'] += assess_probe(data, self.c, host)
            if p['kind'] != 'reliability':
                result['checks'].append(dict(assess_join(data, r), name='join-' + host))
            if data.get('first_packet_delay_seconds') is not None and data.get('seconds', 0) < seconds * .95:
                result['checks'].append(check('receive-duration-' + host, 'INCONCLUSIVE', 'short receive window'))
        for host, job in samplers.items():
            ref = next(iter(clients.values() or probes.values()))
            offset = max(0, ref['started']-job['started'])
            if clients:
                offset += result['clients'][p['placement']].get('ttfb_seconds', 0)
            result['cpu'][host] = cpu_summary(job['path'].read_text(), warmup+offset, seconds)
        if p['kind'] == 'reliability':
            reference = next(iter(clients.values() or probes.values()))
            start = reference['started'] + warmup
            if clients:
                start += result['clients'][p['placement']].get('ttfb_seconds', 0)
            samples = [s for s in result['samples'] if start+2 <= s['monotonic'] <= start+seconds-2]
            result['checks'].append(assess_backend(self.c, dict(kind='performance', placement=p['placement'], offload=True), samples, self.ingress))
        for host in ('hg', 'fnos'):
            result['checks'].append(assess_diagnostics(self.c, [d[host] for d in diagnostics], host, host in clients))
        if p['kind'] == 'overlap':
            first = probes[p['first_leave']]
            other = probes['fnos' if p['first_leave'] == 'hg' else 'hg']
            # Need at least ten seconds after the first subscription exits.
            result['checks'].append(check('remaining-subscriber',
                'PASS' if other['finished']-first['finished'] >= 10 and result['rtp'][other['role']].get('max_gap_seconds', 99) <= self.c['thresholds']['max_read_gap_seconds'] else 'INCONCLUSIVE',
                'continuous native observation spans the other subscriber leaving'))
        result['checks'].append(self.natural_leave(p['name']))
        if ports:
            result['checks'].append(assess_ports(self.c, ports, self.physical_ports(p['name'] + '-after')))
        result['returncodes'] = {j['label']: j['proc'].returncode for j in jobs}
        if any(j['proc'].returncode not in (0, 1) for j in jobs):
            result['checks'].append(check('probe-exit', 'ERROR', 'probe failed; inspect stderr'))
        result['status'] = worst([v['status'] for v in result['checks']])
        print(p['name'] + ': ' + result['status'], flush=True)
        self.save()

    def cleanup(self):
        errors = []
        for role in reversed(self.deployed):
            try:
                self.session(role, 'stop')
            except Exception as e:
                errors.append(str(e))
        self.stop_jobs()
        for role in reversed(self.deployed):
            try:
                self.session(role, 'check')
            except Exception as e:
                errors.append('helper exit could not be verified: ' + str(e))
        if hasattr(self, 'original') and 'state' in self.original:
            try:
                state = self.remote('hg', STATE)
                (self.out / 'final-state.txt').write_text(state)
                if state != self.original['state']:
                    raise RuntimeError('boot/config/CPU affinity changed externally; not overwriting')
                wan = json.loads(self.remote('hg', 'ubus call network.interface.wan status'))
                if not wan.get('up') or wan.get('uptime', 0) < self.original['wan'].get('uptime', 0):
                    raise RuntimeError('WAN disconnected/restarted during test')
                policy = json.loads(self.remote('hg', 'airoha-mcastctl get'))
                for key in ('enabled', 'frozen', 'paused', 'ingress', 'egress'):
                    if policy[key] != self.original['policy'][key]:
                        raise RuntimeError('runtime policy changed during test: ' + key)
                features = self.remote('hg', 'ethtool -k ' + shlex.quote(self.c['hg']['gro_device']))
                if features != (self.out / 'original-features.txt').read_text():
                    raise RuntimeError('network features changed during test')
                bridge = self.c['hg']['bridge']
                observed = self.remote('hg', 'cat ' + ' '.join(shlex.quote('/sys/class/net/' + bridge + '/bridge/' + k)
                                       for k in ('multicast_snooping', 'multicast_querier'))).split()
                if observed != self.original['bridge']:
                    raise RuntimeError('bridge snooping/querier changed during test')
                self.report['cleanup']['state_unchanged'] = True
            except Exception as e:
                errors.append(str(e))
        # On any remote cleanup failure retain watchdogs and the shared lock.
        if not errors:
            for role in reversed(self.deployed):
                try:
                    directory = shlex.quote(self.dirs[role])
                    self.remote(role, 'test "$(cat ' + directory + '/owner)" = ' + shlex.quote(self.token) + ' && rm -rf -- ' + directory)
                except Exception as e:
                    errors.append(str(e))
            if self.locked and not errors:
                lock = '/tmp/hg-qos-regression.lock'
                try:
                    self.remote('hg', 'test "$(cat ' + lock + '/owner)" = ' + shlex.quote(self.token) + ' && rm ' + lock + '/owner && rmdir ' + lock)
                    self.locked = False
                except Exception as e:
                    errors.append(str(e))
        self.report['cleanup'].update(status='ERROR' if errors else 'PASS', errors=errors,
                                      network_settings_written=False)

    def save(self):
        (self.out / 'summary.json').write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')
        (self.out / 'summary.json').chmod(0o600)
        path = self.out / 'REPORT.md'
        lines = ['# IPTV 可靠性回归', '', '结果：**' + self.report['status'] + '**', '',
                 '卸载、IFC、GRO、CPU 调度和频率保持原状。CPU 数值包含相同的诊断采样开销。', '',
                 '| 阶段 | 判定 | HG 总 CPU | 探针丢包 | HTTP TS 连续性错误 |', '|---|---|---:|---:|---:|']
        for p in self.report['phases']:
            lines.append('| {} | {} | {} | {} | {} |'.format(p['name'], p.get('status', 'RUNNING'),
                p.get('cpu', {}).get('hg', {}).get('cpu_total_pct', '—'),
                sum(v.get('socket_drops', 0) for v in p.get('rtp', {}).values()),
                sum(v.get('ts_cc_errors', 0) for v in p.get('clients', {}).values())))
        lines += ['', '## 分项判定', '']
        for item in self.report.get('checks', []):
            lines.append('- {}: {} — {}'.format(item['name'], item['status'], item['reason']))
        for p in self.report['phases']:
            lines.append('- {}: {}'.format(p['name'], p.get('status', 'ERROR')))
            if 'background_verified' in p:
                lines.append('  - 背景流入口证据：' + ('已核验' if p['background_verified'] else '未核验；本项只代表普通入退组'))
            for item in p['checks']:
                lines.append('  - {}: {} — {}'.format(item['name'], item['status'], item['reason']))
        lines += ['', '## 清理与范围', '', '```json', json.dumps(self.report['cleanup'], ensure_ascii=False, indent=2), '```', '',
                  '未覆盖项目：'] + ['- ' + s for s in self.report['not_covered']]
        lines += ['', '此模式不改变卸载状态，不能据此推断开关卸载的因果性能收益。',
                  '未配置非订阅物理口或其计数受其他业务干扰时，不能宣称已排除洪泛。']
        if self.report.get('error'):
            lines += ['', '执行异常：' + self.report['error']]
        path.write_text('\n'.join(lines) + '\n')

    def run(self):
        self.report['plan'] = reliability_plan(self.c)
        self.report['checks'] = []
        try:
            self.preflight(); self.deploy()
            for p in self.report['plan']:
                if p['kind'] != 'reliability':
                    continue
                self.phase(p)
            background = self.background()
            self.report['checks'].append(background)
            original = self.c['channel']
            if background['status'] == 'PASS':
                self.c['channel'] = dict(original, **self.c['background_channel'])
            try:
                for p in self.report['plan']:
                    if p['kind'] == 'reliability':
                        continue
                    self.phase(dict(p, background_verified=background['status'] == 'PASS'))
            finally:
                self.c['channel'] = original
            self.report['status'] = worst([p['status'] for p in self.report['phases']] + [v['status'] for v in self.report['checks']])
        except (Exception, KeyboardInterrupt) as e:
            self.report.update(status='ERROR', error=type(e).__name__ + ': ' + str(e))
            for p in self.report['phases']:
                if 'status' not in p:
                    p['status'] = 'ERROR'
                    p['checks'].append(check('execution', 'ERROR', self.report['error']))
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            self.cleanup()
            self.report['status'] = worst([self.report['status'], self.report['cleanup']['status']])
            self.report['finished'] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.save()
        print(self.report['status'] + ' ' + str(self.out), flush=True)
        return EXIT[self.report['status']]
