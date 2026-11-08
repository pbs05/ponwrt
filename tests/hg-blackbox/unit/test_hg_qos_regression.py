import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hg_qos_regression", ROOT / "hg-qos-regression.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class QoSRegressionTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / "configs/regression.example.json").read_text())

    def samples(self, deliveries=(1, 1, 1), rate=450, seconds=2.5):
        sent, received = [], []
        for t, delivered in zip(self.c["tiers"], deliveries):
            n = int(rate * 1e6 * seconds / 8 / 1400)
            sent.append(dict(port=t["port"], phase=1, packets=n, bytes=n*1400))
            nr = int(n*delivered)
            received.append(dict(port=t["port"], phase=1, packets=nr, bytes=nr*1400,
                                 dscp={str(t["dscp"]): nr}, socket_drops=0))
        return sent, received

    def verdict(self, deliveries, **kw):
        tx, rx = self.samples(deliveries)
        return m.assess(self.c, "congestion", tx, rx, seconds=2.5, congestion=True,
                        hardware_bound=kw.pop("hardware_bound", True), **kw)

    def test_config(self):
        m.validate(self.c)

    def test_no_shell_interface_injection(self):
        self.c["output_device"] = "lan4;reboot"
        with self.assertRaises(ValueError):
            m.validate(self.c)

    def test_duplicate_ports_rejected(self):
        self.c["tiers"][1]["port"] = self.c["tiers"][0]["port"]
        with self.assertRaises(ValueError):
            m.validate(self.c)

    def test_production_does_not_install_rules(self):
        self.assertEqual(m.make_rules(self.c, "test", "production-bridge"), ("", ""))

    def test_bridge_classifier_precedes_flow_add(self):
        for case in ("bridge-auto", "bridge-slow-control"):
            _, rules = m.make_rules(self.c, "test", case)
            self.assertIn("hook forward priority -205", rules)
            self.assertNotIn("hook forward priority -150", rules)

    def hook_chains(self, bridge=-210):
        return {"nftables": [{"chain": dict(family=f, table=t, name="forward", hook="forward", prio=p)}
                             for f, t, p in (("bridge", "hg_qos", bridge), ("bridge", "fw4", -200),
                                             ("inet", "hg_qos", -160), ("inet", "fw4", 0))]}

    def test_correct_numeric_hook_order(self):
        self.assertEqual(m.check_hook_order(self.hook_chains(), test_override=True)["bridge"]["test_classifier"], -205)

    def test_wrong_production_order_is_setup_error(self):
        with self.assertRaisesRegex(ValueError, "Configuration error"):
            m.check_hook_order(self.hook_chains(-160), test_override=True)

    def test_equal_hook_priority_is_rejected(self):
        with self.assertRaises(ValueError):
            m.check_hook_order(self.hook_chains(-200))

    def test_test_override_must_follow_production(self):
        with self.assertRaisesRegex(ValueError, "Test setup error"):
            m.check_hook_order(self.hook_chains(-202), test_override=True)

    def test_bridge_binding_uses_real_destination(self):
        text = "00400 BND IPv4 5T orig=192.0.2.2:50000->192.0.2.3:45101 new=192.0.2.2:50000->192.0.2.3:45101 "
        self.assertEqual(m.bound_ports(self.c, text, 50000, self.c["fnos_ip"]), [45101])
        self.assertEqual(m.bound_ports(self.c, text, 50000, self.c["test_vip"]), [])
        self.assertEqual(m.bound_ports(self.c, text, 50010, self.c["fnos_ip"]), [])

    def test_software_table_does_not_request_hardware(self):
        family, rules = m.make_rules(self.c, "test", "routed-software")
        self.assertEqual(family, "inet")
        self.assertIn("flow add @sw", rules)
        self.assertNotIn("flags offload", rules)
        self.assertIn("ct original ip daddr 198.19.255.3", rules)

    def test_control_notrack_is_scoped(self):
        _, rules = m.make_rules(self.c, "test", "bridge-slow-control")
        for line in rules.splitlines():
            if "notrack" in line:
                self.assertIn("192.0.2.2", line)
                self.assertIn("192.0.2.3", line)
                self.assertIn("45101", line)

    def test_rotation_changes_class_not_test_port(self):
        _, rules = m.make_rules(self.c, "test", "congestion", 1)
        self.assertIn("udp dport 45101 ip dscp set 3 meta priority set 2", rules)

    def test_dscp_regression_fails(self):
        tx, rx = self.samples(rate=4, seconds=1.5)
        for r in rx:
            r["dscp"] = {"0": r["packets"]}
        r = m.assess(self.c, "bridge-auto", tx, rx)
        self.assertEqual(r["status"], "FAIL")

    def test_small_positive_mismatch_not_hidden_as_low_sample(self):
        tx, rx = self.samples(rate=4, seconds=1.5)
        rx[0].update(packets=1, dscp={"0": 1})
        r = m.assess(self.c, "bridge-auto", tx, rx)
        self.assertEqual(r["packets"][0]["status"], "FAIL")

    def test_no_packets_never_passes(self):
        r = m.assess(self.c, "bridge-auto", [], [])
        self.assertEqual(r["status"], "INCONCLUSIVE")

    def test_low_rate_correct_marks_pass(self):
        tx, rx = self.samples(rate=4, seconds=1.5)
        self.assertEqual(m.assess(self.c, "routed-software", tx, rx)["status"], "PASS")

    def test_equal_share_is_priority_failure(self):
        self.assertEqual(self.verdict((.70, .70, .70))["priority"]["status"], "FAIL")

    def test_sp_like_delivery_passes(self):
        self.assertEqual(self.verdict((.99, .98, .15))["priority"]["status"], "PASS")

    def test_no_binding_is_inconclusive(self):
        self.assertEqual(self.verdict((.7, .7, .7), hardware_bound=False)["priority"]["status"], "INCONCLUSIVE")

    def test_receiver_errors_are_inconclusive(self):
        self.assertEqual(self.verdict((.7, .7, .7), udp_errors=1)["priority"]["status"], "INCONCLUSIVE")

    def test_sender_not_saturating_is_inconclusive(self):
        tx, rx = self.samples(rate=100)
        r = m.assess(self.c, "congestion", tx, rx, seconds=2.5, congestion=True, hardware_bound=True)
        self.assertEqual(r["priority"]["status"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
