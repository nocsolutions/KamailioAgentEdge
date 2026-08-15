# KamailioAgentEdge

WebRTC **agent edge / SBC** for the CGSwitch fleet. It moves agent WebRTC
termination and SIP authentication off the ~650 Asterisk dialers and onto one
Kamailio + rtpengine pair, so the Asterisk boxes can drop their public
interfaces entirely.

Routing is written in **Python (KEMI)** — `etc/kamailio/kamailio.cfg` only loads
modules and hands over with `cfgengine "python"`; all logic lives in
`etc/kamailio/kamailio.py`.

```
 agent browser (ViciPhone / JsSIP)
   │  WSS  — SIP over WebSocket over TLS ; DTLS-SRTP media
   ▼
 KamailioAgentEdge ── registrar (usrloc) + digest auth (auth_db) + rtpengine
   │  UDP  — plain SIP ; plain RTP (ulaw/opus, no transcoding)
   ▼
 av*/pd* Asterisk — chan_sip webphone peers rewritten to host=<edge>
```

Why the edge is the registrar: chan_sip on the Asterisk‑11 fleet has no Path
support, so Asterisk can never route back to a `ws:` contact through a proxy.
The edge therefore holds the registration and authenticates it.

Auth without touching the login service: agents keep the **same SIP digest**
(`ext` / `phone_pass`) they use today. Those credentials are mirrored
**read‑only** from each cluster's VICIdial `phones` table into the local
`subscriber` table (`tools/sync_subscribers.py`). `login.avatar.tech` is never
modified — it keeps authenticating agents to the portal and redirecting them to
the VICIdial screen exactly as before.

## Layout

```
etc/kamailio/kamailio.cfg            KEMI bootstrap (modules, listen, modparams)
etc/kamailio/kamailio.py             all routing logic (KEMI / Python)
etc/kamailio/kamailio-local.cfg.example  per-host IPs + secrets (copy, git-ignored)
etc/kamailio/tls.cfg                 WSS TLS settings
etc/kamailio/kamctlrc                kamdbctl config (passwords come from .env)
etc/default/kamailio                 service defaults
etc/rtpengine/rtpengine.conf         media anchor: pub + int interfaces
tools/sync_subscribers.py            VICIdial phones  -> subscriber (read-only mirror)
tools/refresh_trs.py                 fleet trs IPs    -> address allowlist
tools/.env.example                   tool config (copy to .env, git-ignored)
db/webphone-template-edge.sql        per-cluster cutover: webphone peer -> edge
test/webrtc-client/                  headless WebRTC harness (agent, caller, register)
deploy/setup_db.sh                   create the kamailio DB (kamdbctl)
deploy/gen_selfsigned_cert.sh        lab TLS cert
deploy/deploy.sh                     push config to a host, validate, reload
docs/                                DB setup, the Asterisk peer change, testing
```

## Test / target host

`10.4.100.147` (hostname `vici-rtproxy`, **SSH port 9999**), Debian 13:
- kamailio **6.1.3** + `app_python3` (KEMI), tls, websocket, auth_db, usrloc,
  registrar, permissions, htable, pike, rtpengine, nathelper
- rtpengine **mr26.0.1.9** (from `deb.kamailio.org/rtpengine-mr26.0` — matches
  the production border), kernel DKMS module loaded
- MariaDB 11.8 (local kamailio DB)
- interfaces: `ens19 = 216.66.20.147` (public, agent-facing) /
  `ens18 = 10.4.100.147` (internal, Asterisk-facing)

## Bring-up

```bash
# 1. tools + DB
cp tools/.env.example tools/.env        # set KAM_DB_PASS, VICI_DB_*, ONLY_CLUSTERS
sudo deploy/setup_db.sh                  # create kamailio DB (subscriber/location/address)

# 2. config + cert
cp etc/kamailio/kamailio-local.cfg.example etc/kamailio/kamailio-local.cfg   # set IPs, DBURL
EDGE_HOST=10.4.100.147 EDGE_PORT=9999 deploy/deploy.sh     # validates with kamailio -c
sudo deploy/gen_selfsigned_cert.sh                          # lab cert for :4443

# 3. data
tools/refresh_trs.py                     # load cluster telephony-server IPs
tools/sync_subscribers.py --clusters av996   # mirror one cluster's webphone creds

# 4. start
sudo systemctl restart rtpengine kamailio
kamcmd ul.dump                           # watch registrations
```

## Status

**Working end to end on av994**: a real ViciPhone agent registers over WSS,
lands in a MeetMe conference and has bidirectional audio through the edge —
16,985 rx / 16,996 tx RTP packets, 0% loss over a 5m40s call (Asterisk's own
`sip show channelstats`), with rtpengine reporting `DTLS-SRTP successfully
negotiated` and no DTLS errors.

Implemented: WSS handshake, digest REGISTER + usrloc (newest-wins), dialer→agent
and agent→box INVITE with rtpengine WebRTC↔plain-RTP bridging (no transcoding),
in-dialog ACK/BYE via ws alias, rtpengine session teardown on every failure path,
the read-only credential mirror, and the trs allowlist.

Cutover of a cluster is two config changes, no code: `db/webphone-template-edge.sql`
(peer → edge, WebRTC options explicitly negated) and `AGENT_EDGE_WSS` in the
DialerWeb branch `agentedge-wss`. See `docs/`.

Not yet done: agent-originated calls and hold/transfer re-INVITEs, autonomous
credential sync (needs a read-only DB grant per cluster), HA pair (DMQ usrloc
replication), TLS cert automation, and a load soak.
