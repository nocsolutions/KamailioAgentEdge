#!/usr/bin/env python3
"""Group SIP messages in a pcap by Call-ID and print each dialog in order.

Hand-rolled stand-in for `pcap-sip inspect` until that builds. Reads a pcap/pcapng
of UDP SIP, correlates by Call-ID, and prints one line per message so a single
call can be followed without other concurrent dialogs interleaving.

  sipflow.py <file.pcap> [call-id-substring]
"""
import struct
import sys

PCAP_MAGICS = {0xa1b2c3d4: ('<', 1), 0xd4c3b2a1: ('>', 1),
               0xa1b23c4d: ('<', 1000), 0x4d3cb2a1: ('>', 1000)}


def packets(path):
    """Yield (timestamp, payload_bytes) for each captured packet."""
    with open(path, 'rb') as fh:
        data = fh.read()
    if len(data) < 24:
        return
    magic = struct.unpack('<I', data[:4])[0]
    if magic not in PCAP_MAGICS:
        magic = struct.unpack('>I', data[:4])[0]
    if magic not in PCAP_MAGICS:
        sys.exit('not a classic pcap (pcapng unsupported here)')
    endian, tsdiv = PCAP_MAGICS[magic]
    linktype = struct.unpack(endian + 'I', data[20:24])[0]
    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_usec, caplen, _ = struct.unpack(endian + 'IIII', data[off:off + 16])
        off += 16
        pkt = data[off:off + caplen]
        off += caplen
        # strip link layer: 1=Ethernet(14), 113=Linux SLL(16), 276=SLL2(20)
        hdr = {1: 14, 113: 16, 276: 20}.get(linktype)
        if hdr is None or len(pkt) < hdr + 20:
            continue
        ip = pkt[hdr:]
        if (ip[0] >> 4) != 4:
            continue
        ihl = (ip[0] & 0x0f) * 4
        if ip[9] != 17:            # UDP only
            continue
        udp = ip[ihl:]
        if len(udp) < 8:
            continue
        src = '.'.join(str(b) for b in ip[12:16])
        dst = '.'.join(str(b) for b in ip[16:20])
        sport, dport = struct.unpack('>HH', udp[0:4])
        yield (ts_sec + ts_usec / (1e6 if tsdiv == 1 else 1e9),
               f'{src}:{sport}', f'{dst}:{dport}', udp[8:])


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else None

    calls = {}
    seen = set()
    for ts, src, dst, payload in packets(path):
        try:
            text = payload.decode('utf-8', 'replace')
        except Exception:
            continue
        if not text.startswith(('INVITE', 'ACK', 'BYE', 'CANCEL', 'OPTIONS',
                                'REGISTER', 'INFO', 'UPDATE', 'PRACK', 'SIP/2.0')):
            continue
        lines = text.split('\r\n')
        first = lines[0]
        cid = ''
        cseq = ''
        for ln in lines[1:]:
            low = ln.lower()
            if low.startswith('call-id:'):
                cid = ln.split(':', 1)[1].strip()
            elif low.startswith('cseq:'):
                cseq = ln.split(':', 1)[1].strip()
            if cid and cseq:
                break
        if not cid:
            continue
        # de-dup: -i any often captures the same datagram twice
        key = (cid, first, cseq, src, dst, round(ts, 3))
        if key in seen:
            continue
        seen.add(key)
        calls.setdefault(cid, []).append((ts, src, dst, first, cseq))

    for cid, msgs in calls.items():
        if want and want not in cid:
            continue
        print(f'\n=== Call-ID {cid}  ({len(msgs)} messages) ===')
        t0 = msgs[0][0]
        for ts, src, dst, first, cseq in msgs:
            print(f'  +{ts - t0:7.3f}s  {src:>22} -> {dst:<22}  {first}   [{cseq}]')


if __name__ == '__main__':
    main()
