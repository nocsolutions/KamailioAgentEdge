#!/usr/bin/env python3
"""Plain-RTP PCMU caller (the 'dialer' leg) for testing the agent edge.

Runs on the edge box (source IP in the trs allowlist). Sends INVITE for an agent
ext, on 200 ACKs, streams PCMU RTP to the address rtpengine gave, and counts
what comes back. No auth (dialer is IP-trusted).

  caller.py <edge-sip-ip:port> <ext> [realm] [seconds]
"""
import socket, struct, sys, time, random, re

edge = sys.argv[1]; ext = sys.argv[2]
realm = sys.argv[3] if len(sys.argv) > 3 else "avatar.tech"
secs = int(sys.argv[4]) if len(sys.argv) > 4 else 6
ehost, eport = edge.split(":"); eport = int(eport)

sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sip.bind(("0.0.0.0", 0)); sip.settimeout(5)
local_ip = socket.gethostbyname(socket.gethostname())
# use the source IP actually routed to the edge
s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s2.connect((ehost, eport))
local_ip = s2.getsockname()[0]; s2.close()

rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp.bind(("0.0.0.0", 0)); rtp.settimeout(0.05)
rtp_port = rtp.getsockname()[1]

callid = "%x" % random.getrandbits(48)
ftag = "%x" % random.getrandbits(32)
branch = "z9hG4bK%x" % random.getrandbits(32)
sipport = sip.getsockname()[1]

sdp = ("v=0\r\n"
       f"o=- 1 1 IN IP4 {local_ip}\r\n"
       "s=coregears-caller\r\n"
       f"c=IN IP4 {local_ip}\r\n"
       "t=0 0\r\n"
       f"m=audio {rtp_port} RTP/AVP 0 101\r\n"
       "a=rtpmap:0 PCMU/8000\r\n"
       "a=rtpmap:101 telephone-event/8000\r\n"
       "a=sendrecv\r\n")

def invite(extra=""):
    return (f"INVITE sip:{ext}@{realm} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{sipport};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:caller@{local_ip}>;tag={ftag}\r\n"
            f"To: <sip:{ext}@{realm}>\r\n"
            f"Call-ID: {callid}\r\n"
            "CSeq: 1 INVITE\r\n"
            f"Contact: <sip:caller@{local_ip}:{sipport}>\r\n"
            "User-Agent: coregears-caller\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n{sdp}")

print(f"caller {local_ip}:{sipport} rtp:{rtp_port} -> {edge} for ext {ext}")
sip.sendto(invite().encode(), (ehost, eport))

remote_rtp = None; to_tag = None
deadline = time.time() + 8
while time.time() < deadline:
    try:
        data, _ = sip.recvfrom(65535)
    except socket.timeout:
        break
    m = data.decode(errors="replace")
    line = m.split("\r\n", 1)[0]
    print("<<", line)
    if m.startswith("SIP/2.0 200"):
        mt = re.search(r"To:.*?(;tag=[^\r\n;]+)", m)
        to_tag = mt.group(1) if mt else ""
        cm = re.search(r"c=IN IP4 ([0-9.]+)", m.split("\r\n\r\n",1)[1])
        pm = re.search(r"m=audio (\d+)", m.split("\r\n\r\n",1)[1])
        remote_rtp = (cm.group(1), int(pm.group(1)))
        # ACK
        ack = (f"ACK sip:{ext}@{realm} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {local_ip}:{sipport};branch={branch}a;rport\r\n"
               "Max-Forwards: 70\r\n"
               f"From: <sip:caller@{local_ip}>;tag={ftag}\r\n"
               f"To: <sip:{ext}@{realm}>{to_tag}\r\n"
               f"Call-ID: {callid}\r\nCSeq: 1 ACK\r\nContent-Length: 0\r\n\r\n")
        sip.sendto(ack.encode(), (ehost, eport))
        print(f">> ACK; media -> {remote_rtp}")
        break
    if m.startswith("SIP/2.0 4") or m.startswith("SIP/2.0 5") or m.startswith("SIP/2.0 6"):
        print("call rejected"); sys.exit(1)

if not remote_rtp:
    print("no 200 OK / no media address"); sys.exit(1)

# stream PCMU (silence 0xFF), count received
seq = random.getrandbits(16) & 0xffff
ts = 0; ssrc = random.getrandbits(32)
sent = recv = 0
end = time.time() + secs
next_tx = time.time()
while time.time() < end:
    now = time.time()
    if now >= next_tx:
        hdr = struct.pack("!BBHII", 0x80, 0x00, seq & 0xffff, ts & 0xffffffff, ssrc)
        rtp.sendto(hdr + b"\xff" * 160, remote_rtp)
        sent += 1; seq += 1; ts += 160; next_tx += 0.02
    try:
        d, _ = rtp.recvfrom(2048)
        if len(d) >= 12:
            recv += 1
    except socket.timeout:
        pass

# BYE
bye = (f"BYE sip:{ext}@{realm} SIP/2.0\r\n"
       f"Via: SIP/2.0/UDP {local_ip}:{sipport};branch={branch}b;rport\r\n"
       "Max-Forwards: 70\r\n"
       f"From: <sip:caller@{local_ip}>;tag={ftag}\r\n"
       f"To: <sip:{ext}@{realm}>{to_tag}\r\n"
       f"Call-ID: {callid}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n")
sip.sendto(bye.encode(), (ehost, eport))
print(f"MEDIA RESULT (caller): sent={sent} received={recv}")
