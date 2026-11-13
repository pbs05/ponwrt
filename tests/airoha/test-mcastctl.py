#!/usr/bin/env python3
"""Host-only tests for the real multicast controller, parser and boot worker."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'package/network/config/airoha-multicast'

class Controller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='hg-mcastctl-')
        cls.work = Path(cls.temp.name)
        cls.exe = cls.work / 'ctl'
        subprocess.run(['cc', '-Wall', '-Wextra', '-O2', '-o', str(cls.exe),
                        str(PACKAGE / 'src/airoha-mcastctl.c')], check=True)
        fixture = '#define main controller_main\n#include "' + str(PACKAGE / 'src/airoha-mcastctl.c') + '"\n#undef main\n' + r'''
#include <assert.h>
int main(void) {
    struct message m;
    struct nlattr *a[AIROHA_MCAST_A_MAX + 1];
    struct airoha_mcast_status status = { .enabled = 1, .frozen = 1, .active = 2 };
    uint8_t enabled = 1;
    init_message(&m, 32, AIROHA_MCAST_CMD_APPLY);
    attr(&m, AIROHA_MCAST_A_ENABLED, &enabled, sizeof(enabled));
    assert(parse_attrs(&m.n, a, AIROHA_MCAST_A_MAX) == 0);
    assert(*(uint8_t *)value(a[AIROHA_MCAST_A_ENABLED], 1) == 1);
    assert(value(a[AIROHA_MCAST_A_ENABLED], 4) == NULL);
    attr(&m, AIROHA_MCAST_A_ENABLED, &enabled, sizeof(enabled));
    assert(parse_attrs(&m.n, a, AIROHA_MCAST_A_MAX) == -1);
    init_message(&m, 32, AIROHA_MCAST_CMD_GET);
    attr(&m, AIROHA_MCAST_A_STATUS, &status, sizeof(status));
    assert(parse_attrs(&m.n, a, AIROHA_MCAST_A_MAX) == 0);
    assert(value(a[AIROHA_MCAST_A_STATUS], sizeof(status)) != NULL);
    ((struct nlattr *)m.attrs)->nla_len = 1;
    assert(parse_attrs(&m.n, a, AIROHA_MCAST_A_MAX) == -1);
    print_status(&status);
    return 0;
}
'''
        (cls.work/'parser.c').write_text(fixture)
        subprocess.run(['cc', '-O2', '-o', str(cls.work/'parser'), str(cls.work/'parser.c')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_parser_and_json(self):
        s = json.loads(subprocess.check_output([str(self.work/'parser')]))
        self.assertEqual(s['active'], 2)
        self.assertEqual(s['qualification'], 'pending')
        self.assertEqual(set(s['reject']), {'packet','ingress','scope','route','bridge','resource','sample'})

    def test_invalid_arguments_do_not_open_netlink(self):
        for args in ([], ['apply'], ['apply','2'], ['apply','0','lo','lo'], ['apply','1'], ['get','lo']):
            with self.subTest(args=args):
                r = subprocess.run([str(self.exe), *args], capture_output=True)
                self.assertEqual(r.returncode, 2)
                self.assertIn(b'Usage:', r.stderr)

    def test_absent_interfaces_fail_without_applying(self):
        r = subprocess.run([str(self.exe),'apply','1','no-such-hg-if','no-such-hg-if'], capture_output=True)
        self.assertEqual(r.returncode, 1)

    def worker(self, enabled, resolved=True, apply_ok=True):
        with tempfile.TemporaryDirectory(prefix='hg-mcast-boot-') as d:
            d = Path(d)
            worker = (PACKAGE/'files/airoha-multicast-boot').read_text()
            worker = worker.replace('. /lib/functions/network.sh', r'''
network_flush_cache() { :; }
network_get_device() { eval "$1=$2-device"; RESOLVE; }
logger() { printf '%s\n' "$*" >> LOG; }
sleep() { :; }
'''.replace('RESOLVE', ':' if resolved else 'return 1').replace('LOG', str(d/'log')))
            worker = worker.replace('/var/run/airoha-multicast.boot', str(d/'lock'))
            worker = worker.replace('/usr/sbin/airoha-mcastctl', str(d/'ctl'))
            (d/'ctl').write_text('#!/bin/sh\nprintf "%s\n" "$*" >> "' + str(d/'calls') + '"\nexit ' + ('0' if apply_ok else '1') + '\n')
            (d/'ctl').chmod(0o700)
            (d/'worker').write_text(worker)
            subprocess.run(['sh',str(d/'worker'),enabled,'iptv','lan'], check=True, capture_output=True)
            # Same-boot duplicate workers cannot apply a second policy.
            subprocess.run(['sh',str(d/'worker'),enabled,'guest','iot'], check=True, capture_output=True)
            return (d/'calls').read_text().splitlines() if (d/'calls').exists() else []

    def test_disabled_policy_freezes_without_netifd(self):
        self.assertEqual(self.worker('0', resolved=False), ['apply 0'])

    def test_boot_resolves_logical_names_and_uses_one_snapshot(self):
        self.assertEqual(self.worker('1'), ['apply 1 iptv-device lan-device'])

    def test_missing_interfaces_never_apply(self):
        self.assertEqual(self.worker('1', resolved=False), [])

    def test_failed_requests_are_bounded(self):
        self.assertEqual(len(self.worker('1', apply_ok=False)), 150)

    def test_service_restart_uses_policy_helper(self):
        source = (PACKAGE/'files/airoha-multicast.init').read_text()
        self.assertIn('restart() { start; }', source)
        self.assertIn('start() { /usr/libexec/airoha-multicast-policy apply; }', source)

if __name__ == '__main__':
    unittest.main()
