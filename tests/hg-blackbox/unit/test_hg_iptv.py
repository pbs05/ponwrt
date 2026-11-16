"""Offline only: no SSH, IPTV traffic, router or NAS changes."""
import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "helpers"))
import hg_iptv_common as m
spec = importlib.util.spec_from_file_location("iptv_runner", HERE / "hg-iptv-regression.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Decisions(unittest.TestCase):
    def setUp(self):
        self.c = m.config(HERE / "configs/iptv.example.json")

    def test_kernel_vif_pair_accepts_either_index_order(self):
        header = 'Interface BytesIn PktsIn BytesOut PktsOut Flags Local Remote\n'
        for upstream, downstream in ((0, 1), (1, 0)):
            raw = (header + f'{upstream} test.4001 100 1 0 0 00000 010200C0 00000000\n'
                   f'{downstream} test.20 0 0 100 1 00000 020200C0 00000000\n')
            self.assertEqual(m.require_mroute_vifs(raw, 'test.4001', 'test.20'),
                             {'test.4001': upstream, 'test.20': downstream})

    def test_missing_kernel_vif_fails_even_with_upstream_installed(self):
        header = 'Interface BytesIn PktsIn BytesOut PktsOut Flags Local Remote\n'
        upstream = '0 test.4001 100 1 0 0 00000 010200C0 00000000\n'
        downstream = '1 test.20 0 0 100 1 00000 020200C0 00000000\n'
        for rows, missing in ((upstream, 'test.20'), (downstream, 'test.4001'), ('', 'test.4001')):
            with self.assertRaisesRegex(RuntimeError, missing):
                m.require_mroute_vifs(header + rows, 'test.4001', 'test.20')

    def test_kernel_vif_parser_rejects_invalid_or_duplicate_rows(self):
        header = 'Interface BytesIn PktsIn BytesOut PktsOut Flags Local Remote\n'
        row = '0 test.4001 100 1 0 0 00000 010200C0 00000000\n'
        for raw in ('', 'not proc output\n', header + '0 test.4001\n', header + row + row,
                    header + row + row.replace('test.4001', 'test.20'),
                    header + row + row.replace('0 test.', '1 test.')):
            with self.assertRaises(ValueError):
                m.require_mroute_vifs(raw, 'test.4001', 'test.20')

    def test_preflight_missing_vif_stops_before_lock_or_traffic(self):
        obj = runner.Runner.__new__(runner.Runner)
        obj.c = copy.deepcopy(self.c)
        policy = dict(enabled=1, frozen=1, ingress=14, egress=13, broken=0,
                      ppe_stopped=0, scope_lost=0, errors=0, bad_copies=0)
        raw = ('Interface BytesIn PktsIn BytesOut PktsOut Flags Local Remote\n'
               '0 hgi4000 100 1 0 0 00000 010200C0 00000000\n')

        def remote(role, command):
            if command == 'uname -m':
                return obj.c[role]['arch']
            if command.startswith('test "$(id -u)"'):
                return ''
            if command == 'airoha-mcastctl get':
                return json.dumps(policy)
            if command == 'cat /sys/class/net/hgi4000/ifindex':
                return '14'
            if command == 'cat /sys/class/net/br-lan.10/ifindex':
                return '13'
            if command == 'cat /proc/net/ip_mr_vif':
                return raw
            raise AssertionError('must stop before later commands: ' + command)

        obj.remote = mock.Mock(side_effect=remote)
        with tempfile.TemporaryDirectory() as d:
            obj.out = Path(d)
            with mock.patch.object(runner, 'manifest', return_value={}), \
                    mock.patch.object(runner, 'elf_arch', side_effect=['aarch64', 'x86_64', 'x86_64']):
                with self.assertRaisesRegex(RuntimeError, 'br-lan.10'):
                    obj.preflight()
            self.assertEqual((obj.out / 'preflight-mroute-vifs.txt').read_text(), raw)
            self.assertFalse(getattr(obj, 'locked', False))

    def test_native_sampler_enumerates_host_cpus(self):
        cc=shutil.which('cc')
        if not cc:self.skipTest('native C compiler unavailable')
        harness=r'''
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <string.h>
#include <assert.h>
static const char *fixture;
static FILE *fake_fopen(const char *path, const char *mode) {
 (void)mode; assert(!strcmp(path,"/proc/stat"));
 FILE *f=tmpfile(); assert(f); fputs(fixture,f); rewind(f); return f;
}
#define fopen fake_fopen
#include SAMPLE
int main(void) {
 fixture="cpu 91234 0 0 0 0 0 0 0\ncpu0 1 0 0 0 0 0 0 0\ncpu1 1 0 0 0 0 0 0 0\ncpu2 1 0 0 0 0 0 0 0\ncpu3 1 0 0 0 0 0 0 0\nintr 1\n";
 assert(host_cpus()==4);
 fixture="cpu 91234 0 0 0 0 0 0 0\ncpu0 1 0 0 0 0 0 0 0\ncpu2 1 0 0 0 0 0 0 0\n";
 assert(host_cpus()==-1);
 fixture="cpu 91234 0 0 0 0 0 0 0\nintr 1\n";
 assert(host_cpus()==0);
 return 0;
}
'''.replace('SAMPLE',json.dumps(str(HERE/'helpers/hg_iptv_sample.c')))
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/'probe.c';binary=Path(d)/'probe';src.write_text(harness)
            subprocess.run([cc,'-std=c11','-Wall','-Wextra','-Werror','-o',str(binary),str(src)],check=True,capture_output=True)
            subprocess.run([str(binary)],check=True,capture_output=True)

    def test_optional_read_only_diagnostics_command(self):
        raw=json.loads((HERE/'configs/iptv.example.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'iptv.json'
            argv=['sudo','-n','-u','rtp2httpd','/opt/iptv-probe','diag']
            raw['fnos']['diagnostics_command']=argv
            p.write_text(json.dumps(raw))
            self.assertEqual(m.config(p)['fnos']['diagnostics_command'],argv)
            for bad in ('sudo /opt/probe diag', [], ['sudo',None], ['probe\ndiag']):
                raw['fnos']['diagnostics_command']=bad
                p.write_text(json.dumps(raw))
                with self.assertRaises(ValueError): m.config(p)

    def test_diagnostics_command_is_explicit_and_shell_quoted(self):
        with tempfile.TemporaryDirectory() as d:
            obj=runner.Runner.__new__(runner.Runner)
            obj.c=copy.deepcopy(self.c)
            obj.c['fnos']['diagnostics_command']=['sudo','-n','-u','rtp2httpd','/opt/probe with space','diag']
            obj.dirs={'hg':'/tmp/hg','fnos':'/tmp/fnos'}
            obj.out=Path(d)
            obj.remote=mock.Mock(side_effect=['{}','{}','0'])
            obj.diagnostics('sample')
            self.assertEqual(obj.remote.call_args_list[0].args,('hg','/tmp/hg/probe diag'))
            self.assertEqual(obj.remote.call_args_list[1].args,('fnos',"sudo -n -u rtp2httpd '/opt/probe with space' diag"))

    def rtp(self, ttl=123):
        return dict(packets=54251, seconds=15, max_gap_seconds=.01, rtp_packets=54251, non_rtp=0, invalid_metadata=0,
                    socket_drops=0, ttl={str(ttl): 54251}, tos={"0": 54251}, sequence_missing=0,
                    sequence_duplicates=0, sequence_reordered=0, sequence_overflow=0, ssrc_changes=0)

    def stream(self):
        return dict(http=200, bytes=214000000, seconds=45, mbps=38, max_read_gap_seconds=.02,
                    ts_packets=1139000, ts_cc_errors=0, ts_duplicates=0, ts_tei=0,
                    ts_invalid_headers=0, ts_resync_bytes=0, read_error="")

    def test_plan_alternates_order_and_includes_local_delivery(self):
        phases = m.plan(self.c)
        self.assertEqual(len(phases), 10)
        first = [(p['placement'], p['offload']) for p in phases[:4]]
        second = [(p['placement'], p['offload']) for p in phases[4:8]]
        self.assertEqual(first, second[::-1])
        self.assertEqual(len(m.plan(self.c, True)), 14)
        self.assertEqual([p['name'] for p in phases[-2:]], ['mixed-sw', 'mixed-hw'])

    def test_paths_only_requires_observed_outputs(self):
        with self.assertRaises(ValueError):
            m.plan(self.c, paths_only=True)
        self.c['path_checks'] = dict(nonmember_ports=['lan2', 'lan3'], max_nonmember_tx_bytes=65536, leave_seconds=45)
        phases = m.plan(self.c, paths_only=True)
        self.assertEqual([p['kind'] for p in phases], ['path', 'path'])
        self.assertEqual([p['offload'] for p in phases], [False, True])
        with self.assertRaises(ValueError):
            m.plan(self.c, gro_ab=True, paths_only=True)

    def test_path_config_rejects_invalid_observer_ports(self):
        raw = json.loads((HERE/'configs/iptv.example.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'config.json'
            raw['path_checks'] = dict(nonmember_ports=['lan2', 'lan3'])
            path.write_text(json.dumps(raw))
            self.assertEqual(m.config(path)['path_checks']['max_nonmember_tx_bytes'], 65536)
            for ports in ([], 'lan2', ['lan2', 'lan2'], ['lan4'], ['br-lan'], ['lan2;false']):
                raw['path_checks']['nonmember_ports'] = ports
                path.write_text(json.dumps(raw))
                with self.assertRaises(ValueError): m.config(path)

    def test_physical_counter_budget_and_disconnected_ports(self):
        self.c['path_checks'] = dict(nonmember_ports=['lan2', 'lan3'], max_nonmember_tx_bytes=65536)
        raw = ('PORT lan2\ncarrier 1\neth-mac-OctetsTransmittedOK: 1000\n'
               'eth-mac-MulticastFramesXmittedOK: 100\n'
               'PORT lan3\ncarrier 1\neth-mac-OctetsTransmittedOK: 5000\n'
               'eth-mac-MulticastFramesXmittedOK: 200\n')
        before = dict(monotonic=0, ports=m.port_counters(raw, ['lan2', 'lan3']))
        after = copy.deepcopy(before); after['monotonic'] = 60
        after['ports']['lan2']['tx_bytes'] += 600
        self.assertEqual(m.assess_ports(self.c, before, after)['status'], 'PASS')
        after['ports']['lan3']['tx_bytes'] += 100000000
        self.assertEqual(m.assess_ports(self.c, before, after)['status'], 'INCONCLUSIVE')
        after = copy.deepcopy(before); after['monotonic'] = 60
        after['ports']['lan2']['carrier'] = 0
        self.assertEqual(m.assess_ports(self.c, before, after)['status'], 'INCONCLUSIVE')
        after['ports']['lan2']['carrier'] = 1; after['ports']['lan2']['tx_bytes'] = 0
        self.assertEqual(m.assess_ports(self.c, before, after)['status'], 'INCONCLUSIVE')
        with self.assertRaises(ValueError): m.port_counters(raw, ['lan1'])
        with self.assertRaises(ValueError):
            m.port_counters('PORT lan2\ncarrier 1\n tx_bytes: 1000\n', ['lan2'])
        after = copy.deepcopy(before); after['monotonic'] = 60
        after['ports']['lan2']['tx_multicast'] = 0
        self.assertEqual(m.assess_ports(self.c, before, after)['status'], 'INCONCLUSIVE')

    def test_leave_distinguishes_unresolved_and_forwarding_routes(self):
        import ipaddress
        group = ipaddress.IPv4Address(self.c['channel']['group']).packed[::-1].hex().upper()
        self.assertFalse(m.ipmr_forwarding(self.c, group + ' 016433C6 -1 0 0 0\n'))
        self.assertFalse(m.ipmr_forwarding(self.c, group + ' 016433C6 0 100 10000 0\n'))
        self.assertTrue(m.ipmr_forwarding(self.c, group + ' 016433C6 0 100 10000 0 1:1\n'))

    def test_leave_checks_natural_state_without_mode_change(self):
        obj = runner.Runner.__new__(runner.Runner)
        obj.c = copy.deepcopy(self.c)
        obj.c['path_checks'] = dict(leave_seconds=5)
        obj.ingress = 14
        obj.pulse = mock.Mock(return_value=dict(detail=''))
        obj.members = mock.Mock(return_value='no memberships')
        obj.remote = mock.Mock(return_value='Group Origin Iif Pkts Bytes Wrong Oifs\n')
        obj.mode = mock.Mock(side_effect=AssertionError('must not force a flush'))
        with tempfile.TemporaryDirectory() as d:
            obj.out = Path(d)
            self.assertEqual(obj.retired('test')['status'], 'PASS')
        obj.mode.assert_not_called()

    def test_rtp_observation_covers_whole_http_job(self):
        probes = [dict(started=0,ready_observed=1),dict(started=.2,ready_observed=1.2)]
        clients = [dict(started=5,finished=60),dict(started=5.1,finished=61)]
        self.assertEqual(m.covered_window(probes,clients,3,90)['status'],'PASS')
        self.assertEqual(m.covered_window(probes,clients,3,15)['status'],'INCONCLUSIVE')
        clients[0]['started']=2
        self.assertEqual(m.covered_window(probes,clients,3,90)['status'],'INCONCLUSIVE')

    def test_socket_capacity_and_drop_attribution(self):
        base=dict(boot_id='boot',snmp='Udp: InErrors RcvbufErrors InCsumErrors\nUdp: 0 0 0\n',
                  cdm2_rx_cpu_drop=0,rtp2httpd_fds=[],udp_sockets=[])
        live=copy.deepcopy(base)
        live['rtp2httpd_fds']=[dict(pid=40,inode=123)]
        live['udp_sockets']=[dict(inode=123,address=self.c['channel']['group'],port=2000,drops=0,rcvbuf_bytes=4194304)]
        self.assertEqual(m.assess_diagnostics(self.c,[base,live],'hg',True)['status'],'PASS')
        live['udp_sockets'][0]['drops']=1
        self.assertEqual(m.assess_diagnostics(self.c,[base,live],'hg',True)['status'],'FAIL')
        self.assertEqual(m.assess_diagnostics(self.c,[live,live],'hg',True)['status'],'PASS')
        live['udp_sockets'][0].update(drops=0,rcvbuf_bytes=1048576)
        self.assertEqual(m.assess_diagnostics(self.c,[base,live],'hg',True)['status'],'FAIL')
        self.assertEqual(m.assess_diagnostics(self.c,[base,base],'hg',True)['status'],'INCONCLUSIVE')
        shared=dict(base,cdm2_rx_cpu_drop=5)
        self.assertEqual(m.assess_diagnostics(self.c,[base,shared],'hg',False)['status'],'INCONCLUSIVE')

    def test_long_qualification_config_and_source_changes(self):
        raw=json.loads((HERE/'configs/iptv.example.json').read_text())
        raw['timing'].update(seconds=180,rounds=3)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'iptv.json';p.write_text(json.dumps(raw))
            self.assertEqual(m.config(p)['timing']['seconds'],180)
        self.assertEqual(m.assess_rtp(self.c,dict(self.rtp(),source='198.51.100.11'),'hg')['status'],'INCONCLUSIVE')

    def test_recorded_ttl_defect_is_fail_not_expected_pass(self):
        data = self.rtp()
        data['ttl'] = {'123': 26358, '124': 27893}
        self.assertEqual(m.assess_rtp(self.c, data, 'fnos')['status'], 'FAIL')
        self.assertEqual(m.assess_rtp(self.c, self.rtp(), 'fnos')['status'], 'PASS')
        self.assertEqual(m.assess_rtp(self.c, self.rtp(124), 'hg')['status'], 'PASS')
        self.assertEqual(m.assess_rtp(self.c, self.rtp(123), 'hg')['status'], 'FAIL')

    def test_reordering_is_not_packet_loss(self):
        data = dict(self.rtp(), sequence_reordered=2, rtp_gap_events=6)
        result = m.assess_rtp(self.c, data, 'fnos')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['reordered'], 2)
        for change in (dict(sequence_missing=1), dict(sequence_duplicates=1), dict(sequence_reordered=100), dict(max_gap_seconds=2)):
            self.assertEqual(m.assess_rtp(self.c, dict(data, **change), 'fnos')['status'], 'FAIL')

    def test_probe_limit_or_overflow_does_not_claim_network_loss(self):
        for key in ('socket_drops', 'sequence_overflow', 'ssrc_changes'):
            self.assertEqual(m.assess_rtp(self.c, dict(self.rtp(), **{key: 1}), 'fnos')['status'], 'INCONCLUSIVE')
        data = dict(self.rtp(), socket_drops=1, ttl={'124': 54251})
        self.assertEqual(m.assess_rtp(self.c, data, 'fnos')['status'], 'FAIL')

    def test_metadata_and_samples_are_mandatory(self):
        for change in (dict(invalid_metadata=1), dict(tos={'32': 54251}), dict(non_rtp=1)):
            self.assertEqual(m.assess_rtp(self.c, dict(self.rtp(), **change), 'fnos')['status'], 'FAIL')
        self.assertEqual(m.assess_rtp(self.c, dict(self.rtp(), ttl={}), 'fnos')['status'], 'INCONCLUSIVE')
        self.assertEqual(m.assess_rtp(self.c, {}, 'fnos')['status'], 'ERROR')

    def test_ts_http_and_warmup_alignment(self):
        self.assertEqual(m.assess_stream(self.c, self.stream(), 45)['status'], 'PASS')
        for key in ('ts_cc_errors', 'ts_duplicates', 'ts_tei', 'ts_invalid_headers'):
            self.assertEqual(m.assess_stream(self.c, dict(self.stream(), **{key: 1}), 45)['status'], 'FAIL')
        self.assertEqual(m.assess_stream(self.c, dict(self.stream(), ts_resync_bytes=187), 45)['status'], 'PASS')
        self.assertEqual(m.assess_stream(self.c, dict(self.stream(), ts_resync_bytes=188), 45)['status'], 'FAIL')
        self.assertEqual(m.assess_stream(self.c, dict(http=404), 45)['status'], 'FAIL')
        self.assertEqual(m.assess_stream(self.c, dict(error='timeout'), 45)['status'], 'FAIL')
        self.assertEqual(m.assess_stream(self.c, dict(self.stream(), seconds=20), 45)['status'], 'INCONCLUSIVE')

    def backend(self, mask='0010', delivered=100, group='239.1.1.1'):
        return dict(policy=dict(enabled=1, frozen=1, paused=0, broken=0, ppe_stopped=0,
                                errors=0, bad_copies=0, delivered=delivered),
                    detail=f'2630 BIND iif=14 198.51.100.10:8046->{group}:2000 outputs={mask} seen={mask} local=1')

    def test_hardware_needs_exact_flow_and_activity(self):
        p = dict(kind='performance', placement='fnos', offload=True)
        good = [self.backend(delivered=10), self.backend(delivered=200)]
        self.assertEqual(m.assess_backend(self.c, p, good, 14)['status'], 'PASS')
        self.assertEqual(m.assess_backend(self.c, p, [self.backend(), self.backend()], 14)['status'], 'INCONCLUSIVE')
        other = [self.backend(group='239.2.2.2'), self.backend(group='239.2.2.2', delivered=200)]
        self.assertEqual(m.assess_backend(self.c, p, other, 14)['status'], 'INCONCLUSIVE')
        self.assertEqual(m.assess_backend(self.c, p, [self.backend(mask='0018')]*2, 14)['status'], 'FAIL')
        self.assertEqual(m.assess_backend(self.c, dict(p, placement='hg'), good, 14)['status'], 'FAIL')
        sparse=good+[dict(self.backend(delivered=300),detail='')]*8
        self.assertEqual(m.assess_backend(self.c,p,sparse,14)['status'],'INCONCLUSIVE')

    def test_bind_alone_does_not_prove_forwarding_bypass(self):
        rows=[]
        for on in (False,True):
            for _ in range(2):
                rows.append(dict(kind='performance',placement='fnos',offload=on,
                                 clients={'fnos':{'mbps':38}},cpu={'hg':{'net':{
                                     'br-lan.10':{'tx_mbps':.1 if on else 39.5},'eth0':{'tx_mbps':39.5}}}}))
        self.assertEqual(m.assess_bypass(self.c,rows)['status'],'PASS')
        rows[-1]['cpu']['hg']['net']['br-lan.10']['tx_mbps']=39.5
        self.assertEqual(m.assess_bypass(self.c,rows)['status'],'INCONCLUSIVE')

    def test_aggregate_survives_an_incomplete_phase(self):
        self.assertEqual(m.aggregate([dict(kind='performance', placement='fnos', offload=True)]), {})

    def test_config_rejects_ssh_options_fcc_and_wrong_group(self):
        raw = json.loads((HERE/'configs/iptv.example.json').read_text())
        for edit in ('ssh', 'fcc', 'group', 'bool'):
            c = copy.deepcopy(raw)
            if edit == 'ssh': c['hg']['ssh'] = '-oProxyCommand=anything'
            if edit == 'fcc': c['hg']['url'] += '?fcc=198.51.100.10:1234'
            if edit == 'group': c['fnos']['url'] = c['fnos']['url'].replace('239.1.1.1','239.1.1.2')
            if edit == 'bool': c['channel']['ingress_ttl'] = True
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'config.json';path.write_text(json.dumps(c))
                with self.assertRaises(ValueError): m.config(path)

    def test_dry_run_has_no_subprocess(self):
        with mock.patch.object(runner.subprocess, 'run', side_effect=AssertionError('must not execute')), \
             mock.patch.object(runner.subprocess, 'Popen', side_effect=AssertionError('must not execute')), \
             mock.patch('builtins.print'):
            self.assertEqual(runner.main(['--config',str(HERE/'configs/iptv.example.json'),'--dry-run']), 0)

    def test_stale_or_modified_probe_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'probe'
            elf=bytearray(20);elf[:6]=b'\x7fELF\x02\x01';elf[18:20]=(62).to_bytes(2,'little')
            path.write_bytes(elf)
            runner.manifest(path,'fnos')
            runner.manifest(path,'fnos',verify=True)
            path.write_bytes(elf+b'changed')
            with self.assertRaises(ValueError):runner.manifest(path,'fnos',verify=True)

    def test_cpu_uses_dynamic_core_count_and_detects_reset(self):
        meta = dict(meta=True, hz=100, cpus=2)
        rows = [dict(time=i, total=200*i, idle=150*i, user=10*i, system=10*i, softirq=20*i,
                     rtp_ticks=5*i, rtp_identity=42, freq_khz=800000,
                     cores=[[100*i,70*i],[100*i,80*i]], net={'eth0':dict(rx=1000*i,tx=2000*i)}) for i in range(31)]
        text = '\n'.join(json.dumps(x) for x in [meta]+rows)
        result=m.cpu_summary(text,0,30)
        self.assertEqual(result['cpu_total_pct'],25)
        self.assertEqual(result['rtp_cpu_one_core_pct'],5)
        self.assertEqual(result['max_core_pct'],30)
        incomplete=copy.deepcopy(rows)
        for r in incomplete:r['cores']=r['cores'][:1]
        with self.assertRaisesRegex(ValueError,'whole-host'):
            m.cpu_summary('\n'.join(json.dumps(x) for x in [dict(meta,cpus=1)]+incomplete),0,30)
        rows[-3]['net']['eth0']['tx']=0
        with self.assertRaises(ValueError): m.cpu_summary('\n'.join(json.dumps(x) for x in [meta]+rows),0,30)


class Recovery(unittest.TestCase):
    """Run the actual POSIX recovery logic against files and fake commands only."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='iptv-guard-unit-')
        self.root = Path(self.temp.name); self.remote=self.root/'hiptv_aabbcc'
        self.remote.mkdir(); self.lock=self.root/'lock';self.lock.mkdir()
        (self.lock/'owner').write_text('hiptv-unit\n')
        self.sys=self.root/'sys';self.bridge=self.sys/'br-lan/bridge';self.bridge.mkdir(parents=True)
        for name in ('multicast_snooping','multicast_querier'): (self.bridge/name).write_text('0\n')
        (self.root/'control').write_text('1\n');(self.root/'gro').write_text('on\n')
        (self.remote/'original').write_text('0 0 0 on br-lan pon0\n')
        text=(HERE/'helpers/hg_iptv_guard.sh').read_text()
        text=text.replace('/tmp/hiptv_',str(self.root)+'/hiptv_').replace('/tmp/hg-qos-regression.lock',str(self.lock))
        text=text.replace('/sys/kernel/debug/ppe/multicast_enable',str(self.root/'control')).replace('/sys/class/net',str(self.sys))
        (self.remote/'guard.sh').write_text(text)
        bin_dir=self.root/'bin';bin_dir.mkdir()
        fake='#!'+sys.executable+'\n'+r'''
import os,sys,json
from pathlib import Path
r=Path(os.environ['IPTV_FIXTURE_ROOT']);cmd=Path(sys.argv[0]).name;a=sys.argv[1:]
if cmd=='ethtool':
    if a[0]=='-K':
        if (r/'fail-gro').exists():sys.exit(1)
        (r/'gro').write_text(a[-1]+'\n')
    else: print('rx-gro-list: '+(r/'gro').read_text().strip())
elif cmd=='ip':
    for key,file in [('mcast_snooping','multicast_snooping'),('mcast_querier','multicast_querier')]:
        (r/'sys/br-lan/bridge'/file).write_text(a[a.index(key)+1]+'\n')
elif cmd=='airoha-mcastctl':print(json.dumps(dict(paused=1-int((r/'control').read_text()),broken=0,ppe_stopped=0)))
elif cmd=='jsonfilter':
    data=json.loads(Path(a[a.index('-i')+1]).read_text());print(data[a[a.index('-e')+1][2:]])
'''
        for name in ('ethtool','ip','airoha-mcastctl','jsonfilter'):
            path=bin_dir/name;path.write_text(fake);path.chmod(0o700)
        self.env=dict(os.environ,PATH=str(bin_dir)+':'+os.environ['PATH'],IPTV_FIXTURE_ROOT=str(self.root))

    def tearDown(self): self.temp.cleanup()

    def call(self,action,*args):
        return subprocess.run(['/bin/sh',str(self.remote/'guard.sh'),str(self.remote),'hiptv-unit',action,*args],
                              env=self.env,capture_output=True,text=True,timeout=10)

    def changed(self):
        self.assertEqual(self.call('setup').returncode,0)
        self.assertEqual(self.call('mode','1','off').returncode,0)

    def assert_restored(self):
        self.assertEqual((self.root/'control').read_text().strip(),'1')
        self.assertEqual((self.root/'gro').read_text().strip(),'on')
        self.assertEqual((self.bridge/'multicast_snooping').read_text().strip(),'0')
        self.assertEqual((self.bridge/'multicast_querier').read_text().strip(),'0')
        self.assertTrue((self.remote/'restored').exists())
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.remote/'operation').exists())

    def test_normal_restore_and_idempotence(self):
        self.changed();r=self.call('restore');self.assertEqual(r.returncode,0,r.stderr)
        self.assert_restored();self.assertEqual(self.call('restore').returncode,0)
        self.assertNotEqual(self.call('mode','1','off').returncode,0)
        self.assert_restored()

    def test_watchdog_restores_after_controller_disappears(self):
        self.changed();(self.remote/'lease').write_text('0\n')
        r=self.call('watchdog');self.assertEqual(r.returncode,0,r.stderr);self.assert_restored()

    def test_changed_owner_is_not_overwritten(self):
        self.changed();(self.lock/'owner').write_text('another-test\n')
        self.assertNotEqual(self.call('restore').returncode,0)
        self.assertEqual((self.root/'gro').read_text().strip(),'off')
        self.assertEqual((self.lock/'owner').read_text().strip(),'another-test')
        self.assertTrue((self.remote/'restore.error').exists())

    def test_restore_failure_retains_recovery_and_pauses_offload(self):
        self.changed();(self.root/'fail-gro').touch()
        self.assertNotEqual(self.call('restore').returncode,0)
        self.assertEqual((self.root/'control').read_text().strip(),'0')
        self.assertTrue((self.lock/'owner').exists())
        self.assertTrue((self.remote/'restore.error').exists())
        self.assertFalse((self.remote/'restored').exists())
        self.assertFalse((self.remote/'operation').exists())


if __name__=='__main__': unittest.main()
