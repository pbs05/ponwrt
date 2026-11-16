"""Small, bounded pcapng reader for the scenario probe's received TCP records."""
import ipaddress
import struct


def packets(data):
    offset, endian, interfaces = 0, None, []
    while offset < len(data):
        if len(data)-offset < 12:
            raise ValueError("Truncated pcapng header")
        if data[offset:offset+4] == b"\x0a\x0d\x0d\x0a":
            bom = data[offset+8:offset+12]
            if bom not in (b"\x4d\x3c\x2b\x1a", b"\x1a\x2b\x3c\x4d"):
                raise ValueError("Invalid pcapng byte order")
            endian = "<" if bom[0] == 0x4d else ">"
            interfaces = []
        if endian is None:
            raise ValueError("Missing pcapng section")
        kind, size = struct.unpack_from(endian+"II", data, offset)
        if size < 12 or size % 4 or offset+size > len(data) or size > 32*1024*1024:
            raise ValueError("Invalid pcapng block size")
        if struct.unpack_from(endian+"I", data, offset+size-4)[0] != size:
            raise ValueError("Invalid pcapng trailer")
        body = data[offset+8:offset+size-4]
        if kind == 1:
            if len(body) < 8:
                raise ValueError("Invalid pcapng interface")
            interfaces.append(struct.unpack_from(endian+"H", body)[0])
        elif kind == 6:
            if len(body) < 20:
                raise ValueError("Invalid pcapng packet")
            interface, _, _, captured, _ = struct.unpack_from(endian+"IIIII", body)
            if interface >= len(interfaces) or captured > len(body)-20:
                raise ValueError("Invalid packet interface/length")
            yield interfaces[interface], body[20:20+captured]
        offset += size


def tcp_observations(data, cases, nonce, win_ip):
    index = {c["Id"]: c for c in cases if c["Protocol"] == "tcp"}
    win = ipaddress.IPv4Address(win_ip).packed
    rows, seen = {}, set()
    for link, frame in packets(data):
        if link == 1:
            if len(frame) < 14:
                continue
            ether, off = int.from_bytes(frame[12:14], "big"), 14
            while ether in (0x8100, 0x88a8) and len(frame) >= off+4:
                ether, off = int.from_bytes(frame[off+2:off+4], "big"), off+4
            if ether != 0x800:
                continue
            ip = frame[off:]
        elif link == 101:
            ip = frame
        else:
            raise ValueError(f"Unsupported pcapng link type {link}")
        if len(ip) < 40 or ip[0] >> 4 != 4 or ip[9] != 6 or ip[16:20] != win:
            continue
        ihl = (ip[0] & 15)*4
        if ihl < 20 or len(ip) < ihl+20 or int.from_bytes(ip[6:8], "big") & 0x3fff:
            continue
        tcp = ip[ihl:]
        thl = (tcp[12] >> 4)*4
        payload = tcp[thl:]
        if thl < 20 or len(payload) < 24 or payload[:4] != b"HGQS" or int.from_bytes(payload[16:24], "big") != nonce:
            continue
        case_id, phase = payload[6], payload[4]
        c = index.get(case_id)
        if not c or phase > 1 or ip[12:16] != ipaddress.IPv4Address(c["Address"]).packed:
            continue
        if int.from_bytes(tcp[0:2], "big") != c["Port"] or int.from_bytes(tcp[2:4], "big") != c["SourcePort"]:
            continue
        dscp = str(ip[1] >> 2)
        # Dedupe repeated NIC indications and retransmitted records. A different
        # DSCP on a repeated record is still retained as mismatch evidence.
        identity = case_id, phase, payload[8:16], dscp
        if identity in seen:
            continue
        seen.add(identity)
        row = rows.setdefault((case_id, phase), dict(Case=case_id, Phase=phase, Packets=0, DSCP={}))
        row["Packets"] += 1
        row["DSCP"][dscp] = row["DSCP"].get(dscp, 0)+1
    return list(rows.values())
