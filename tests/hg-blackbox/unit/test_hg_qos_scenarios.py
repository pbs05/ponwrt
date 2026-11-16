import importlib.util
import json
import ipaddress
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hg_qos_scenarios", ROOT / "hg-qos-scenarios.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from hg_qos_pcap import tcp_observations


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / "configs/scenarios.example.json").read_text())

    def test_configuration(self):
        m.validate(self.c)

    def test_unsafe_container_rejected(self):
        self.c["endpoints"]["normal"]["container"] = "x;reboot"
        with self.assertRaises(ValueError):
            m.validate(self.c)

    def test_same_ports_distinct_identities(self):
        cases = m.make_cases(self.c)
        self.assertEqual(len({c["Port"] for c in cases}), 1)
        self.assertEqual({c["ExpectedDSCP"] for c in cases[:12]}, {7, 3, 8})
        self.assertEqual({c["Role"] for c in cases[:12]}, set(m.ROLES))

    def test_base_services_override_identity(self):
        exceptions = [c for c in m.make_cases(self.c) if "exception" in c["Name"]]
        self.assertTrue(exceptions)
        self.assertTrue(all(c["ExpectedDSCP"] == 7 for c in exceptions))
        self.assertTrue(all(c["SourcePort"] in (53, 67) for c in exceptions))
        self.assertTrue(all(c["Port"] >= 1024 for c in exceptions))

    def test_unique_tuples(self):
        cases = m.make_cases(self.c, congestion=True)
        tuples = {(c["Address"], c["SourcePort"], c["Port"], c["Protocol"]) for c in cases}
        self.assertEqual(len(cases), len(tuples))

    def test_contention_checks_both_orderings(self):
        cases = m.make_cases(self.c, congestion=True)
        self.assertEqual([c["Role"] for c in cases if c["Group"] == 1], ["normal", "proxy"])
        self.assertEqual([c["Role"] for c in cases if c["Group"] == 2], ["proxy", "background"])

    def test_one_bad_packet_is_failure(self):
        r = m.observation(7, dict(packets=1, dscp={"0": 1}), 100)
        self.assertEqual(r["status"], "FAIL")

    def test_missing_tos_is_not_a_dscp_failure(self):
        r = m.observation(7, dict(packets=100, dscp={"-1": 100}), 100)
        self.assertEqual(r["status"], "INCONCLUSIVE")

    def test_missing_observations_never_pass(self):
        self.assertEqual(m.observation(7, {}, 100)["status"], "INCONCLUSIVE")

    def test_incomplete_capture_never_passes(self):
        self.assertEqual(m.observation(7, dict(packets=100, dscp={"7": 100}), 120)["status"], "INCONCLUSIVE")

    def test_receiver_drops_never_pass(self):
        self.assertEqual(m.observation(7, dict(packets=100, dscp={"7": 100}), 100, socket_drops=2)["status"], "INCONCLUSIVE")

    def samples(self, cases, deliveries=None):
        sender = {"Sent": [], "Received": [], "Errors": []}
        receivers = {r: {"samples": [], "errors": []} for r in m.ROLES}
        for c in cases:
            for phase in (0, 1):
                count = int(c["RateMbps"]*1e6*c["Seconds"]/(8*c["Size"])) if c["Bulk"] and phase else 128
                n = int(count*(deliveries or {}).get(c["Role"], 1)) if phase else count
                reply = 0 if c["Bulk"] and phase else n
                sender["Sent"].append(dict(Case=c["Id"], Phase=phase, Packets=count, Bytes=count*c["Size"], Replies=reply, Errors=0))
                sender["Received"].append(dict(Case=c["Id"], Phase=phase, Packets=reply, DSCP={str(c["ExpectedDSCP"]): reply}))
                receivers[c["Role"]]["samples"].append(dict(case=c["Id"], phase=phase, packets=n, bytes=n*c["Size"],
                                                             dscp={str(c["ExpectedDSCP"]): n}, socket_drops=0))
        return sender, receivers

    def test_both_directions_and_phases(self):
        cases = m.make_cases(self.c)
        sender, receivers = self.samples(cases)
        r, competition = m.assess(cases, sender, receivers, 1000)
        self.assertTrue(all(x["status"] == "PASS" for x in r))
        self.assertFalse(competition)
        sender["Received"][0]["DSCP"] = {"0": 128}
        r, _ = m.assess(cases, sender, receivers, 1000)
        self.assertEqual(r[0]["status"], "FAIL")
        self.assertEqual(r[0]["phases"][0]["reverse"]["status"], "FAIL")

    def test_transport_loss_fails(self):
        cases = m.make_cases(self.c)[:1]
        sender, receivers = self.samples(cases)
        sender["Sent"][1]["Replies"] = 100
        r, _ = m.assess(cases, sender, receivers, 1000)
        self.assertEqual(r[0]["status"], "FAIL")

    def test_null_receiver_samples(self):
        r, _ = m.assess(m.make_cases(self.c)[:1], {}, {"normal": {"samples": None}}, 1000)
        self.assertEqual(r[0]["status"], "INCONCLUSIVE")

    def contention(self, deliveries, **kwargs):
        cases = [c for c in m.make_cases(self.c, congestion=True) if c["Group"] == 1]
        sender, receivers = self.samples(cases, deliveries)
        return m.assess(cases, sender, receivers, 1000, **kwargs)[1][0]

    def test_effective_priority_passes(self):
        self.assertEqual(self.contention({"normal": .99, "proxy": .46})["status"], "PASS")

    def test_equal_delivery_under_saturation_fails(self):
        self.assertEqual(self.contention({"normal": .73, "proxy": .73})["status"], "FAIL")

    def test_not_saturated_never_passes(self):
        self.assertEqual(self.contention({"normal": 1, "proxy": 1})["status"], "INCONCLUSIVE")

    def test_host_drops_invalidate_scheduling(self):
        self.assertEqual(self.contention({"normal": .99, "proxy": .46}, host_drops=1)["status"], "INCONCLUSIVE")

    def test_wrong_link_invalidate_scheduling(self):
        self.assertEqual(self.contention({"normal": .99, "proxy": .46}, link_valid=False)["status"], "INCONCLUSIVE")

    def test_bulk_does_not_claim_reverse_marking(self):
        cases = [c for c in m.make_cases(self.c, congestion=True) if c["Bulk"]]
        sender, receiver = self.samples(cases)
        r, _ = m.assess(cases, sender, receiver, 1000)
        self.assertTrue(all(p["reverse"] is None for c in r for p in c["phases"]))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        c = json.loads((ROOT / "configs/scenarios.example.json").read_text())
        self.case = [x for x in m.make_cases(c) if x["Protocol"] == "tcp"][0]

    def frame(self, dscp=7, nonce=123):
        c = self.case
        ip = bytearray(20)
        ip[0], ip[1], ip[9] = 0x45, dscp << 2, 6
        ip[2:4] = (64).to_bytes(2, "big")
        ip[12:16] = ipaddress.IPv4Address(c["Address"]).packed
        ip[16:20] = ipaddress.IPv4Address("192.0.2.2").packed
        tcp = bytearray(20)
        tcp[:4] = struct.pack("!HH", c["Port"], c["SourcePort"])
        tcp[12] = 0x50
        payload = bytearray(24)
        payload[:4], payload[4], payload[6] = b"HGQS", 1, c["Id"]
        payload[8:16], payload[16:24] = struct.pack("!Q", 1), struct.pack("!Q", nonce)
        return bytes(12)+b"\x08\x00"+ip+tcp+payload

    def pcap(self, frames, endian="<"):
        def block(kind, body):
            size = len(body)+12
            return struct.pack(endian+"II", kind, size)+body+struct.pack(endian+"I", size)
        data = block(0x0a0d0d0a, struct.pack(endian+"IHHq", 0x1a2b3c4d, 1, 0, -1))
        data += block(1, struct.pack(endian+"HHI", 1, 0, 128))
        for frame in frames:
            body = struct.pack(endian+"IIIII", 0, 0, 0, len(frame), len(frame))+frame
            data += block(6, body+bytes((-len(body)) % 4))
        return data

    def test_parse_little_and_big_endian(self):
        for endian in ("<", ">"):
            rows = tcp_observations(self.pcap([self.frame()], endian), [self.case], 123, "192.0.2.2")
            self.assertEqual(rows[0]["DSCP"], {"7": 1})

    def test_ignores_other_runs_and_directions(self):
        self.assertEqual(tcp_observations(self.pcap([self.frame(nonce=456)]), [self.case], 123, "192.0.2.2"), [])
        self.assertEqual(tcp_observations(self.pcap([self.frame()]), [self.case], 123, "192.0.2.3"), [])

    def test_duplicates_do_not_inflate_coverage(self):
        rows = tcp_observations(self.pcap([self.frame(), self.frame()]), [self.case], 123, "192.0.2.2")
        self.assertEqual(rows[0]["Packets"], 1)

    def test_wrong_mark_on_repeated_record_is_not_hidden(self):
        rows = tcp_observations(self.pcap([self.frame(), self.frame(dscp=0)]), [self.case], 123, "192.0.2.2")
        self.assertEqual(rows[0]["DSCP"], {"7": 1, "0": 1})

    def test_truncated_capture_rejected(self):
        with self.assertRaises(ValueError):
            tcp_observations(self.pcap([self.frame()])[:-1], [self.case], 123, "192.0.2.2")


if __name__ == "__main__":
    unittest.main()
