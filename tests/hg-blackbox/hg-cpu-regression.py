#!/usr/bin/env python3
"""CPU policy black-box regression. Default is local plan only; never flashes."""
import argparse
import datetime as dt
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
sys.path.insert(0, str(HERE/'helpers'))
from hg_cpu_common import config, plan, affinity, latency, compare, family, retry_busy
from hg_iptv_common import worst, EXIT

LOCK = '/tmp/hg-qos-regression.lock'
HASH = 'sha256sum /etc/config/network /etc/config/pon /etc/config/firewall /etc/config/airoha_cpu'
COUNTERS = 'cat /proc/stat /proc/interrupts /proc/softirqs /proc/net/softnet_stat /proc/meminfo'


class AffinityFailure(RuntimeError):
    pass


class Runner:
    def __init__(self, c, output):
        self.c = c
        self.token = 'hcpu-' + uuid.uuid4().hex
        self.out = Path(output or ROOT/'logs/hg-blackbox/cpu'/(
            dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+self.token[-8:])).resolve()
        self.out.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.out.chmod(0o700)
        self.jobs, self.trial, self.locked = [], None, False
        self.report = dict(status='RUNNING', plan=plan(c), phases=[], checks=[], cleanup={})
        self.save()
        (self.out/'script.py').write_bytes(Path(__file__).read_bytes())
        (self.out/'hg_cpu_common.py').write_bytes((HERE/'helpers/hg_cpu_common.py').read_bytes())
        (self.out/'config.json').write_text(json.dumps(c, indent=2)+'\n')

    def ssh(self, role):
        h = self.c[role]
        args = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2']
        if self.c.get('ssh_config'):
            args += ['-F',self.c['ssh_config']]
        if h.get('known_hosts'):
            args += ['-o','UserKnownHostsFile='+h['known_hosts']]
        return args+[h['ssh']]

    def remote(self, role, command, data=None):
        r = subprocess.run(self.ssh(role)+[command], input=data, capture_output=True, timeout=25)
        if r.returncode:
            raise RuntimeError(f'{role}: {r.returncode}: {r.stderr.decode(errors="replace")[-1200:]}')
        return r.stdout.decode()

    def ctl(self, *args):
        return retry_busy(lambda: json.loads(self.remote('hg', shlex.join(['airoha-cpuctl',*args]))))

    def pulse(self):
        # Shared lock expires locally even if this host disappears. The CPU
        # controller separately owns its 180-second rollback lease.
        self.remote('hg', f'test "$(cat {LOCK}/owner)" = {self.token} && '
                    f'read t rest < /proc/uptime && echo $(( ${{t%.*}} + 240 )) > {LOCK}/deadline')
        if self.trial:
            self.ctl('renew', self.trial)

    def preflight(self):
        status = self.ctl('status')
        if status['enabled'] or (status.get('state') or {}).get('active'):
            raise RuntimeError('disable/restore the CPU policy before regression; no persistent policy is overwritten')
        self.ctl('plan')
        self.hashes = self.remote('hg', HASH)
        (self.out/'config-hashes.txt').write_text(self.hashes)
        self.remote('hg', f'umask 077; mkdir {LOCK} && echo {self.token} > {LOCK}/owner')
        self.locked = True
        self.pulse()
        # Detached lock janitor; it removes only its own owner/deadline files.
        janitor = f'''trap '' HUP
while [ "$(cat {LOCK}/owner 2>/dev/null)" = '{self.token}' ]; do
 read t rest < /proc/uptime
 end=$(cat {LOCK}/deadline 2>/dev/null)
 case "$end" in ''|*[!0-9]*) exit 1;; esac
 if [ "${{t%.*}}" -ge "$end" ]; then
  rm -f {LOCK}/deadline {LOCK}/owner
  rmdir {LOCK}
  exit
 fi
 sleep 5
done
'''
        self.remote('hg', 'nohup sh -c '+shlex.quote(janitor)+' </dev/null >/dev/null 2>&1 &')
        if self.c.get('traffic'):
            t = self.c['traffic']
            route = self.remote('client', shlex.join(['ip',family(t),'route','get',t['server'],'from',t['source']]))
            (self.out/'client-route.txt').write_text(route)
            if ' via '+t['gateway']+' ' not in route:
                raise RuntimeError('client route does not use the declared HG gateway')
            self.remote('client','command -v iperf3 >/dev/null && command -v ping >/dev/null')
        self.original = status

    def launch(self, label, argv):
        out = (self.out/(label+'.txt')).open('wb')
        err = (self.out/(label+'.stderr')).open('wb')
        try:
            p = subprocess.Popen(self.ssh('client')+[shlex.join(argv)], stdout=out, stderr=err)
        except BaseException:
            out.close(); err.close(); raise
        job = dict(proc=p,out=out,err=err,label=label)
        self.jobs.append(job)
        return job

    def stop_jobs(self):
        for job in self.jobs:
            p = job['proc']
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=3)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
            job['out'].close(); job['err'].close()
        self.jobs.clear()

    def phase(self, phase, index):
        name = f"{index:02d}-{phase['stage']}-{phase['direction']}"
        row = dict(phase, snapshots=[], checks=[])
        self.report['phases'].append(row)
        print('START '+name, flush=True)
        if not phase['stage'].startswith('baseline'):
            # Know ownership before SSH can fail after the remote apply.
            self.trial = str(uuid.uuid4())
            self.ctl('trial',phase['stage'],'180',self.trial)
        else:
            s = self.ctl('status')
            if (s.get('state') or {}).get('active'):
                raise RuntimeError('baseline still has an active CPU policy')
        self.pulse()
        start = time.monotonic()
        if self.c.get('traffic'):
            t = self.c['traffic']
            args = ['iperf3',family(t),'-c',t['server'],'-p',str(t['port']),'-B',t['source'],
                    '-t',str(phase['seconds']),'-P','4','-J']
            if phase['direction'] == 'down': args += ['-R']
            self.launch(name+'-iperf',args)
            self.launch(name+'-ping',['ping',family(t),'-n','-I',t['source'],'-i','0.2','-c',str(phase['seconds']*5),'-W','1',t['server']])
        end = start + phase['seconds']
        while time.monotonic() < end or any(j['proc'].poll() is None for j in self.jobs):
            if time.monotonic() > end + 25:
                raise TimeoutError('traffic helper exceeded bounded duration')
            self.pulse()
            status = self.ctl('status')
            n = len(row['snapshots'])
            path = self.out/f'{name}-{n:03d}'
            path.with_suffix('.json').write_text(json.dumps(status)+'\n')
            path.with_suffix('.counters').write_text(self.remote('hg', COUNTERS))
            row['snapshots'].append(path.name)
            if self.trial:
                row['checks'].append(affinity(status))
                if row['checks'][-1]['status'] != 'PASS':
                    raise AffinityFailure('CPU affinity drifted during trial')
            time.sleep(5)
        if self.jobs:
            for j in self.jobs:
                j['out'].close(); j['err'].close()
            result = json.loads((self.out/(name+'-iperf.txt')).read_text())
            if result.get('error') or any(j['proc'].returncode not in (0,1) for j in self.jobs):
                raise RuntimeError('traffic probe failed')
            row['mbps'] = result['end']['sum_received']['bits_per_second']/1e6
            try:
                row['latency'] = latency((self.out/(name+'-ping.txt')).read_text())
            except ValueError as e:
                row['checks'].append(dict(status='INCONCLUSIVE',reason=str(e)))
        self.stop_jobs()
        if self.trial:
            restored = self.ctl('restore',self.trial)
            if not restored['restored'] or restored.get('conflicts'):
                raise RuntimeError('CPU rollback incomplete or external mask change')
            self.trial = None
        self.save()
        print('DONE '+name, flush=True)

    def save(self):
        (self.out/'summary.json').write_text(json.dumps(self.report,ensure_ascii=False,indent=2)+'\n')
        lines = ['# HG CPU policy regression','',f"Status: **{self.report['status']}**",'',
                 'Requested/effective IRQ and thread masks are checked. Raw CPU/softirq/softnet/memory counters are retained.',
                 'PPE bypass and IPTV payload integrity require the separate multicast/IPTV regression.', '']
        lines += ['- '+json.dumps(c,ensure_ascii=False) for c in self.report.get('checks',[])]
        lines += ['', 'Cleanup: '+json.dumps(self.report['cleanup']), '', self.report.get('error','')]
        (self.out/'REPORT.md').write_text('\n'.join(lines)+'\n')

    def run(self):
        try:
            self.preflight()
            for index, phase in enumerate(self.report['plan']):
                self.phase(phase,index)
            self.report['checks'] = compare(self.report['phases'])
            self.report['status'] = worst([x['status'] for x in self.report['checks']])
        except AffinityFailure as e:
            self.report.update(status='FAIL',error=str(e))
        except (Exception, KeyboardInterrupt) as e:
            self.report.update(status='ERROR',error=f'{type(e).__name__}: {e}')
        finally:
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,signal.SIG_IGN)
            self.stop_jobs()
            try:
                if self.trial:
                    r = self.ctl('restore',self.trial)
                    if not r['restored'] or r.get('conflicts'): raise RuntimeError('rollback failed/conflicted')
                if hasattr(self,'hashes') and self.remote('hg',HASH) != self.hashes:
                    raise RuntimeError('configuration hashes changed')
                if self.locked:
                    self.remote('hg',f'test "$(cat {LOCK}/owner)" = {self.token} && rm {LOCK}/owner {LOCK}/deadline && rmdir {LOCK}')
                self.report['cleanup'] = dict(status='PASS')
            except Exception as e:
                self.report['cleanup'] = dict(status='ERROR',error=str(e),local_watchdog='restores the leased trial independently')
                self.report['status'] = 'ERROR'
            self.save()
        print(self.report['status']+' '+str(self.out),flush=True)
        return EXIT[self.report['status']]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=HERE/'local/cpu.json')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        c = config(args.config)
        if not args.run:
            print(json.dumps(dict(mode='PLAN ONLY; no SSH',phases=plan(c)),indent=2)); return 0
        def interrupted(sig, frame): raise KeyboardInterrupt(str(sig))
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,interrupted)
        return Runner(c,args.output).run()
    except (ValueError,OSError,KeyError) as e:
        parser.error(str(e))


if __name__ == '__main__':
    sys.exit(main())
