#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / 'helpers'))
import hg_iptv_reliability as r
from hg_iptv_common import config
spec = importlib.util.spec_from_file_location('iptv_runner_reliable', HERE / 'hg-iptv-regression.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.c = config(HERE / 'configs/iptv.example.json')

    def test_default_matrix_and_reverse_order(self):
        phases = r.reliability_plan(self.c)
        self.assertEqual(len(phases), 30)
        matrix = phases[:18]
        self.assertEqual([(p['placement'], p['load']) for p in matrix[:6]],
                         [(p['placement'], p['load']) for p in matrix[6:12]][::-1])
        self.assertTrue(all(p['offload'] == 'unchanged' for p in matrix))
        self.assertEqual([p['first_leave'] for p in phases[-2:]], ['hg', 'fnos'])

    def test_plan_only_never_ssh(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('no SSH')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(['--config', str(HERE/'configs/iptv.example.json'), '--profile', 'reliability']), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data['runtime_changes'], [])
        self.assertEqual(len(data['phases']), 30)

    def test_legacy_plan_unchanged(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            runner.main(['--config', str(HERE/'configs/iptv.example.json')])
        self.assertEqual(len(json.loads(out.getvalue())['phases']), 10)

    def test_mutating_entrypoints_blocked(self):
        obj = runner.ReliabilityRunner.__new__(runner.ReliabilityRunner)
        obj.remote = mock.Mock(side_effect=AssertionError('must not touch device'))
        for name, args in (('mode', [True]), ('quiesce', []), ('guard', ['setup'])):
            with self.assertRaises(RuntimeError):
                getattr(obj, name)(*args)
        obj.remote.assert_not_called()

    def test_join_timeout_and_threshold(self):
        for delay, status in ((None, 'FAIL'), (.001, 'PASS'), (1, 'PASS'), (1.01, 'FAIL'), (-1, 'ERROR')):
            self.assertEqual(r.assess_join(dict(first_packet_delay_seconds=delay), self.c['reliability'])['status'], status)

    def test_background_requires_exact_fresh_evidence(self):
        channel = dict(group='239.1.1.1', source='198.51.100.10', port=2000, source_port=8000, ingress_vlan=4000)
        proof = dict(channel, boot_id='boot', packets=1000, seconds=5, observed_at=90, unsubscribed=True, filter_active=True)
        self.assertTrue(r.background_proof(channel, proof, 'boot\n', 100))
        for change in (dict(ingress_vlan=4001), dict(boot_id='reboot'), dict(observed_at=-100), dict(packets=0), dict(unsubscribed=False)):
            self.assertFalse(r.background_proof(channel, dict(proof, **change), 'boot', 100))
        self.assertFalse(r.background_proof(channel, {}, 'boot', 100))

    def test_guard_only_owns_temporary_executable(self):
        guard = (HERE/'helpers/hg_iptv_observe.sh').read_text()
        self.assertIn('readlink "$p"', guard)
        self.assertIn('= "$dir/probe"', guard)
        self.assertIn('timeout -k 2', guard)
        for forbidden in ('devmem', 'multicast_enable', 'ethtool -K', 'ip link set', 'uci ', 'pkill'):
            self.assertNotIn(forbidden, guard)

    def test_ifc_unknown_is_not_success(self):
        self.assertIsNone(r.parse_ifc('old firmware')['active'] if 'active' in r.parse_ifc('old firmware') else None)
        self.assertEqual(r.parse_ifc('automatic=1 active=0 last_error=-28\nhardware_drops_error=-5')['active'], '0')

    def test_probe_overflow_is_separate_from_wire_loss(self):
        d = dict(packets=10000, seconds=15, max_gap_seconds=.01, rtp_packets=10000,
                 non_rtp=0, invalid_metadata=0, socket_drops=5, ttl={'124':10000}, tos={'0':10000},
                 sequence_missing=5, sequence_duplicates=0, sequence_reordered=0,
                 sequence_overflow=0, ssrc_changes=0, rcvbuf_bytes=4194304, socket_inode=123)
        checks = r.assess_probe(d, self.c, 'hg')
        self.assertEqual([c['status'] for c in checks], ['INCONCLUSIVE', 'FAIL'])
        d.update(socket_drops=0, warmup_socket_drops=5, socket_drops_total=5, sequence_missing=0)
        self.assertEqual([c['status'] for c in r.assess_probe(d, self.c, 'hg')], ['PASS', 'PASS'])


class ObserverSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iptv-session-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root/'hiptv_aabbcc'; self.directory.mkdir()
        self.script = self.directory/'observe.sh'
        text = (HERE/'helpers/hg_iptv_observe.sh').read_text()
        text = text.replace('/tmp/hiptv_', str(self.root)+'/hiptv_')
        text = text.replace('/tmp/hg-qos-regression.lock', str(self.root/'lock'))
        # Fixture clock expiry and instantaneous grace; production uses 310s.
        text = text.replace('sleep 310', 'true # fixture grace')
        self.script.write_text(text)
        (self.directory/'owner').write_text('unit-token\n')
        (self.root/'lock').mkdir(); (self.root/'lock/owner').write_text('unit-token\n')
        subprocess.run(['cc','-x','c','-','-o',str(self.directory/'probe')],
                       input='#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n',text=True,check=True)

    def call(self, action, *args):
        return subprocess.run(['/bin/sh',str(self.script),str(self.directory),'unit-token',action,*args],
                              capture_output=True, timeout=5)

    def test_expired_lease_stops_only_owned_process(self):
        own = subprocess.Popen([str(self.directory/'probe'), '30'])
        other = subprocess.Popen(['/bin/sleep', '30'])
        try:
            self.assertIsNone(own.poll())
            (self.directory/'coordinator').touch()
            (self.directory/'lease').write_text('0\n')
            self.assertEqual(self.call('watchdog').returncode, 0)
            own.wait(timeout=2)
            self.assertIsNone(other.poll())
            self.assertFalse((self.root/'lock').exists())
            self.assertNotEqual(self.call('pulse').returncode, 0)
            self.assertEqual(self.call('check').returncode, 0)
        finally:
            for p in (own, other):
                if p.poll() is None: p.terminate()
                p.wait(timeout=2)

    def test_wrong_owner_cannot_stop_or_release(self):
        (self.directory/'owner').write_text('someone-else\n')
        self.assertNotEqual(self.call('stop').returncode, 0)
        self.assertTrue((self.root/'lock').exists())

    def test_probe_has_hard_deadline(self):
        result = self.call('run', '1', '30')
        self.assertEqual(result.returncode, 124)


class NativeJoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='iptv-native-unit-')
        cls.binary = Path(cls.tmp.name)/'probe'
        sources = [str(HERE/'helpers'/('hg_iptv_'+s+'.c')) for s in ('native','rtp','sample','diag','pse')]
        subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror','-o',str(cls.binary),*sources],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_join_clock_and_full_receive_window(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reserve:
            sender.bind(('127.0.0.1',0)); sport=sender.getsockname()[1]
            reserve.bind(('127.0.0.1',0)); port=reserve.getsockname()[1]
            reserve.close()
            sender.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton('127.0.0.1'))
            before=time.monotonic()
            p=subprocess.Popen([str(self.binary),'rtp','lo','239.255.5.145',str(port),'127.0.0.1',str(sport),'1','0','2'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertIn('RTP_READY', p.stderr.readline())
                time.sleep(.25)
                end=time.monotonic()+1.2; seq=0
                while time.monotonic()<end:
                    sender.sendto(struct.pack('!BBHII',0x80,33,seq,0,123)+b'x'*32,('239.255.5.145',port))
                    seq+=1;time.sleep(.005)
                out,err=p.communicate(timeout=3)
                self.assertEqual(p.returncode,0,err)
                data=json.loads(out)
                self.assertGreaterEqual(data['first_packet_delay_seconds'],.2)
                self.assertGreaterEqual(data['seconds'],.99)
                self.assertLess(data['max_gap_seconds'],.2)  # join wait is separate
                self.assertGreater(data['rcvbuf_bytes'],0)
                self.assertGreater(data['socket_inode'],0)
                self.assertGreaterEqual(data['join_monotonic_seconds'],before)
                self.assertEqual(data['socket_drops_total'],0)
            finally:
                if p.poll() is None:p.kill();p.communicate()

    def test_no_first_packet_is_bounded(self):
        p=subprocess.run([str(self.binary),'rtp','lo','239.255.5.146','59997','127.0.0.1','59998','15','0','1'],
                         capture_output=True,text=True,timeout=3)
        self.assertEqual(p.returncode,1)
        self.assertIsNone(json.loads(p.stdout)['first_packet_delay_seconds'])


if __name__ == '__main__':
    unittest.main()
