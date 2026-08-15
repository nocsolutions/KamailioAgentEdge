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

- **Media / a full call** through rtpengine (needs a WebRTC client or a
  sipp+SDP scenario and a static Asterisk peer). The rtpengine offer/answer flags
  and the dialer↔agent / agent↔box INVITE routing are written but unexercised.
- **`sync_subscribers.py` against live cluster data** — network path to the
  av996 DB is open, but the read-only grant isn't created yet (see DB-SETUP.md).
- **WSS from a real browser** (DTLS-SRTP, ICE) — only UDP SIP has been driven so
  far; the WSS/TLS listener is up and the handshake path (`ksr_xhttp_event`) is
  in place but untested end to end.

## Next

1. Create `coregears_ro` on av996, run `sync_subscribers.py --clusters av996`,
   confirm the pilot cluster's webphones populate `subscriber`.
2. A sipp WSS scenario (register + call) for the media path, or point one real
   ViciPhone at `wss://10.4.100.147:4443/` with its station peer set to the edge.
3. Verify a dialer→agent call: originate `SIP/<ext>` from an av996 box to the
   edge, confirm rtpengine bridges DTLS-SRTP↔RTP and audio flows.
