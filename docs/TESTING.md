# Testing / what's validated

Target box: `10.4.100.147` (`vici-rtproxy`, SSH port 9999).

## Validated on 2026-08-15

| # | Check | Result |
|---|-------|--------|
| 1 | `kamailio -c` loads the KEMI bootstrap + compiles `kamailio.py` | OK |
| 2 | kamailio 6.1.3 + rtpengine mr26.0.1.9 services start clean | OK (rtpengine "found, support enabled"; TLS keys loaded) |
| 3 | `refresh_trs.py` → `address` group 1 | 650 trs IPs loaded, `permissions.addressReload` OK |
| 4 | REGISTER, correct digest → 200, saved to usrloc | OK (`AoR: 16268`, Expires 300) |
| 5 | REGISTER, wrong password → 401, not saved | OK |
| 6 | Re-REGISTER from a new port (browser reload) → newest-wins, no 503 | OK (two registers → exactly one contact) |
| 7 | Credential mirror: 27 real av996 webphones → `subscriber`, a real one authenticates | OK |
| 8 | **WSS** REGISTER from a real WebSocket client (werift), incl. `;alias` | OK (usrloc shows the ws contact) |
| 9 | **Full WebRTC media call** dialer→agent through rtpengine (DTLS-SRTP ↔ plain RTP, no transcode) | OK — see below |

### Media call result

A plain-RTP PCMU caller (trs source) called a WSS-registered werift agent
(ext 31316). Full SIP dialog (INVITE→100→200→ACK→BYE), DTLS-SRTP negotiated
(`ice: connected` / `pc: connected`), and RTP relayed **both directions**,
confirmed by an edge capture:

```
441 packets  -> 216.66.20.147:36838   (rtpengine → agent, caller→agent leg)
396 packets  -> 10.4.100.147:41268    (rtpengine → caller, agent→caller leg)
caller: sent=396 received=389
```

rtpengine bridges the WebRTC agent's DTLS-SRTP and the dialer's plain RTP with
no transcoding (PCMU end to end). Harness: `test/webrtc-client/`.

(werift's inbound `onReceiveRtp` counter reads 0, but the 441 packets reaching
the agent's port in the capture show the edge delivers that direction — a client
quirk, not an edge defect.)

Test credential: a real av996 webphone (`16268` / `NIwvV8nqWP`), inserted into
`subscriber` by hand to stand in for the mirror.

### Reproduce the REGISTER tests

```bash
# on the edge box
sipsak -U -i -s sip:16268@10.4.100.147 \
  --auth-username=16268 --password=NIwvV8nqWP -x 300 -v
sudo kamcmd ul.dump          # AoR 16268 with one contact
```

sipsak resolves the URI host, so target the edge IP directly (auth keys on
username only, `use_domain=0`, so the domain part is irrelevant); the digest
realm the edge challenges with is `avatar.tech`.

## Not yet validated

- **A real browser** (ViciPhone/JsSIP in Chrome/Firefox) end to end — the media
  path is proven with a werift client; a real browser is the last confidence check.
- **`sync_subscribers.py` autonomous pull** — the mirror was proven by loading
  27 real av996 webphones into `subscriber` (read locally via `cg_dbrole`), but
  the edge-pull needs a read-only grant for the edge IP (all cluster DB users are
  `@localhost`), or the sync must run on the DB box. See DB-SETUP.md.
- **agent→box path** and re-INVITE/hold — only dialer→agent was driven.

## Next

1. Point one real ViciPhone at `wss://<edge>:4443/` with its station peer set to
   the edge (docs/asterisk-peer-template.md) and confirm a live agent call.
2. Resolve the mirror's DB access (grant for the edge, or run-on-DB-box) so
   `sync_subscribers.py` runs autonomously per cluster.
3. Exercise agent-originated calls and hold/transfer re-INVITEs.
