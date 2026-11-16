"""PSE stress profile: current forwarding policy, bounded probes and owned logs."""
import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import math
import re
from pathlib import Path
import shlex
import signal
import statistics
import subprocess
import time
from urllib.parse import urlsplit

from hg_iptv_common import EXIT, assess_stream, binding, check, cpu_summary, worst
from hg_iptv_reliability import assess_probe

PREFIX = Path(__file__).resolve().parents[1] / 'configs/pse-switching-prefix.json'


def stress_plan(c, require_channels=False):
    settings = c.get('pse_stress', {})
    seconds = settings.get('seconds', 1800)
    if type(seconds) is not int or not 610 <= seconds <= 3600:
        raise ValueError('pse_stress.seconds must be 610..3600')
    if settings.get('expected_path') not in (None,'switch','replicate'):
        raise ValueError('expected_path must be switch or replicate')
    receivers=settings.get('receivers',[])
    if not isinstance(receivers,list) or len(receivers)>3:
        raise ValueError('pse_stress.receivers must be a list with at most three peers')
    ports={c['hg']['port']};bits={c['hg']['ppe_bit']};addresses={c['fnos']['address']}
    for peer in receivers:
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*',peer['ssh']) or peer['arch'] not in ('aarch64','x86_64'):
            raise ValueError('invalid peer SSH alias or architecture')
        for key in ('interface','hg_port'):
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}',peer[key]):
                raise ValueError('invalid peer interface/port')
        address=ipaddress.IPv4Address(peer['address'])
        bit=peer['ppe_bit']
        if address.is_multicast or address.is_unspecified or address.is_loopback:
            raise ValueError('peer requires a unicast address')
        if type(bit) is not int or bit not in (0,1,2,3,4,5,13):
            raise ValueError('peer ppe_bit is not a supported physical output')
        if peer['hg_port'] in ports or bit in bits or peer['address'] in addresses:
            raise ValueError('multiport peers require distinct physical outputs and addresses')
        if peer['hg_port'] in c.get('path_checks',{}).get('nonmember_ports',[]):
            raise ValueError('a subscribed peer cannot be a nonmember observer')
        ports.add(peer['hg_port']);bits.add(bit);addresses.add(peer['address'])
    if require_channels:
        ipaddress.IPv4Address(urlsplit(c['hg']['url']).hostname)
    fixture = json.loads(PREFIX.read_text())
    channels = settings.get('channels', [])
    if require_channels and len(channels) != fixture['channel_slots']:
        raise ValueError('pse_stress requires exactly %d local channel slots' % fixture['channel_slots'])
    if channels:
        if len(channels) != fixture['channel_slots']:
            raise ValueError('provide every channel slot from pse-switching-prefix.json')
        for entry in channels:
            # Reuse the ordinary tuple/URL validators without writing a file.
            for role in ('hg', 'fnos'):
                url = urlsplit(entry['urls'][role])
                suffix = '/rtp/%s:%d' % (entry['group'], entry['port'])
                if (url.scheme not in ('http', 'https') or not url.hostname or url.username or
                        url.password or url.query or url.fragment or not url.path.endswith(suffix)):
                    raise ValueError('stress channels require direct credential-free RTP URLs')
            if ipaddress.IPv4Address(entry['group']) not in ipaddress.IPv4Network('239.0.0.0/8'):
                raise ValueError('stress channel is not an IPTV multicast group')
            source = ipaddress.IPv4Address(entry['source'])
            if source.is_multicast or source.is_unspecified or source.is_loopback:
                raise ValueError('stress channel requires a unicast source')
            for key, low, high in (('port', 1024, 65535), ('source_port', 1, 65535),
                                   ('ingress_ttl', 2, 255), ('dscp', 0, 63), ('ecn', 0, 3)):
                if type(entry[key]) is not int or not low <= entry[key] <= high:
                    raise ValueError('invalid stress channel ' + key)
    actions = []
    # Replay complete prefixes only; 610s is the original three-round case.
    for start in range(0, seconds-195+1, fixture['period_seconds']):
        for action in fixture['actions']:
            at = start + action['at']
            if at < seconds:
                actions.append(dict(action, at=at))
    for index, action in enumerate(actions):
        following = next((row['at'] for row in actions[index+1:] if row['role'] == action['role']), seconds)
        action['wall_seconds'] = min(180, min(seconds, following + fixture['overlap_seconds']) - action['at'])
        if not 1 <= action['wall_seconds'] <= 180:
            raise ValueError('stress action lifetime falls outside bounded HTTP probe limits')
    return dict(seconds=seconds, overlap_seconds=fixture['overlap_seconds'], actions=actions,
                required_channel_slots=fixture['channel_slots'], channels_ready=bool(channels),
                multiport_receivers=len(receivers),
                fixture_sha256=hashlib.sha256(PREFIX.read_bytes()).hexdigest())


