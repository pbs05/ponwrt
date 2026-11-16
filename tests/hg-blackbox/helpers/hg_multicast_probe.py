#!/usr/bin/env python3
"""Bounded Linux multicast endpoint. JSON requests on stdin, replies on stdout.

The controller transfers this source in the SSH command; no remote files or
persistent interface settings are created. Packet membership is socket scoped.
"""
import collections
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import sys
import time

MAGIC = b"HGMCAST1"
HEADER = struct.Struct("!8s16sHI")
SOL_PACKET = 263


def metric():
    return {"packets": 0, "sequences": set(), "duplicates": 0,
            "dscp": collections.Counter(), "ecn": collections.Counter(),
            "ttl": collections.Counter(), "vlans": collections.Counter(),
            "lengths": collections.Counter(), "tuples": collections.Counter(), "fragments": 0}


class Probe:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = bytes.fromhex(cfg["token"])
        self.records = {}
        self.receivers = {}
        self.sockets = []
        self.raw = None
        self.udp_drops = 0
        self.limited = False
        self.observations = 0
        self.index = socket.if_nametoindex(cfg["interface"])
        if cfg["role"] == "receiver":
            self.raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            self.sockets.append(self.raw)
            self.raw.bind((cfg["interface"], 0))
            self.raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.raw.setsockopt(SOL_PACKET, 8, 1)  # PACKET_AUXDATA
            # Receive copies even when the NIC has no subscribed multicast MAC.
            # Closing this socket releases its promiscuous reference.
            self.raw.setsockopt(SOL_PACKET, 1, struct.pack("IHH8s", self.index, 1, 0, b""))
            self.raw.setblocking(False)

    def record(self, kind, body, group, source, sport, port, tos, ttl, vlans, length, fragments=0):
        if len(body) < HEADER.size:
            return
        magic, token, phase, sequence = HEADER.unpack_from(body)
        if magic != MAGIC or token != self.token:
            return
        self.observations += 1
        if self.observations > 300000:
            self.limited = True
            return
        key = (phase, group, kind)
        row = self.records.setdefault(key, metric())
        row["packets"] += 1
        row["duplicates"] += sequence in row["sequences"]
        row["sequences"].add(sequence)
        row["dscp"][str(tos >> 2)] += 1
        row["ecn"][str(tos & 3)] += 1
        row["ttl"][str(ttl)] += 1
        row["vlans"][",".join(str(v) for v in vlans)] += 1
        row["lengths"][str(length)] += 1
        row["tuples"][f"{source}:{sport}->{group}:{port}"] += 1
        row["fragments"] += bool(fragments)

    def packet(self):
        frame, anc, flags, address = self.raw.recvmsg(65535, 256)
        if address[2] == 4 or flags & socket.MSG_TRUNC or len(frame) < 14:
            return  # PACKET_OUTGOING would double-count local transmissions.
        protocol, offset, vlans = struct.unpack_from("!H", frame, 12)[0], 14, []
        for level, kind, value in anc:
            if level == SOL_PACKET and kind == 8 and len(value) >= 20:
                status, _, _, _, _, tci, _ = struct.unpack_from("IIIHHHH", value)
                if status & (1 << 4):
                    vlans.append(tci & 4095)
        while protocol in (0x8100, 0x88A8) and len(frame) >= offset + 4:
            tci, protocol = struct.unpack_from("!HH", frame, offset)
            vlans.append(tci & 4095)
            offset += 4
            if len(vlans) > 2:
                return
        if protocol != 0x0800 or len(frame) < offset + 20:
            return
        ip = frame[offset:]
        ihl = (ip[0] & 15) * 4
        length, fragment = struct.unpack_from("!HH", ip, 2)[0], struct.unpack_from("!H", ip, 6)[0]
        if ip[0] >> 4 != 4 or ihl < 20 or ip[9] != 17 or fragment & 0x1FFF:
            return
        if len(ip) < ihl + 8 or length < ihl + 8 or len(ip) < length:
            return
        sport, port, udp_len, _ = struct.unpack_from("!HHHH", ip, ihl)
        if udp_len < 8:
            return
        self.record("wire", ip[ihl + 8:min(length, ihl + udp_len)],
                    socket.inet_ntoa(ip[16:20]), socket.inet_ntoa(ip[12:16]), sport, port,
                    ip[1], ip[8], vlans, length, fragment & 0x3FFF)

    def datagram(self, sock):
        body, anc, flags, address = sock.recvmsg(65535, 256)
        if flags & socket.MSG_TRUNC:
            self.limited = True
            return
        group, tos, ttl = self.receivers[sock], 0, -1
        for level, kind, value in anc:
            if level == socket.IPPROTO_IP:
                if kind == socket.IP_TOS:
                    tos = value[0]
                elif kind == socket.IP_TTL:
                    ttl = struct.unpack("i", value)[0]
            elif level == socket.SOL_SOCKET and kind == 40:  # SO_RXQ_OVFL
                self.udp_drops = max(self.udp_drops, struct.unpack("I", value)[0])
        self.record("app", body, group, address[0], address[1], self.cfg["port"], tos, ttl, [], len(body) + 28)

    def membership(self, group, join):
        if not ipaddress.IPv4Address(group).is_multicast:
            raise ValueError("membership requires a multicast group")
        old = [s for s, g in self.receivers.items() if g == group]
        if join and not old:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.cfg["interface"].encode() + b"\0")
            sock.setsockopt(socket.IPPROTO_IP, 49, 0)  # IP_MULTICAST_ALL
            sock.setsockopt(socket.IPPROTO_IP, 13, 1)  # IP_RECVTOS
            sock.setsockopt(socket.IPPROTO_IP, 12, 1)  # IP_RECVTTL
            sock.setsockopt(socket.SOL_SOCKET, 40, 1)
            sock.bind((group, self.cfg["port"]))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(group) + socket.inet_aton(self.cfg["address"]))
            sock.setblocking(False)
            self.receivers[sock] = group
        elif not join:
            for sock in old:
                del self.receivers[sock]
                self.sockets.remove(sock)
                sock.close()
        return {"groups": sorted(self.receivers.values())}

    def send(self, req):
        if self.cfg["role"] != "sender":
            raise ValueError("send requested on receiver")
        count, pps, size = int(req["count"]), int(req["pps"]), int(req["payload"])
        groups = req["groups"]
        if not 1 <= count <= 10000 or not 1 <= pps <= 2000 or not HEADER.size <= size <= 8972:
            raise ValueError("send bounds exceeded")
        if not 1 <= len(groups) <= 2 or any(not ipaddress.IPv4Address(g).is_multicast for g in groups):
            raise ValueError("one or two multicast groups required")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.cfg["interface"].encode() + b"\0")
            sock.bind((self.cfg["source"], self.cfg["source_port"]))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.cfg["source"]))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(req["ttl"]))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(req["dscp"]) << 2 | int(req.get("ecn", 0)))
            sock.setsockopt(socket.IPPROTO_IP, 10, 2 if req.get("df", True) else 0)  # IP_MTU_DISCOVER
            start = time.monotonic()
            sent = collections.Counter()
            for seq in range(count):
                delay = start + seq / pps - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                body = HEADER.pack(MAGIC, self.token, int(req["phase"]), seq) + b"x" * (size - HEADER.size)
                for group in groups:
                    sock.sendto(body, (group, self.cfg["port"]))
                    sent[group] += 1
            return {"sent": sent, "elapsed": time.monotonic() - start, "payload": size}

    def command(self, req):
        op = req["op"]
        if op == "join" or op == "leave":
            return self.membership(req["group"], op == "join")
        if op == "send":
            return self.send(req)
        if op == "collect":
            phase = int(req["phase"])
            rows = []
            for (p, group, kind), row in self.records.items():
                if p == phase:
                    rows.append(dict(row, group=group, kind=kind, sequences=sorted(row["sequences"])))
            drops = 0
            if self.raw:
                _, drops = struct.unpack("II", self.raw.getsockopt(SOL_PACKET, 6, 8))
            return {"rows": rows, "capture_drops": drops, "udp_drops": self.udp_drops, "limited": self.limited}
        if op == "clear":
            self.records.clear()
            if self.raw:
                self.raw.getsockopt(SOL_PACKET, 6, 8)
            return {}
        raise ValueError("unknown operation")

    def close(self):
        for sock in self.sockets:
            sock.close()


