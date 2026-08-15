# The Asterisk-side change (per cluster)

Today each webphone registers directly to its cluster's Asterisk over WSS. The
peer VICIdial generates (`/etc/asterisk/sip-vicidial.conf`, cluster-wide since
`phones.server_ip='0.0.0.0'`) looks like:

```
[16268]
username=16268
secret=NIwvV8nqWP
host=dynamic
transport=wss
encryption=yes
avpf=yes
dtlsenable=yes
icesupport=yes
...
allow=gsm
allow=opus
```

To move an agent onto the edge, that station peer becomes a **static peer
pointing at the edge**, with every WebRTC/DTLS option **explicitly negated** (the
edge now terminates them; the Asterisk leg is plain RTP):

```
[16268]
type=friend
host=<edge-int-ip>        ; 10.4.100.147 (edge, Asterisk-facing socket)
port=5060
qualify=yes
context=default
disallow=all
allow=ulaw
allow=opus
directmedia=no
nat=force_rport,comedia
transport=udp
encryption=no
avpf=no
force_avp=no
icesupport=no
dtlsenable=no
rtcp_mux=no
```

> **Negate, do not omit.** chan_sip peer settings are sticky across both
> `sip reload` and `module reload chan_sip.so`. Verified on av994 (2026-08-15):
> after removing `dtlsenable=yes` from the template — with zero `dtlsenable`
> lines left in `sip-vicidial.conf` and `sip show peer` reporting
> `Encryption: No` — Asterisk *still* offered
> `m=audio <port> UDP/TLS/RTP/SAVP` with an `a=fingerprint`. The edge answered
> plain `RTP/AVP`, the transports did not match, and the call had no audio.
> Only a full Asterisk restart clears the stale value, which a 650-box cutover
> cannot do — the explicit `no` values are what make a plain reload enough.
> Canonical SQL: `db/webphone-template-edge.sql`.

- VICIdial originates `SIP/16268` → Asterisk sends the INVITE to the edge →
  the edge looks up the agent's WSS registration and bridges media via rtpengine.
- The webphone's WSS URL (a VICIdial ViciPhone setting, **not** login.avatar.tech)
  is repointed from the cluster Asterisk to `wss://<edge>:4443/`.
- Rollback = revert the peer template and the WSS URL; the next agent login
  registers directly again.

This is a change to how VICIdial *generates* the peer, and it needs **no code
change**: the peer body comes from `vicidial_conf_templates.template_contents`
(row `template_id='webphone'`, literal `\n` separators). Rewrite that row and set
`servers.rebuild_conf_files='Y'`; VICIdial's cron regenerates
`sip-vicidial.conf` and reloads chan_sip within ~1 minute. See
`db/webphone-template-edge.sql`.

The browser side is the matching half: DialerWeb branch `agentedge-wss` sends the
webphone's WSS to `AGENT_EDGE_WSS` (`libs/config.php`) instead of
`wss://<its TR>:8089/ws`. Both halves are cluster-wide, which is deliberate — a
cluster goes to the edge wholesale, so there is no per-station fork.

Because the peer is generated cluster-wide, every telephony server in the cluster
gets the edge-pointing peer at once, and a call originated on any of them reaches
the agent through the edge.