def assess_pse(rows, seconds):
    if not rows:
        return check('pse-buffers', 'ERROR', 'no actual FQ observations')
    ordered = sorted(rows, key=lambda row: row['monotonic'])
    minimum = min(row['fq'] for row in ordered)
    alarms = sum(bool(row['isr'] & 256) for row in ordered)
    if minimum < 1024 or alarms:
        return check('pse-buffers', 'FAIL', 'low actual FQ or PSE_FQ_EMPTY', minimum=minimum, alarms=alarms)
    gap = max((b['monotonic']-a['monotonic'] for a,b in zip(ordered, ordered[1:])), default=math.inf)
    span = ordered[-1]['monotonic']-ordered[0]['monotonic']
    if span < seconds*.98 or gap > .25:
        return check('pse-buffers', 'INCONCLUSIVE', 'incomplete observation window', seconds=span, max_gap=gap)
    return check('pse-buffers', 'PASS', 'actual FQ observed without exhaustion', minimum=minimum,
                 alarms=alarms, samples=len(ordered), max_gap=gap)


def irq_counts(raw):
    if '# airoha-qdma-irq-errors 1' not in raw:
        raise ValueError('IRQ error diagnostics unavailable')
    rows = [line.split() for line in raw.splitlines() if line and not line.startswith('#')]
    if not rows or any(len(row) != 8 for row in rows):
        raise ValueError('malformed IRQ error diagnostics')
    return {(int(row[0]), int(row[1]), int(row[3])): int(row[4]) for row in rows}


def assess_switching_hardware(c, catalog, actions, samples, began, ingress):
    eligible = found = 0
    expected_path = c.get('pse_stress', {}).get('expected_path')
    for sample in samples:
        at = sample['monotonic']-began
        active = [a for a in actions if a['role']=='fnos' and a['at']+3<=at<a['at']+a['wall_seconds']-1]
        if not active:
            continue
        action=active[-1]; channel=catalog[action['channel']]
        masks=binding(dict(c,channel=channel),sample['detail'],ingress)
        eligible+=1
        if masks==[1<<c['hg']['ppe_bit']]:
            found+=1
        if expected_path and masks:
            lines=[line for line in sample['detail'].splitlines()
                   if f"->{channel['group']}:{channel['port']} " in line and ' BIND ' in line]
            if any(' path='+expected_path+' ' not in line for line in lines):
                return check('switching-hardware','FAIL','unexpected programmed egress path')
    if eligible<10:
        return check('switching-hardware','INCONCLUSIVE','insufficient settled fnOS BIND observations',samples=eligible)
    ratio=found/eligible
    return check('switching-hardware','PASS' if ratio>=c['thresholds']['min_bound_sample_ratio'] else 'FAIL',
                 'exact routed tuple/output hardware evidence',bound_ratio=ratio,samples=eligible)


