"""Offline PSE replay/decision regressions; no router or NAS access."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'helpers'))
import hg_iptv_pse as p
import hg_iptv_common as common
spec=importlib.util.spec_from_file_location('iptv_pse_runner',HERE/'hg-iptv-regression.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class PseDecisions(unittest.TestCase):
    def setUp(self):
        self.c=common.config(HERE/'configs/iptv.example.json')

    def catalog(self):
        return [dict(self.c['channel'],group=f'239.1.1.{i+1}',
                     urls={h:f'http://192.0.2.{1 if h=="hg" else 3}:5140/rtp/239.1.1.{i+1}:2000'
                           for h in ('hg','fnos')}) for i in range(10)]

    def test_captured_timing_and_bounded_overlap(self):
        plan=p.stress_plan(self.c)
        self.assertEqual(len(plan['actions']),207)
        self.assertEqual(plan['actions'][0]['at'],0)
        self.assertEqual(plan['actions'][1]['at'],5.536)
        self.assertEqual(plan['actions'][23]['at'],200)
        self.assertAlmostEqual(plan['actions'][0]['wall_seconds'],38.569)
        self.assertTrue(all(1<=a['wall_seconds']<=180 for a in plan['actions']))
        self.c['pse_stress']=dict(seconds=610,channels=self.catalog())
        self.assertEqual(len(p.stress_plan(self.c,True)['actions']),69)

    def test_catalog_requires_complete_direct_url_mapping(self):
        with self.assertRaises(ValueError):p.stress_plan(self.c,True)
        self.c['pse_stress']=dict(channels=self.catalog())
        self.assertTrue(p.stress_plan(self.c,True)['channels_ready'])
        for change in ('short','redirect','source','port'):
            c=copy.deepcopy(self.c)
            if change=='short':c['pse_stress']['channels'].pop()
            if change=='redirect':c['pse_stress']['channels'][0]['urls']['hg']+='?fcc=1'
            if change=='source':c['pse_stress']['channels'][0]['source']='239.1.1.1'
            if change=='port':c['pse_stress']['channels'][0]['port']=0
            with self.assertRaises(ValueError):p.stress_plan(c,True)

    def test_plan_mode_never_contacts_a_device(self):
        with mock.patch('subprocess.run',side_effect=AssertionError('no SSH')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(['--config',str(HERE/'configs/iptv.example.json'),'--profile','pse-stress']),0)
        data=json.loads(out.getvalue())
        self.assertEqual(data['runtime_changes'],[])
        self.assertEqual(len(data['phases']['actions']),207)

    def test_multiport_requires_distinct_real_outputs(self):
        peer=dict(ssh='peer-nas',arch='x86_64',address='192.0.2.4',
                  interface='eth1',hg_port='lan3',ppe_bit=3)
        self.c['pse_stress']=dict(receivers=[peer])
        self.assertEqual(p.stress_plan(self.c)['multiport_receivers'],1)
        for update in (dict(hg_port='lan4'),dict(ppe_bit=4),dict(address='192.0.2.3'),
                       dict(ssh='-oProxyCommand=bad'),dict(ppe_bit=6),dict(interface='eth0;false')):
            c=copy.deepcopy(self.c);c['pse_stress']['receivers'][0].update(update)
            with self.assertRaises(ValueError):p.stress_plan(c)
        self.c['path_checks']=dict(nonmember_ports=['lan3'])
        with self.assertRaises(ValueError):p.stress_plan(self.c)

    def test_extra_peer_known_hosts_resolves_from_configuration(self):
        raw=json.loads((HERE/'configs/iptv.example.json').read_text())
        raw['pse_stress']=dict(receivers=[dict(known_hosts='peer-hosts')])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';path.write_text(json.dumps(raw))
            c=common.config(path)
            self.assertEqual(c['pse_stress']['receivers'][0]['known_hosts'],str(Path(tmp)/'peer-hosts'))

    def test_profiles_cannot_change_runtime_controls(self):
        obj=runner.PseStressRunner.__new__(runner.PseStressRunner)
        for name,args in (('mode',[True]),('guard',['setup']),('quiesce',[])):
            with self.assertRaises(RuntimeError):getattr(obj,name)(*args)

    def test_actual_fq_alarm_and_sampling_coverage(self):
        rows=[dict(monotonic=i*.05,fq=16366,isr=0) for i in range(201)]
        self.assertEqual(p.assess_pse(rows,10)['status'],'PASS')
        rows[30]['fq']=2
        self.assertEqual(p.assess_pse(rows,10)['status'],'FAIL')
        rows[30]['fq']=16366;rows[30]['isr']=256
        self.assertEqual(p.assess_pse(rows,10)['status'],'FAIL')
        rows[30]['isr']=0
        self.assertEqual(p.assess_pse(rows[::10],10)['status'],'INCONCLUSIVE')
        self.assertEqual(p.assess_pse(rows[:5],10)['status'],'INCONCLUSIVE')
        self.assertEqual(p.assess_pse([],10)['status'],'ERROR')

    def test_irq_rows_use_error_counts_not_last_timestamp(self):
        raw='# airoha-qdma-irq-errors 1\n0 1 122 4 7 00000100 00000100 999999\n'
        self.assertEqual(p.irq_counts(raw),{(0,1,4):7})
        for bad in ('', 'old counters\n',raw+'1 bad\n'):
            with self.assertRaises(ValueError):p.irq_counts(bad)

    def test_comparison_requires_control_and_enforces_all_limits(self):
        baseline=dict(configuration='x',catalog='y',fixture='z',background_controlled=True,
                      rounds=[dict(mbps=40,cpu_total_pct=4,max_core_pct=12,ping_p99_ms=.5)]*3)
        self.assertEqual(p.compare_performance(baseline,baseline)['status'],'PASS')
        for key,value in (('mbps',37),('cpu_total_pct',5.1),('max_core_pct',15.1),('ping_p99_ms',2.6)):
            candidate=copy.deepcopy(baseline)
            for row in candidate['rounds']:row[key]=value
            self.assertEqual(p.compare_performance(baseline,candidate)['status'],'FAIL',key)
        candidate=dict(baseline,configuration='different')
        self.assertEqual(p.compare_performance(baseline,candidate)['status'],'INCONCLUSIVE')
        candidate=dict(baseline,background_controlled=False)
        self.assertEqual(p.compare_performance(baseline,candidate)['status'],'INCONCLUSIVE')

    def test_switching_needs_real_exact_bind_and_expected_path(self):
        catalog=self.catalog();actions=[dict(at=0,role='fnos',channel=0,wall_seconds=180)]
        line='0001 BIND iif=14 198.51.100.10:8046->239.1.1.1:2000 outputs=0010 seen=0010 path=switch fport=1\n'
        samples=[dict(monotonic=10+i,detail=line) for i in range(12)]
        self.c['pse_stress']=dict(expected_path='switch')
        self.assertEqual(p.assess_switching_hardware(self.c,catalog,actions,samples,0,14)['status'],'PASS')
        samples[0]['detail']=line.replace('path=switch','path=replicate')
        self.assertEqual(p.assess_switching_hardware(self.c,catalog,actions,samples,0,14)['status'],'FAIL')
        for row in samples:row['detail']='no BIND'
        self.assertEqual(p.assess_switching_hardware(self.c,catalog,actions,samples,0,14)['status'],'FAIL')

    def test_remote_recording_preserves_newlines_and_exit_status(self):
        obj=runner.PseStressRunner.__new__(runner.PseStressRunner)
        obj.token='test-token';obj.jobs=[];obj.dirs={'hg':'/tmp/hiptv_aabbcc'}
        obj.ssh=lambda role:['ssh','unused']
        with tempfile.TemporaryDirectory() as tmp:
            obj.out=Path(tmp)
            with mock.patch.object(subprocess,'Popen') as popen:
                job=obj.launch('hg','unit',['pse','2'])
            command=popen.call_args.args[0][-1]
            self.assertIn('\n',command)
            self.assertIn("printf '%s'",command)
            self.assertIn(' | tee ',command)
            self.assertNotIn('%s\\\\n',command)
            for f in ('out','err'):job[f].close()

    def test_split_json_lines_are_buffered_once(self):
        obj=runner.PseStressRunner.__new__(runner.PseStressRunner)
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'rows';f.write_text('{"fq":1}\n{"fq":')
            job=dict(path=f,offset=0,pending='')
            self.assertEqual(obj.rows(job),[dict(fq=1)])
            with f.open('a') as out:out.write('2}\n')
            self.assertEqual(obj.rows(job),[dict(fq=2)])
            self.assertEqual(obj.rows(job),[])


class NativePse(unittest.TestCase):
    def test_actual_sampler_only_maps_read_and_stops_on_fault(self):
        source=(HERE/'helpers/hg_iptv_pse.c').read_text()
        start=source.index('int pse_main(');end=source.index('\nint health_main(',start)
        function=source[start:end]
        harness=r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <time.h>
static uint32_t registers[1024];
static int tick,mode,faults;
static int target_board(void) { return 1; }
static int fake_clock(int clock,struct timespec *t) {
 (void)clock;t->tv_sec=tick/20;t->tv_nsec=(tick%20)*50000000;return 0;
}
static int fake_sleep(const struct timespec *a,struct timespec *b) {
 (void)a;(void)b;tick++;
 if(tick==2&&mode==1)registers[0x108/4]=2;
 if(tick==2&&mode==2)registers[8/4]=256;
 return 0;
}
static void *fake_map(void *a,size_t n,int prot,int flags,int fd,long offset) {
 (void)a;(void)n;(void)flags;(void)fd;assert(prot==PROT_READ&&offset==0x1fb50000);return registers;
}
static int fault_snapshot(const char *d,unsigned fq,unsigned isr) {
 assert(!d);assert(fq<1024||isr&256);faults++;return 0;
}
#define open(...) 3
#define close(fd) ((void)(fd))
#define mmap fake_map
#define munmap(a,b) ((void)(a),(void)(b))
#define clock_gettime fake_clock
#define nanosleep fake_sleep
'''
        main=r'''
int main(void) {
 char *args[]={"pse","2"};
 for(mode=0;mode<3;mode++) {
  tick=0;faults=0;registers[0x108/4]=16366;registers[8/4]=0;
  assert(pse_main(2,args)==(mode!=0));
  assert(faults==(mode!=0));
 }
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            c=Path(tmp)/'test.c';exe=Path(tmp)/'test'
            c.write_text(harness+function+main)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(c),'-o',str(exe)],check=True,capture_output=True)
            result=subprocess.run([str(exe)],check=True,capture_output=True,text=True)
            rows=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(any(row['fq']==2 for row in rows))
            self.assertTrue(any(row['isr']==256 for row in rows))


if __name__=='__main__':unittest.main()
