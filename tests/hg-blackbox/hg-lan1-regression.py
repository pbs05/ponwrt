#!/usr/bin/env python3
"""Read-only LAN1 health observation; reboot/load require explicit options."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NUMBERS = ('carrier', 'speed', 'carrier_changes', 'rx_packets', 'tx_packets',
           'rx_bytes', 'tx_bytes', 'rx_errors', 'tx_errors', 'rx_dropped',
           'tx_dropped', 'rx_crc_errors', 'pcs_rx_sync', 'internal_port_link',
           'freqdet_lock', 'mac_stop_bits')
ERRORS = ('rx_errors', 'tx_errors', 'rx_crc_errors')
EXIT = {'PASS': 0, 'FAIL': 1, 'INCONCLUSIVE': 2, 'ERROR': 3}


def parse_snapshot(text):
    fields, marker, log = text.partition('__KERNEL_LOG__\n')
    if not marker:
        raise ValueError('missing snapshot terminator')
    result = {}
    for line in fields.splitlines():
        k, sep, value = line.partition('=')
        if not sep or k in result:
            raise ValueError('malformed/duplicate snapshot field')
        result[k] = value
    if not re.fullmatch(r'[0-9a-f-]{36}', result.get('boot_id', '')):
        raise ValueError('missing boot identity')
    for key in NUMBERS:
        result[key] = int(result[key])
    result['uptime'] = float(result['uptime'])
    result['kernel_log'] = log.splitlines()
    return result


def evaluate(samples, event='none', expected_speed=2500, warmup=30):
    failures, limits = [], []
    good = [s for s in samples if 'data' in s]
    if len(good) < 2:
        return 'INCONCLUSIVE', ['fewer than two usable samples']
    boots = {s['data']['boot_id'] for s in good}
    changes = 0
    bad_since = None
    stable = 0
    for previous, sample in zip(good, good[1:]):
        old, new = previous['data'], sample['data']
        same_boot = old['boot_id'] == new['boot_id']
        if not same_boot:
            bad_since = None
        else:
            delta = new['carrier_changes'] - old['carrier_changes']
            if delta < 0:
                limits.append('carrier counter reset without reboot')
            changes += max(0, delta)
            if event == 'none' and delta > 0:
                failures.append('unexpected carrier change')
            for key in ERRORS:
                if new[key] > old[key]:
                    failures.append(key + ' increased')
                elif new[key] < old[key]:
                    limits.append(key + ' reset without reboot')
        if event != 'none' and sample['elapsed'] < warmup:
            continue
        healthy = (new['carrier'] == 1 and new['speed'] == expected_speed and
                   new['pcs_rx_sync'] == 1 and new['freqdet_lock'] == 1 and
                   new['mac_stop_bits'] == 0)
        if healthy:
            bad_since = None
            stable += 1
        elif bad_since is None:
            bad_since = sample['elapsed']
        elif sample['elapsed'] - bad_since >= 3:
            failures.append('persistent link/PCS fault after warmup')
        if new.get('led_offloaded') != '1':
            failures.append('LAN1 LED is not hardware controlled')
    if event == 'none' and len(boots) != 1:
        failures.append('unexpected HG reboot')
    if event == 'reboot' and len(boots) < 2:
        limits.append('new HG boot not observed')
    if event in ('link-change', 'peer-restart') and not changes:
        limits.append('requested physical event not observed')
    if stable < 2:
        limits.append('insufficient stable samples')
    if any('error' in s and (event == 'none' or s['elapsed'] >= warmup)
           for s in samples):
        limits.append('SSH observation gaps; management-path failures are not proof of HG failure')
    reasons = sorted(set(failures + limits))
    return ('FAIL' if failures else 'INCONCLUSIVE' if limits else 'PASS'), reasons


def ssh_args(args):
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=3',
           '-o', 'ServerAliveCountMax=2']
    if args.ssh_config:
        cmd += ['-F', str(Path(args.ssh_config).resolve())]
    if args.known_hosts:
        cmd += ['-o', 'UserKnownHostsFile=' + str(Path(args.known_hosts).resolve())]
    return cmd + ['--', args.ssh]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ssh', required=True, help='existing OpenSSH alias or user@host')
    p.add_argument('--ssh-config')
    p.add_argument('--known-hosts')
    p.add_argument('--duration', type=float, default=60)
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--expect-speed', type=int, choices=(100, 1000, 2500), default=2500)
    p.add_argument('--event', choices=('none', 'link-change', 'peer-restart', 'reboot'), default='none',
                   help='link-change/peer-restart are performed manually during observation')
    p.add_argument('--warmup', type=float, default=30,
                   help='initial grace period in seconds for an explicit event')
    p.add_argument('--output')
    p.add_argument('--traffic-command', nargs=argparse.REMAINDER,
                   help='explicit local argv, last option; no shell expansion')
    args = p.parse_args()
    if args.duration < 15 or args.interval < 1 or args.interval > 60 or args.warmup < 0:
        p.error('duration >=15, interval 1..60 and warmup >=0 required')
    if args.duration <= args.interval * 2 or (args.event != 'none' and args.duration <= args.warmup + args.interval * 2):
        p.error('duration must leave at least three samples after warmup')
    if args.event == 'reboot' and args.traffic_command:
        p.error('separate reboot and load rounds so their results remain attributable')
    output = Path(args.output or ROOT / 'logs/hg-blackbox/lan1' /
                  dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    helper = (HERE / 'helpers/hg_lan1_snapshot.sh').read_text()
    (output / 'snapshot.sh').write_text(helper)
    samples, traffic = [], None
    report = {'status': 'RUNNING', 'event': args.event,
              'scope': 'LAN1 observable health; no PHY/PCS fault injection or offload throughput proof',
              'samples': samples, 'physical_recovery_proven': False}
    started = time.monotonic()

    def capture():
        item = {'elapsed': time.monotonic() - started,
                'utc': dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            r = subprocess.run(ssh_args(args) + ['sh -s'], input=helper, text=True,
                               capture_output=True, timeout=20)
            (output / f'snapshot-{len(samples):05}.txt').write_text(r.stdout)
            if r.returncode:
                raise RuntimeError(f'SSH/snapshot exit {r.returncode}: {r.stderr[-500:]}')
            item['data'] = parse_snapshot(r.stdout)
        except (subprocess.SubprocessError, ValueError, KeyError, OSError, RuntimeError) as e:
            item['error'] = str(e)
        samples.append(item)
        (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')

    try:
        capture()
        if 'data' not in samples[0]:
            raise RuntimeError('initial snapshot unavailable; no event/load started')
        if args.event == 'reboot':
            r = subprocess.run(ssh_args(args) + ['reboot'], capture_output=True, timeout=15)
            if r.returncode not in (0, 255):
                raise RuntimeError('reboot command rejected')
        if args.traffic_command:
            log = (output / 'traffic.log').open('wb')
            try:
                traffic = subprocess.Popen(args.traffic_command, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                log.close()
        while time.monotonic() - started < args.duration:
            time.sleep(min(args.interval, max(0, args.duration - (time.monotonic() - started))))
            capture()
        report['status'], report['reasons'] = evaluate(samples, args.event, args.expect_speed, args.warmup)
        if traffic and traffic.poll() not in (0, None):
            report['status'] = 'ERROR'
            report['reasons'].append('traffic command failed; inspect traffic.log')
    except (Exception, KeyboardInterrupt) as e:
        report.update(status='ERROR', reasons=[str(e)])
    finally:
        if traffic and traffic.poll() is None:
            try:
                os.killpg(traffic.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                traffic.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(traffic.pid, signal.SIGKILL)
                traffic.wait()
            report['traffic_stopped_at_end'] = True
        (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
        (output / 'REPORT.md').write_text(
            '# HG LAN1 observation\n\nStatus: ' + report['status'] + '\n\n' +
            report['scope'] + '\n\n' + '\n'.join('- ' + x for x in report.get('reasons', [])) + '\n')
    print(f"{report['status']}: {output}")
    return EXIT[report['status']]


if __name__ == '__main__':
    sys.exit(main())