def compare_performance(base, candidate):
    fields = ('configuration', 'catalog', 'fixture', 'background_controlled')
    if any(base.get(k) != candidate.get(k) for k in fields) or not base.get('background_controlled'):
        return check('SDK-path-performance', 'INCONCLUSIVE', 'configuration/load/background comparison is not controlled')
    if any(len(report.get('rounds', [])) < 3 for report in (base, candidate)):
        return check('SDK-path-performance', 'INCONCLUSIVE', 'three complete rounds required')
    limits = dict(mbps=.95, cpu_total_pct=1, max_core_pct=3, ping_p99_ms=2)
    observed = {}
    for key, limit in limits.items():
        try:
            a = statistics.median(row[key] for row in base['rounds'])
            b = statistics.median(row[key] for row in candidate['rounds'])
        except (KeyError, TypeError, statistics.StatisticsError):
            return check('SDK-path-performance', 'INCONCLUSIVE', 'measurement missing: ' + key)
        if not all(isinstance(v, (int,float)) and math.isfinite(v) for v in (a,b)):
            return check('SDK-path-performance', 'ERROR', 'invalid measurement: ' + key)
        observed[key] = dict(baseline=a, candidate=b)
        if (key == 'mbps' and b < a*limit) or (key != 'mbps' and b-a > limit):
            return check('SDK-path-performance', 'FAIL', 'configured comparison limit exceeded', observed=observed)
    return check('SDK-path-performance', 'PASS', 'fixed-program load meets comparison limits; not maximum throughput', observed=observed)


