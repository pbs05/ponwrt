import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'helpers'))
from hg_qos_common import check_qos_profile


class ProfileTests(unittest.TestCase):
    def test_dscp_profile_and_queue_zero(self):
        actual = {'mode': 'dscp', 'broken': 0, 'map': [0, 0, 2, 2, 6, 6, 4, 4] * 8}
        expected = {'mode': 'dscp', 'map': {'7': 4, '3': 2, '8': 0}}
        self.assertTrue(check_qos_profile(json.dumps(actual), expected)['checked'])
        actual['map'][8] = 4
        with self.assertRaisesRegex(ValueError, 'expected Q0, got Q4'):
            check_qos_profile(json.dumps(actual), expected)

    def test_fault_mode_and_missing_map(self):
        for actual in ({'mode': 'priority'}, {'mode': 'dscp'},
                       {'mode': 'dscp', 'broken': 1}, {'mode': 'compat'}):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                check_qos_profile(json.dumps(actual), {'mode': 'dscp'})

    def test_unknown_old_kernel_is_not_invented(self):
        self.assertEqual(check_qos_profile('not found')['runtime']['mode'], 'unknown')
        with self.assertRaises(ValueError):
            check_qos_profile('not found', {'mode': 'dscp'})

    def test_priority_requires_actual_table(self):
        actual = {'mode': 'priority', 'priority_map': [4, 0, 0, 0, 4, 0, 7, 7] + [4] * 8}
        self.assertTrue(check_qos_profile(json.dumps(actual), {'mode': 'priority'})['checked'])
        actual['priority_map'] = None
        with self.assertRaises(ValueError):
            check_qos_profile(json.dumps(actual), {'mode': 'priority'})