def main():
    cfg = json.loads(sys.argv[1])
    probe = Probe(cfg)
    buffer = b""
    deadline = time.monotonic() + min(int(cfg.get("deadline", 600)), 3600)
    try:
        device = Path("/sys/class/net") / cfg["interface"]
        link = {name: (device / name).read_text().strip() for name in ("carrier", "mtu", "operstate")}
        print(json.dumps(dict(link, ready=True, interface=cfg["interface"], ifindex=probe.index)), flush=True)
        while time.monotonic() < deadline:
            ready, _, _ = select.select([0] + ([probe.raw] if probe.raw else []) + list(probe.receivers), [], [], 1)
            for fd in ready:
                if fd == 0:
                    part = os.read(0, 65536)
                    if not part:
                        return
                    buffer += part
                    if len(buffer) > 65536:
                        raise ValueError("request too large")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        req = json.loads(line)
                        try:
                            answer = dict(probe.command(req), id=req["id"])
                        except (OSError, ValueError, KeyError) as exc:
                            answer = {"id": req.get("id"), "error": str(exc)}
                        print(json.dumps(answer), flush=True)
                else:
                    try:
                        if fd == probe.raw:
                            probe.packet()
                        elif fd in probe.receivers:
                            probe.datagram(fd)
                    except BlockingIOError:
                        pass
    finally:
        probe.close()


if __name__ == "__main__":
    main()
