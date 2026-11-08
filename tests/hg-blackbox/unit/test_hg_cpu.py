"""CPU regression decisions and plan-only safety; no router access."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'helpers'))
import hg_cpu_common as m
spec = importlib.util.spec_from_file_location('cpu_runner',HERE/'hg-cpu-regression.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CPU(unittest.TestCase):
    def test_retry_only_transient_controller_lock(self):
        busy=RuntimeError('airoha-cpu: Another CPU policy operation is active')
        pause=mock.Mock()
        op=mock.Mock(side_effect=[busy,busy,{'ok':True}])
        self.assertEqual(m.retry_busy(op,pause),{'ok':True})
        self.assertEqual(pause.call_count,2)
        op=mock.Mock(side_effect=RuntimeError('wrong token'))
        with self.assertRaises(RuntimeError):m.retry_busy(op,pause)
        self.assertEqual(op.call_count,1)
        op=mock.Mock(side_effect=busy)
        with self.assertRaises(RuntimeError):m.retry_busy(op,pause)
        self.assertEqual(op.call_count,15)

    def read_config(self, traffic):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json'
            p.write_text(json.dumps(dict(hg=dict(ssh='hg'),client=dict(ssh='fnos'),traffic=traffic)))
            return m.config(p)

    def test_ipv6_route_family_and_gateway(self):
        c=self.read_config(dict(server='2001:db8::10',source='2001:db8:1::3',gateway='fe80::1'))
        self.assertEqual(m.family(c['traffic']),'-6')
        self.assertEqual(c['traffic']['gateway'],'fe80::1')
        self.assertEqual(m.family(m.config(HERE/'configs/cpu.example.json')['traffic']),'-4')

    def test_traffic_address_family_and_scope_validation(self):
        for traffic in [dict(server='2001:db8::10',source='192.0.2.3',gateway='fe80::1'),
                        dict(server='2001:db8::10',source='2001:db8:1::3',gateway='fe80::1%eth0'),
                        dict(server='ff02::1',source='2001:db8:1::3',gateway='fe80::1')]:
            with self.subTest(traffic=traffic), self.assertRaises(ValueError): self.read_config(traffic)

    def test_plan_stages_both_directions_and_bracket_baselines(self):
        p=m.plan(m.config(HERE/'configs/cpu.example.json'))
        self.assertEqual(len(p),30)
        self.assertEqual(p[0]['stage'],'baseline')
        self.assertEqual(p[2]['stage'],'network')
        self.assertEqual(p[12]['stage'],'full')
        self.assertEqual(p[-1]['stage'],'baseline-after')

    def test_dry_run_never_ssh(self):
        with mock.patch.object(runner.subprocess,'run',side_effect=AssertionError('no execution')), \
             mock.patch.object(runner.subprocess,'Popen',side_effect=AssertionError('no execution')), \
             mock.patch('builtins.print'):
            self.assertEqual(runner.main(['--config',str(HERE/'configs/cpu.example.json')]),0)

    def test_affinity_uses_effective_irq_and_missing_napi(self):
        s=dict(state=dict(active=True,trial=True,ops=[dict(id='task:3',kind='task',role='napi')]),
               actual=[dict(id='task:3',want=2,value=2,effective=None)])
        self.assertEqual(m.affinity(s)['status'],'PASS')
        s['actual'][0]['value']=None
        self.assertEqual(m.affinity(s)['status'],'FAIL')
        s['actual'][0].update(value=2,effective=1)
        self.assertEqual(m.affinity(s)['status'],'FAIL')

    def rows(self):
        return [dict(round=1,stage=stage,direction='up',mbps=1000,latency=dict(p99_ms=5,loss_pct=0))
                for stage in ('baseline','full','baseline-after')]

    def test_throughput_latency_and_drift(self):
        rows=self.rows();self.assertEqual(m.compare(rows)[0]['status'],'PASS')
        rows[1]['mbps']=949;self.assertEqual(m.compare(rows)[0]['status'],'FAIL')
        rows=self.rows();rows[1]['latency']['p99_ms']=5.51
        self.assertEqual(m.compare(rows)[0]['status'],'FAIL')
        rows=self.rows();rows[-1]['mbps']=800
        self.assertEqual(m.compare(rows)[0]['status'],'INCONCLUSIVE')
        self.assertEqual(m.compare([])[0]['status'],'INCONCLUSIVE')

    def test_ping_complete_sample(self):
        text='\n'.join('64 bytes time=2.5 ms' for _ in range(100))+'\n0% packet loss'
        self.assertEqual(m.latency(text)['p99_ms'],2.5)
        with self.assertRaises(ValueError):m.latency('')


if __name__ == '__main__':
    unittest.main()
