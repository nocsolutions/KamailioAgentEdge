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
pointing at the edge**, with all the WebRTC/DTLS bits stripped (the edge now
terminates them; the Asterisk leg is plain RTP):

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
; no transport=wss, no dtls*, no avpf, no icesupport, no encryption
```

- VICIdial originates `SIP/16268` → Asterisk sends the INVITE to the edge →
  the edge looks up the agent's WSS registration and bridges media via rtpengine.
- The webphone's WSS URL (a VICIdial ViciPhone setting, **not** login.avatar.tech)
  is repointed from the cluster Asterisk to `wss://<edge>:4443/`.
- Rollback = revert the peer template and the WSS URL; the next agent login
  registers directly again.

This is a change to how VICIdial *generates* the peer (the phones/template
layer), applied per cluster. Getting VICIdial to emit the static-host template
for webphones is the integration point still to be worked out with the VICIdial
config owner; until then the edge can be exercised with a manually-written peer
on one box, or with sipp/sipsak as in docs/TESTING.md.

Because the peer is generated cluster-wide, every telephony server in the cluster
gets the edge-pointing peer at once, and a call originated on any of them reaches
the agent through the edge.
