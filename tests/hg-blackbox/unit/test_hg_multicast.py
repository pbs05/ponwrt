#!/usr/bin/env python3
"""No SSH or packets: regression decisions and native receiver serialization."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT/'tests/hg-blackbox/hg-multicast-regression.py'
spec = importlib.util.spec_from_file_location('multicast', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Multicast(unittest.TestCase):
    def config(self):
        return dict(group='239.77.1.1', ttl=8, dscp=7, ecn=2, payload=1400,
                    receivers=[dict(name='a', member=True),dict(name='b', member=True)])

    def data(self):
        return dict(packets=3, duplicates=0, invalid_metadata=0, limited=0, udp_drops=0,
                    sequences=[0,1,2], ttl={'8':3}, dscp={'7':3}, ecn={'2':3}, lengths={'1428':3})

    def evaluate(self, data, phase=None):
        return m.evaluate_local(self.config(), phase or dict(local=True),
                                dict(sent={'239.77.1.1':3}), data)['status']

    def test_valid_local_headers(self):
        self.assertEqual(self.evaluate(self.data()), 'PASS')

    def test_forwarded_ttl_is_wrong_for_local_socket(self):
        self.assertEqual(self.evaluate(dict(self.data(),ttl={'7':3})), 'FAIL')

    def test_duplicate_missing_wrong_interface_or_tos_fails(self):
        for edit in (dict(duplicates=1),dict(sequences=[0,2]),dict(invalid_metadata=1),
                     dict(dscp={'0':3}),dict(ecn={'0':3}),dict(lengths={'1500':3})):
            self.assertEqual(self.evaluate(dict(self.data(),**edit)), 'FAIL')

    def test_probe_overflow_is_inconclusive(self):
        self.assertEqual(self.evaluate(dict(self.data(),udp_drops=1)), 'INCONCLUSIVE')
        self.assertEqual(self.evaluate(dict(self.data(),limited=1)), 'INCONCLUSIVE')

    def test_optional_local_phases(self):
        c = self.config()
        self.assertEqual(len(m.phases(c)), 5)
        c['local_probe'] = '/tmp/hg-mcast-local'
        self.assertEqual(len(m.phases(c)), 9)
        self.assertEqual(m.phases(c)[-2]['name'], 'local-only')
        self.assertEqual(m.phases(c)[-2]['members'], [])
        self.assertFalse(m.phases(c)[-1]['local'])

    def test_native_helper_serialization(self):
        src = ROOT/'tests/hg-blackbox/helpers/hg_mcast_local.c'
        with tempfile.TemporaryDirectory(prefix='hg-mcast-local-test-') as d:
            d = Path(d)
            fixture = '#define main probe_main\n#include "'+str(src)+'"\n#undef main\n'+r'''
int main(void) {
    records[3].packets = 2;
    records[3].seen[0] = true;
    records[3].seen[9999] = true;
    records[3].ttl[8] = 2;
    records[3].tos[(7 << 2) | 2] = 2;
    records[3].length[9000] = 2;
    collect(3);
    return 0;
}
'''
            (d/'test.c').write_text(fixture)
            subprocess.run(['cc','-Wall','-Wextra','-fsanitize=undefined,bounds','-g','-o',str(d/'probe'),str(d/'test.c')], check=True)
            r = subprocess.run([str(d/'probe')], capture_output=True, text=True, check=True)
            self.assertEqual(r.stderr, '')
            data = json.loads(r.stdout)
            self.assertEqual(data['sequences'], [0,9999])
            self.assertEqual(data['dscp'], {'7':2})
            self.assertEqual(data['ecn'], {'2':2})
            self.assertEqual(data['lengths'], {'9000':2})

if __name__ == '__main__':
    unittest.main()
