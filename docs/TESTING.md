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

### Real browser agent through the edge (2026-08-15)

A live agent logged into av994 (ViciPhone/SIP.js, station 21381), placed into
MeetMe conference 8600051, with media anchored on the edge:

```
SIP/21381-0000001b  Up  8600051,q
sip show channelstats:
  Peer          Duration   Recv: Pack   Lost        Send: Pack   Lost     Jitter
  10.4.100.147  00:05:40   0000016985   0 (0.00%)   0000016996   0 (0.00%) 0.0005
```

Bidirectional audio, zero loss, over a 5m40s call — Asterisk's own counters.
rtpengine logged `DTLS-SRTP successfully negotiated` with zero DTLS errors.

Four root causes had to be fixed to get here:

1. **ACK dropped at the edge.** Asterisk ACKed to the browser's ws contact
   `sip:…@<rand>.invalid`; kamailio DNS-resolved it and dropped the packet
   (`bad host name … dropping packet`). The 2xx was never ACKed, the Asterisk
   channel stayed `Down`, no app ran, and the call died after 60s for "lack of
   RTP activity". Fix: `handle_ruri_alias()` on in-dialog requests +
   `set_contact_alias()` on replies from the agent.
2. **Asterisk still offered DTLS** — chan_sip sticky settings; see
   `asterisk-peer-template.md`.
3. **The reply was processed as an offer.** `rtpengine_manage()` only emits
   `OP_ANSWER` on a reply when `FL_SDP_BODY` is set on the request, and that
   flag is set only by `rtpengine_manage()` itself handling the INVITE
   (kamailio 6.1 `rtpengine.c:5579/5583/5599`). Since the INVITE uses
   `rtpengine_offer()`, every SDP reply became a second `OP_OFFER` with swapped
   tags, propagating the browser's DTLS/SAVPF onto the Asterisk leg (the
   ClientHello storms). Fix: `rtpengine_answer()` in the reply route.
4. **Relay-only ICE.** DialerWeb forced `iceTransportPolicy:"relay"` whenever a
   `CG_turn_servers` row exists. A/B measured with an identical client against
   `turn2.noc.solutions`: `relay` → ICE stuck in `checking`, 0 RTP packets,
   rtpengine `SRTP output wanted, but no crypto suite was negotiated`; `all` →
   ICE connected, DTLS-SRTP, ~960 packets per 20s. Fixed in DialerWeb branch
   `agentedge-wss` (`agent/turn_servers.php`); TURN stays as fallback.

### Synthetic media call result

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

- **agent→box path** (browser dialing its own cluster box) and hold/transfer
  re-INVITEs — only dialer→agent has been driven end to end. Note the
  `rtpengine_answer()` fix is what makes the agent-originated direction viable at
  all: under the old `OP_OFFER` misclassification the 200 OK to Chrome would have
  carried `a=setup:actpass`, which libwebrtc rejects (RFC 5763 §5).
- **`sync_subscribers.py` autonomous pull** — the mirror was proven by loading
  real av996/av994 webphones into `subscriber` (read locally via `cg_dbrole`), but
  the edge-pull needs a read-only grant for the edge IP (all cluster DB users are
  `@localhost`), or the sync must run on the DB box. See DB-SETUP.md.
- **Load / capacity** — the session-leak fixes (CANCEL, failure route, unrouted
  BYE, failed relay) are in but have not been driven at dialer volumes. Watch
  `rtpengine-ctl list numsessions` against a dialer's abandon rate: with
  `silent-timeout=3600` and a 10k-port range, leaked sessions exhaust the range
  and then *every* call loses audio.

## Dialog tracking (what it does and does not fix)

`dlg_manage()` runs on every initial INVITE and the dialog module reports
lifecycle events to `ksr_dialog_event`. Verified on av994: dialogs track
sessions exactly (`dlg.stats_active` 0 → 1 → 0 alongside rtpengine 0 → 1 → 0),
and the full teardown suite still passes.

It buys two things:

1. **A safe CANCEL discriminator.** A CANCEL that matches no transaction used to
   be ambiguous - an orphaned offer (safe to release) looks identical to an
   established call whose transaction has long since been freed (releasing would
   cut live audio), so we released nothing. `is_known_dlg()` now tells them
   apart: no dialog → release the media, dialog present → keep it and log.
   Exercised with `test/webrtc-client/stray_cancel.py`, which sends a CANCEL for
   an unknown Call-ID and expects a clean `481` with no exception.
2. **A single guaranteed teardown hook** (`dialog:end` / `dialog:failed`) that
   overlaps the per-message deletes. `rtpengine_delete` is idempotent, so the
   fast paths stay where they are.

It does **not** fix the vanished-browser case, and it is worth being precise
about why: when the dialog module fires the event from its own timer rather than
from a request it passes a **faked** message (`dlg_run_event_route` →
`faked_msg_next()`), and rtpengine only reads the Call-Id for real SIP
(`rtpengine.c:3648`). So the delete is a no-op on a pure dialog timeout. That
case is still covered by rtpengine's reaper (`offer-timeout` / `silent-timeout`,
180s) and, in practice, by the far end hanging up on RTP inactivity first.

## Known operational gotchas

- **A kamailio restart strands every agent.** SIP.js is configured
  `reconnectionAttempts: 0`, so browsers never reconnect their WebSocket; agents
  must reload the page. Worth changing in the webphone config before a real
  cutover.
- The edge needs `edge.avatar.tech` to resolve for its own tooling
  (`/etc/hosts`), and browsers need it to match the `*.avatar.tech` cert.

## Next

1. Exercise agent-originated calls and hold/transfer re-INVITEs.
2. Resolve the mirror's DB access (grant for the edge, or run-on-DB-box) so
   `sync_subscribers.py` runs autonomously per cluster.
3. Soak a cluster at real agent volume and watch rtpengine session count.
