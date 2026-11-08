"""Offline decisions for the opt-in ingress CPU policy regression."""
import ipaddress
import json
from pathlib import Path
import re
import statistics
import time


def config(path):
    path = Path(path).resolve()
    c = json.loads(path.read_text())
    for role in ('hg', 'client'):
        if role not in c:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', c[role]['ssh']):
            raise ValueError('SSH aliases only; no option injection')
        if c[role].get('known_hosts'):
            c[role]['known_hosts'] = str((path.parent / c[role]['known_hosts']).resolve())
    if c.get('ssh_config'):
        c['ssh_config'] = str((path.parent / c['ssh_config']).resolve())
    for k, default, lo, hi in (('seconds', 180, 15, 180), ('rounds', 3, 1, 4)):
        n = c.setdefault(k, default)
        if type(n) is not int or not lo <= n <= hi:
            raise ValueError(f'{k} must be {lo}..{hi}')
    if c.get('traffic'):
        if 'client' not in c:
            raise ValueError('traffic needs a separate Linux client')
        versions = set()
        for key in ('server', 'source', 'gateway'):
            a = ipaddress.ip_address(c['traffic'][key])
            if a.is_multicast or a.is_unspecified or a.is_loopback:
                raise ValueError('unicast traffic addresses required')
            if getattr(a, 'scope_id', None):
                raise ValueError('use an unscoped gateway address as shown by ip route')
            versions.add(a.version)
            c['traffic'][key] = str(a)
        if len(versions) != 1:
            raise ValueError('server, source and gateway must use the same address family')
        port = c['traffic'].setdefault('port', 5201)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('invalid iperf port')
    return c


def family(traffic):
    return '-6' if ipaddress.ip_address(traffic['server']).version == 6 else '-4'


def retry_busy(operation, pause=None):
    """The local lease watchdog may briefly own the controller operation lock.

    Only this pre-operation rejection is safe to retry; other failures propagate.
    """
    pause = pause or time.sleep
    for attempt in range(15):
        try:
            return operation()
        except RuntimeError as exc:
            if 'Another CPU policy operation is active' not in str(exc) or attempt == 14:
                raise
            pause(.2)


def plan(c):
    out = []
    for round_id in range(c['rounds']):
        stages = ['network', 'services', 'full']
        if round_id % 2:
            stages.reverse()
        for stage in ['baseline'] + stages + ['baseline-after']:
            for direction in (['up', 'down'] if c.get('traffic') else ['idle']):
                out.append(dict(round=round_id+1, stage=stage, direction=direction, seconds=c['seconds']))
    return out


def affinity(status):
    state = status.get('state') or {}
    if not state.get('active') or not state.get('trial'):
        return {'status': 'FAIL', 'reason': 'trial is not active'}
    bad = [v for v in status['actual'] if v['value'] is not None and
           (v['value'] != v['want'] or (v['effective'] is not None and v['effective'] != v['want']))]
    required = {o['id'] for o in state['ops'] if o.get('role') == 'napi' or o['kind'] == 'irq'}
    absent = [v['id'] for v in status['actual'] if v['id'] in required and v['value'] is None]
    return {'status': 'FAIL' if bad or absent else 'PASS', 'mismatch': bad, 'absent': absent,
            'reason': 'requested/effective IRQ and current thread affinity'}


def latency(text):
    values = sorted(float(n) for n in re.findall(r'time[=<]([0-9.]+)\s*ms', text))
    loss = re.search(r'([0-9.]+)% packet loss', text)
    if len(values) < 50 or not loss:
        raise ValueError('insufficient complete ping sample')
    return dict(p99_ms=values[min(len(values)-1, int(len(values)*.99))], samples=len(values), loss_pct=float(loss[1]))


def compare(rows):
    results = []
    for row in rows:
        if row['stage'].startswith('baseline') or row['direction'] == 'idle':
            continue
        bases = [r for r in rows if r['round'] == row['round'] and r['direction'] == row['direction']
                 and r['stage'].startswith('baseline') and 'mbps' in r and 'latency' in r]
        key = dict(round=row['round'], stage=row['stage'], direction=row['direction'])
        if len(bases) != 2 or 'mbps' not in row or 'latency' not in row:
            results.append(dict(key, status='INCONCLUSIVE', reason='missing bracketing performance samples'))
            continue
        rates = [r['mbps'] for r in bases]
        rtts = [r['latency']['p99_ms'] for r in bases]
        base_rate, base_rtt = statistics.mean(rates), statistics.mean(rtts)
        if base_rate <= 0 or max(rates)-min(rates) > base_rate*.10 or max(rtts)-min(rtts) > max(base_rtt*.20, 1):
            results.append(dict(key, status='INCONCLUSIVE', reason='baseline drift; repeat under controlled external conditions'))
            continue
        rate_ok = row['mbps'] >= base_rate*.95
        jitter_ok = row['latency']['p99_ms'] <= base_rtt + max(.5, base_rtt*.10)
        loss_ok = row['latency']['loss_pct'] <= max(r['latency']['loss_pct'] for r in bases)
        results.append(dict(key, status='PASS' if rate_ok and jitter_ok and loss_ok else 'FAIL',
                            reason='throughput >=95%; P99 increase <= max(10%,0.5ms); no added ping loss',
                            throughput_ratio=row['mbps']/base_rate, baseline_p99_ms=base_rtt,
                            p99_ms=row['latency']['p99_ms']))
    if not results:
        results.append(dict(status='INCONCLUSIVE', reason='no external WAN traffic; affinity checks alone do not establish jitter/throughput'))
    return results
