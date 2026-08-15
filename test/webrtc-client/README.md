# Headless WebRTC test client

A dependency-light harness to exercise the agent edge without a browser. Used to
validate the WSS registrar path and the DTLS-SRTP ↔ plain-RTP media bridge.

- **register.js** — SIP-over-WebSocket (RFC 7118) REGISTER with digest+qop. Proves
  the `ksr_xhttp_event` handshake and the WS registrar path.
- **agent.js** — a WebRTC agent (werift): registers over WSS, auto-answers an
  INVITE with a DTLS-SRTP/PCMU answer, sends a PCMU stream and counts RTP.
- **caller.py** — a plain-RTP PCMU "dialer" (no deps): from a trs-allowed source
  it INVITEs an agent ext, ACKs, streams RTP and counts what returns.

## Run (on the edge box, so ICE candidates are the edge's own IPs)

```bash
npm install ws werift          # once
# 1. register only:
node register.js wss://216.66.20.147:4443 <ext> <phone_pass> avatar.tech
# 2. full media call — start the agent, then the caller:
node agent.js  wss://216.66.20.147:4443 <ext> <phone_pass> avatar.tech &
python3 caller.py 10.4.100.147:5060 <ext> avatar.tech 12
```

The caller's source IP must be in the `address` (trs) table; when running on the
edge itself, add the edge IP for the test:
`INSERT INTO address (grp,ip_addr,mask,port) VALUES (1,'10.4.100.147',32,0);`
then `kamcmd permissions.addressReload`.

## Verifying media at the edge

rtpengine relays both directions; confirm with a capture on the edge:

```bash
sudo tcpdump -i any -n 'udp portrange 30000-40000' -w /tmp/media.pcap
sudo tcpdump -nr /tmp/media.pcap | awk '{print $3" -> "$5}' | sort | uniq -c | sort -rn
```

You should see two high-count RTP flows (one toward the agent's media port on the
public interface, one toward the caller's port). The pcap also feeds
`pcap-sip inspect` for a SIP ladder + RTP-stream summary.

## Note on werift receive counting

`agent.js` reliably sends and the edge delivers inbound SRTP to its port (visible
in the capture), but werift's `onReceiveRtp` counter may read 0 — a client-side
quirk, not an edge issue. Trust the pcap for the inbound direction.
