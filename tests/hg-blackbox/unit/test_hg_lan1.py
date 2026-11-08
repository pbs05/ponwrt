import importlib.util
from pathlib import Path
import unittest

FILE = Path(__file__).resolve().parents[1] / 'hg-lan1-regression.py'
SPEC = importlib.util.spec_from_file_location('hg_lan1', FILE)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def sample(t, **changes):
    data = dict.fromkeys(M.NUMBERS, 0)
    data.update(boot_id='01234567-0123-0123-0123-0123456789ab',
                carrier=1, speed=2500, pcs_rx_sync=1, freqdet_lock=1,
                led_offloaded='1', uptime=1000 + t)
    data.update(changes)
    return {'elapsed': t, 'data': data}


class LAN1Tests(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(M.evaluate([sample(i) for i in (0, 5, 10)])[0], 'PASS')

    def test_carrier_flap(self):
        self.assertEqual(M.evaluate([sample(0), sample(5, carrier_changes=2),
                                     sample(10, carrier_changes=2)])[0], 'FAIL')

    def test_transient_sync(self):
        self.assertEqual(M.evaluate([sample(0), sample(1, pcs_rx_sync=0),
                                     sample(2), sample(3)])[0], 'PASS')

    def test_persistent_sync(self):
        self.assertEqual(M.evaluate([sample(0), sample(5, pcs_rx_sync=0),
                                     sample(10, pcs_rx_sync=0)])[0], 'FAIL')

    def test_counter_errors(self):
        for k in M.ERRORS:
            self.assertEqual(M.evaluate([sample(0), sample(5, **{k: 1}),
                                         sample(10, **{k: 1})])[0], 'FAIL')

    def test_historical_errors_and_drops(self):
        self.assertEqual(M.evaluate([sample(i, rx_errors=4, rx_dropped=i)
                                     for i in (0, 5, 10)])[0], 'PASS')

    def test_gap_is_not_proof_of_hg_fault(self):
        s = [sample(0), {'elapsed': 5, 'error': 'jump host down'}, sample(10), sample(15)]
        self.assertEqual(M.evaluate(s)[0], 'INCONCLUSIVE')

    def test_reboot_warmup(self):
        new = '22222222-2222-2222-2222-222222222222'
        s = [sample(0), {'elapsed': 5, 'error': 'expected reboot'}]
        s += [sample(i, boot_id=new) for i in (30, 35, 40)]
        self.assertEqual(M.evaluate(s, event='reboot')[0], 'PASS')
        self.assertEqual(M.evaluate([sample(i) for i in (0, 35, 40)], event='reboot')[0], 'INCONCLUSIVE')

    def test_event_must_be_observed(self):
        s = [sample(i) for i in (0, 35, 40)]
        self.assertEqual(M.evaluate(s, event='link-change')[0], 'INCONCLUSIVE')
        s[-1]['data']['carrier_changes'] = 2
        self.assertEqual(M.evaluate(s, event='link-change')[0], 'PASS')

    def test_led(self):
        self.assertEqual(M.evaluate([sample(i, led_offloaded='0') for i in (0, 5, 10)])[0], 'FAIL')

    def test_counter_reset(self):
        self.assertEqual(M.evaluate([sample(0, carrier_changes=3), sample(5), sample(10)])[0], 'INCONCLUSIVE')

    def test_parse_requires_complete_snapshot(self):
        s = sample(0)['data']
        text = ''.join(f'{k}={v}\n' for k, v in s.items()) + '__KERNEL_LOG__\nmessage\n'
        parsed = M.parse_snapshot(text)
        self.assertEqual(parsed['carrier'], 1)
        self.assertEqual(parsed['kernel_log'], ['message'])
        for bad in (text.replace('carrier=1', 'carrier=garbled'),
                    text.replace('carrier=1\n', ''),
                    text.replace('__KERNEL_LOG__\n', ''),
                    text.replace('speed=2500', 'carrier=1\nspeed=2500')):
            with self.assertRaises((ValueError, KeyError)):
                M.parse_snapshot(bad)


if __name__ == '__main__':
    unittest.main()