class PseStressMixin:
    def preflight(self):
        self.stress = stress_plan(self.c, require_channels=True)
        super().preflight()
        self.report['plan'] = self.stress['actions']
        self.report['profile'] = 'pse-stress'
        self.report['not_covered'] = [
            'FCC and decoded picture quality; requests use direct RTP URLs',
            'RTP sequence integrity and physical multicast isolation require the separate reliability/path tests',
            'Maximum throughput, power consumption, GEM/LLID isolation and IPv6 multicast',
        ]
        self.catalog = self.c['pse_stress']['channels']
        self.remote('fnos', 'command -v ping >/dev/null')
        raw = self.members() + self.remote('fnos', 'cat /proc/net/igmp')
        original = self.c['channel']
        try:
            for entry in self.catalog:
                self.c['channel'] = entry
                if self.target_present(raw):
                    raise RuntimeError('stress catalog already has a subscriber: ' + entry['group'])
        finally:
            self.c['channel'] = original
        self.irq_before = irq_counts(self.remote('hg', 'cat /sys/kernel/debug/airoha_eth-*/qdma_irq_errors'))
        self.peers=[]
        for index,entry in enumerate(self.c['pse_stress'].get('receivers',[]),1):
            role='peer%d'%index
            self.c[role]=entry;self.dirs[role]=self.directory+'d'+format(index,'x')
            self.peers.append(role)
            if self.remote(role,'uname -m').strip()!=entry['arch']:
                raise RuntimeError('peer architecture mismatch: '+role)
            output=self.remote(role,'ip -4 address show dev '+shlex.quote(entry['interface']))
            if not any(line.strip().startswith('inet '+entry['address']+'/') for line in output.splitlines()):
                raise RuntimeError('peer address is not local to its selected interface')
            mac=self.remote(role,'cat '+shlex.quote('/sys/class/net/'+entry['interface']+'/address')).strip().lower()
            fdb=self.remote('hg','bridge fdb show')
            if not any(line.lower().startswith(mac+' ') and ' dev '+entry['hg_port']+' ' in line+' ' for line in fdb.splitlines()):
                raise RuntimeError('peer MAC is not learned on its declared HG port')
            if self.remote('hg','cat '+shlex.quote('/sys/class/net/'+entry['hg_port']+'/carrier')).strip()!='1':
                raise RuntimeError('peer HG port has no carrier')
            self.remote('hg','test -d '+shlex.quote('/sys/class/net/'+self.c['hg']['bridge']+'/brif/'+entry['hg_port']))

    def deploy(self):
        super().deploy()
        for role in self.peers:
            directory=shlex.quote(self.dirs[role])
            self.remote(role,'umask 077; mkdir -m 700 '+directory)
            self.deployed.append(role)
            self.upload(role,'owner',self.token+'\n')
            binary='fnos' if self.c[role]['arch']==self.c['fnos']['arch'] else 'hg'
            self.upload(role,'probe',Path(self.c['binaries'][binary]).read_bytes())
            self.upload(role,'observe.sh',(Path(__file__).parent/'hg_iptv_observe.sh').read_bytes())
            self.remote(role,'chmod 700 '+shlex.quote(self.dirs[role]+'/probe'))
            self.session(role,'pulse')
            command=shlex.join(['/bin/sh',self.dirs[role]+'/observe.sh',self.dirs[role],self.token,'watchdog'])
            self.remote(role,"(trap '' HUP; "+command+') </dev/null >'+directory+'/watchdog.log 2>&1 &')

    def launch(self, role, label, arguments):
        # Keep probe output on the sending host as well as on the controller.
        out = (self.out/(label+'.jsonl')).open('wb')
        err = (self.out/(label+'.stderr')).open('wb')
        remote_path = self.dirs[role]+'/'+label+'.jsonl'
        status = self.dirs[role]+'/'+label+'.exit'
        command = shlex.join(['/bin/sh', self.dirs[role]+'/observe.sh', self.dirs[role],
                              self.token, 'run', '300', *arguments])
        command = ("umask 077\n( " + command + "; printf '%s' \"$?\" > " + shlex.quote(status) +
                   " ) | tee " + shlex.quote(remote_path) + "\ntest \"$(cat " + shlex.quote(status) + ")\" = 0")
        started = time.monotonic()
        try:
            proc = subprocess.Popen(self.ssh(role)+[command], stdout=out, stderr=err)
        except BaseException:
            out.close(); err.close(); raise
        job = dict(proc=proc, out=out, err=err, role=role, label=label, path=self.out/(label+'.jsonl'),
                   started=started, remote_path=remote_path, offset=0, pending='')
        self.jobs.append(job)
        return job

    def rows(self, job):
        with job['path'].open() as f:
            f.seek(job['offset']); data=job['pending']+f.read(); job['offset']=f.tell()
        lines=data.split('\n'); job['pending']=lines.pop()
        return [json.loads(line) for line in lines if line.strip()]

    def checkpoint(self, label):
        raw = self.remote('hg', 'cat /sys/kernel/debug/airoha_eth-*/qdma_irq_errors')
        (self.out/(label+'-irq.txt')).write_text(raw)
        after=irq_counts(raw)
        if after.keys()!=self.irq_before.keys() or any(after[k]!=v for k,v in self.irq_before.items()):
            raise RuntimeError('acknowledged QDMA error counter changed')
        sample=self.pulse()
        if any(sample['policy'][key] != self.original['policy'][key] for key in ('errors','bad_copies')):
            raise RuntimeError('multicast backend reported new errors')
        return sample

    def transaction_snapshot(self, label):
        # Normal images omit the temporary fault journal. Its absence must
        # not prevent a black-box regression; record the evidence boundary.
        raw = self.remote('hg',
            "if test -e /sys/kernel/debug/ppe/multicast_transactions; then "
            "cat /sys/kernel/debug/ppe/multicast_transactions; else "
            "printf '# unavailable: optional multicast transaction journal not built\\n'; fi")
        (self.out / ('transactions-' + label + '.txt')).write_text(raw)
        available = raw.startswith('# airoha-mcast-transactions ')
        self.report.setdefault('diagnostics', {}).setdefault('multicast_transactions', {})[label] = available

    def cleanup(self):
        super().cleanup()
        if self.report['cleanup']['status']=='PASS' and hasattr(self,'fault_directory'):
            try:
                self.remote('hg','rmdir '+shlex.quote(self.fault_directory))
            except Exception as error:
                self.report['cleanup']['status']='ERROR'
                self.report['cleanup']['errors'].append('fault directory retained: '+str(error))

    def multiport(self):
        if not self.peers:
            self.report['multiport_qualification']='NOT_CONFIGURED'
            return []
        results=[];original=self.c['channel'];self.c['channel']=self.catalog[0]
        ch=self.c['channel'];roles=['fnos']+self.peers
        mask=(1<<self.c['hg']['ppe_bit'])
        for role in self.peers:mask|=1<<self.c[role]['ppe_bit']
        try:
            for local in (False,True):
                name='multiport-'+('local' if local else 'forward-only')
                jobs={};samples=[]
                before=self.physical_ports(name+'-before') if self.c.get('path_checks') else None
                for role in roles+(['hg'] if local else []):
                    interface=self.c['hg']['ingress'] if role=='hg' else self.c[role]['interface']
                    duration=20 if role=='fnos' else 40 if role=='hg' else 35
                    jobs[role]=self.launch(role,name+'-'+role,['rtp',interface,ch['group'],str(ch['port']),
                        ch['source'],str(ch['source_port']),str(duration),'3','5'])
                self.ready(list(jobs.values()))
                began=time.monotonic()
                last_observed=[began]
                def observe():
                    if observer['proc'].poll() not in (None,0):
                        raise RuntimeError('multiport PSE observer reported a fault')
                    if time.monotonic()-last_observed[0]>=2:
                        samples.append(self.pulse())
                        last_observed[0]=time.monotonic()
                observer=self.launch('hg',name+'-pse',['pse','50',self.fault_directory])
                self.wait_jobs(list(jobs.values()),65,samples,observe)
                for role,job in jobs.items():
                    data=json.loads(job['path'].read_text())
                    for item in assess_probe(data,self.c,'hg' if role=='hg' else 'fnos'):
                        results.append(dict(item,name=name+'-'+role+'-'+item['name']))
                settled=[s for s in samples if 3<=s['monotonic']-began<=15]
                if not settled:
                    results.append(check(name+'-BIND','INCONCLUSIVE','no settled multiport observations'))
                else:
                    expected=self.c['pse_stress'].get('expected_path')
                    if expected and mask&(1<<13):
                        expected='replicate'
                    valid=sum(binding(self.c,s['detail'],self.ingress)==[mask] and
                              any(f'->{ch["group"]}:{ch["port"]} ' in line and
                                  f' local={int(local)} ' in line and
                                  (not expected or ' path='+expected+' ' in line)
                                  for line in s['detail'].splitlines())
                              for s in settled)
                    results.append(check(name+'-BIND','PASS' if valid/len(settled)>=.8 else 'FAIL',
                                         'exact combined output bitmap and local-copy request',mask=mask))
                self.wait_jobs([observer],20,samples)
                results.append(assess_pse(self.rows(observer),50))
                results.append(self.natural_leave(name))
                if before:
                    from hg_iptv_common import assess_ports
                    results.append(assess_ports(self.c,before,self.physical_ports(name+'-after')))
            self.report['multiport_qualification']='TESTED'
            self.report['multiport_output_mask']=mask
            self.report['multiport_speeds']={}
            for port in [self.c['hg']['port']]+[self.c[r]['hg_port'] for r in self.peers]:
                raw=self.remote('hg','ethtool '+shlex.quote(port))
                (self.out/('multiport-link-'+port+'.txt')).write_text(raw)
                speed=re.search(r'Speed:\s*(\d+)Mb/s',raw)
                self.report['multiport_speeds'][port]=int(speed[1]) if speed else None
            self.report['mixed_native_covered']=bool(mask&(1<<13))
            speeds=list(self.report['multiport_speeds'].values())
            self.report['unequal_speed_covered']=None not in speeds and len(set(speeds))>1
        finally:
            self.c['channel']=original
        return results

    def run(self):
        result=dict(name='pse-stress',checks=[],samples=[],clients={},status='RUNNING')
        self.report['phases']=[result];self.report['checks']=[]
        pse_jobs=[];health_jobs=[];cpu_jobs=[];http_jobs=[];fq_rows=[];health_rows=[]
        try:
            self.preflight();self.deploy()
            self.fault_directory='/root/hiptv_pse_'+self.token.replace('-','_')
            self.remote('hg','umask 077; mkdir -m 700 '+shlex.quote(self.fault_directory))
            self.report['fault_evidence_directory']=self.fault_directory
            self.transaction_snapshot('before')
            began=time.monotonic();next_action=0;next_probe=0;next_checkpoint=0;next_round=0
            actions=self.stress['actions'];seconds=self.stress['seconds']
            while True:
                elapsed=time.monotonic()-began
                if elapsed>=seconds: break
                if elapsed>=next_probe:
                    duration=min(280,math.ceil(seconds-elapsed)+2)
                    pse_jobs.append(self.launch('hg','pse-%04d'%int(elapsed),['pse',str(duration),self.fault_directory]))
                    address=urlsplit(self.c['hg']['url']).hostname
                    health_jobs.append(self.launch('fnos','health-%04d'%int(elapsed),['health',address,str(duration)]))
                    next_probe=elapsed+270
                if next_round<3 and elapsed>=next_round*200:
                    cpu_jobs.append(self.sampler('pse-cpu-%d'%next_round,'hg',195));next_round+=1
                for job in pse_jobs:
                    fq_rows+=self.rows(job)
                    if job['proc'].poll() not in (None,0): raise RuntimeError('PSE observer failed or detected exhaustion')
                for job in health_jobs:
                    values=self.rows(job)
                    for row in values: row['coordinator_seconds']=job['started']-began+row['elapsed']
                    health_rows+=values
                    if any(not row['ok'] for row in values) or job['proc'].poll() not in (None,0):
                        raise RuntimeError('fnOS cannot ping HG')
                if not fq_rows:
                    if elapsed>5:
                        raise RuntimeError('PSE observer did not produce a ready sample')
                    time.sleep(.05)
                    continue
                if any(row['fq']<1024 or row['isr']&256 for row in fq_rows[-40:]):
                    raise RuntimeError('PSE FQ threshold/alarm reached')
                while next_action<len(actions) and elapsed>=actions[next_action]['at']:
                    action=actions[next_action];entry=self.catalog[action['channel']]
                    job=self.launch('receiver','switch-%04d'%next_action,[
                        '-url',entry['urls'][action['role']],'-source',self.c['receiver']['address'],
                        '-seconds','180','-warmup','0','-wall-seconds',str(action['wall_seconds'])])
                    job['action']=action;http_jobs.append(job);next_action+=1
                    if elapsed-action['at']>.5:
                        result['checks'].append(check('schedule-'+job['label'],'INCONCLUSIVE','launch missed captured timeline',late_seconds=elapsed-action['at']))
                for job in http_jobs:
                    if job.get('collected') or job['proc'].poll() is None: continue
                    job['out'].flush();data=json.loads(job['path'].read_text());job['collected']=True
                    result['clients'][job['label']]=data
                    expected=max(1,job['action']['wall_seconds']-data.get('ttfb_seconds',0))
                    verdict=assess_stream(self.c,data,expected);result['checks'].append(dict(verdict,name=job['label']))
                    if verdict['status'] in ('FAIL','ERROR'): raise RuntimeError('media integrity failed: '+job['label'])
                if elapsed>=next_checkpoint:
                    result['samples'].append(self.checkpoint('checkpoint-%04d'%int(elapsed)))
                    next_checkpoint=elapsed+10
                time.sleep(.05)
            self.wait_jobs(self.jobs,35,result['samples'])
            for job in pse_jobs: fq_rows+=self.rows(job)
            for job in health_jobs:
                values=self.rows(job)
                for row in values: row['coordinator_seconds']=job['started']-began+row['elapsed']
                health_rows+=values
            for job in http_jobs:
                if job.get('collected'): continue
                data=json.loads(job['path'].read_text());result['clients'][job['label']]=data
                expected=max(1,job['action']['wall_seconds']-data.get('ttfb_seconds',0))
                result['checks'].append(dict(assess_stream(self.c,data,expected),name=job['label']))
            result['checks'].append(assess_pse(fq_rows,seconds))
            result['checks'].append(assess_switching_hardware(
                self.c,self.catalog,actions,result['samples'],began,self.ingress))
            result['checks'].append(check('management-ping','PASS' if health_rows and all(r['ok'] for r in health_rows) else 'FAIL','independent fnOS management ping'))
            result['samples'].append(self.checkpoint('after-playback'))
            original=self.c['channel']
            try:
                for index,entry in enumerate(self.catalog):
                    self.c['channel']=entry;result['checks'].append(self.natural_leave('stress-channel-%d'%index))
            finally: self.c['channel']=original
            final=self.launch('hg','pse-after-leave',['pse','2',self.fault_directory])
            self.wait_jobs([final],10,result['samples']);last=self.rows(final)
            result['checks'].append(assess_pse(last,2))
            recovery=bool(last and fq_rows and abs(statistics.median(r['fq'] for r in last)-statistics.median(r['fq'] for r in fq_rows[:10]))<=256)
            result['checks'].append(check('FQ-recovered','PASS' if recovery else 'INCONCLUSIVE','actual FQ after natural leave versus initial window'))
            result['checks']+=self.multiport()
            self.transaction_snapshot('after')
            rounds=[]
            for index,job in enumerate(cpu_jobs):
                cpu=cpu_summary(job['path'].read_text(),10,180)
                pings=[r['rtt_ms'] for r in health_rows if index*200+10<=r['coordinator_seconds']<=index*200+190]
                clients=[result['clients'][j['label']] for j in http_jobs if index*200<=j['action']['at']<(index+1)*200]
                if len(pings)<100 or not clients: continue
                total_seconds=sum(v['seconds'] for v in clients)
                rounds.append(dict(cpu,mbps=sum(v['bytes'] for v in clients)*8/total_seconds/1e6,
                                   ping_p99_ms=sorted(pings)[min(len(pings)-1,math.ceil(.99*len(pings))-1)]))
            self.report['performance']=dict(rounds=rounds,
                configuration=hashlib.sha256('\n'.join(self.original['state'].splitlines()[1:]).encode()).hexdigest(),
                catalog=hashlib.sha256(json.dumps(self.catalog,sort_keys=True).encode()).hexdigest(),
                fixture=self.stress['fixture_sha256'],background_controlled=self.c['pse_stress'].get('background_controlled') is True)
            (self.out/'performance.json').write_text(json.dumps(self.report['performance'],indent=2)+'\n')
            result['status']=worst([r['status'] for r in result['checks']]);self.report['status']=result['status']
        except (Exception,KeyboardInterrupt) as error:
            self.report.update(status='ERROR',error=type(error).__name__+': '+str(error))
            result['status']='ERROR';result['checks'].append(check('execution','ERROR',self.report['error']))
        finally:
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,signal.SIG_IGN)
            self.cleanup();self.report['status']=worst([self.report['status'],self.report['cleanup']['status']])
            self.report['finished']=dt.datetime.now(dt.timezone.utc).isoformat();self.save()
        print(self.report['status']+' '+str(self.out),flush=True)
        return EXIT[self.report['status']]


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Compare controlled fixed-load performance JSON reports')
    parser.add_argument('--compare',nargs=2,type=Path,required=True,metavar=('BASELINE','CANDIDATE'))
    args=parser.parse_args()
    verdict=compare_performance(*(json.loads(p.read_text()) for p in args.compare))
    print(json.dumps(verdict,indent=2));raise SystemExit(EXIT[verdict['status']])
