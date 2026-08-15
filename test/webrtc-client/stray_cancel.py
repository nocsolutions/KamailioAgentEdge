#!/usr/bin/env python3
"""Send a CANCEL for a Call-ID the edge has never seen.

Exercises the CANCEL branch that finds no matching transaction - the path that
now consults is_known_dlg() before deciding whether it is safe to release media.
Expected: a 481 reply, no python exception in the kamailio log.

  stray_cancel.py <edge-ip> [port]
"""
import random
import socket
import sys

host = sys.argv[1] if len(sys.argv) > 1 else "10.4.100.147"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5060

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(4)
s.connect((host, port))
local_ip = s.getsockname()[0]
local_port = s.getsockname()[1]

callid = "stray%x" % random.getrandbits(48)
branch = "z9hG4bK%x" % random.getrandbits(32)
ftag = "%x" % random.getrandbits(32)

msg = (
    f"CANCEL sip:21382@{host}:{port} SIP/2.0\r\n"
    f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r\n"
    "Max-Forwards: 70\r\n"
    f"From: <sip:tester@{local_ip}>;tag={ftag}\r\n"
    f"To: <sip:21382@{host}>\r\n"
    f"Call-ID: {callid}\r\n"
    "CSeq: 102 CANCEL\r\n"
    "Content-Length: 0\r\n\r\n"
)
print(f"sending stray CANCEL call-id={callid} from {local_ip}:{local_port}")
s.send(msg.encode())
try:
    data, _ = s.recvfrom(65535)
    print("reply:", data.decode(errors="replace").split("\r\n")[0])
except socket.timeout:
    print("NO REPLY (would cause retransmissions)")
