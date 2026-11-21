#!/usr/bin/env python3
"""Host-only controller/profile and init-script contract tests."""
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / 'package/network/config/airoha-qos'

class Controller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp', prefix='qosctl-test-')
        cls.binary = pathlib.Path(cls.tmp.name) / 'ctl'
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-O2',
                        str(SRC / 'src/airoha-qosctl.c'), '-o', str(cls.binary)], check=True)
        harness = pathlib.Path(cls.tmp.name) / 'status.c'
        harness.write_text('#define main controller_main\n#include "' +
                           str(SRC / 'src/airoha-qosctl.c') + '"\n#undef main\n'
                           'int main(void) { uint8_t map[64] = {0}; '
                           'print_profile(0, map, 0, 0, NULL); return 0; }\n')
        cls.status = pathlib.Path(cls.tmp.name) / 'status'
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(harness),
                        '-o', str(cls.status)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_ctl(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True, text=True)

    def test_complete_default_and_override(self):
        r = self.run_ctl('validate', 'dscp')
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads(r.stdout)
        self.assertEqual(p['map'], [0, 0, 2, 2, 6, 6, 4, 4] * 8)
        r = self.run_ctl('validate', 'dscp', '7:0', '63:7')
        p = json.loads(r.stdout)
        self.assertEqual(p['map'][7], 0)
        self.assertEqual(p['map'][63], 7)
        self.assertEqual(len(p['map']), 64)
        for mode in ('priority', 'compat'):
            p = json.loads(self.run_ctl('validate', mode).stdout)
            self.assertEqual(p['mode'], 'priority')
            self.assertEqual(p['priority_map'], [4, 0, 0, 0, 4, 0, 7, 7] + [4] * 8)

    def test_old_kernel_does_not_claim_new_mapping(self):
        p = json.loads(subprocess.check_output([str(self.status)], text=True))
        self.assertEqual(p['mode'], 'compat')
        self.assertIsNone(p['priority_map'])

    def test_invalid_never_reaches_kernel(self):
        for args in [('dscp', '64:1'), ('dscp', '1:8'), ('dscp', '1:2', '1:3'),
                     ('dscp', '-1:2'), ('dscp', '1:2:3'), ('dscp', '1:'),
                     ('dscp', '1:0x7'), ('dscp', '1:$(id)'), ('bogus',),
                     ('dscp', '99999999999999999999999999999:1')]:
            with self.subTest(args=args):
                self.assertEqual(self.run_ctl('validate', *args).returncode, 2)

    def run_boot(self, mode='dscp', maps='7:4 3:2', device='lan1',
                 apply_rc=0, get_rc=0, load_rc=0):
        init = (SRC / 'files/airoha-qos.init').read_text()
        init = init.replace('. /lib/functions.sh', ':')
        init = init.replace('/usr/sbin/airoha-qosctl', 'fake_ctl')
        helpers = f"""
logger() {{ echo "LOG $*" >&2; }}
config_load() {{ return {load_rc}; }}
config_get() {{
    case "$1" in
        device) device='{device}';;
        mode) mode='{mode}'; mode="${{mode:-$4}}";;
        maps) maps='{maps}';;
    esac
}}
fake_ctl() {{
    case "$1" in
        validate) '{self.binary}' "$@";;
        get) echo "GET $2" >&2; return {get_rc};;
        apply) shift; echo "APPLY $*"; return {apply_rc};;
    esac
}}
boot
"""
        return subprocess.run(['sh'], input=init + helpers, capture_output=True, text=True)

    def test_boot_only_applies_explicit_valid_dscp(self):
        r = self.run_boot()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'APPLY lan1 dscp 7:4 3:2')
        self.assertIn('GET lan1', r.stderr)
        for args, expected_rc in [(dict(mode='priority'), 0), (dict(mode='compat'), 0), (dict(mode=''), 0),
                                  (dict(mode='bogus'), 2), (dict(maps='7:9 3:2'), 2),
                                  (dict(maps='7:4 7:2'), 2), (dict(load_rc=3), 3)]:
            with self.subTest(args=args):
                r = self.run_boot(**args)
                self.assertEqual(r.returncode, expected_rc, r.stderr)
                self.assertEqual(r.stdout, '')
                self.assertNotIn('GET ', r.stderr)
        config = (SRC / 'files/airoha_qos').read_text()
        self.assertIn("option mode 'priority'", config)

    def test_boot_does_not_guess_another_device(self):
        r = self.run_boot(device='missing', get_rc=4)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertIn('GET missing', r.stderr)
        self.assertNotIn('GET lan1', r.stderr)

    def test_boot_preserves_apply_failure_status(self):
        r = self.run_boot(apply_rc=7)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(r.stdout.strip(), 'APPLY lan1 dscp 7:4 3:2')
        self.assertIn('Boot apply failed (7)', r.stderr)

    def test_uapi_matches_kernel(self):
        patch = (ROOT / 'target/linux/airoha/patches-6.18/940-04-net-airoha-boot-qos-netlink.patch').read_text()
        source = (SRC / 'src/airoha_qos.h').read_text()
        for line in source.splitlines():
            self.assertIn('+' + line, patch)

    def test_init_order_and_reload(self):
        init = (SRC / 'files/airoha-qos.init').read_text()
        self.assertIn('START=16', init)
        # Exercise the real service functions with a fake logger. Runtime
        # start/reload/restart must never invoke the boot-only controller.
        script = init + '\nlogger() { :; }\nstart\nreload\nrestart\nstop\n'
        r = subprocess.run(['sh'], input=script, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '')

if __name__ == '__main__':
    unittest.main(verbosity=2)
