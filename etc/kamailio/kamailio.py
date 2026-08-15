## KamailioAgentEdge - KEMI (Python) routing
##
## All SIP routing for the agent edge lives here; kamailio.cfg only loads
## modules and hands over with `cfgengine "python"`. Kamailio calls, by name:
##
##   ksr_request_route(msg)      every incoming SIP request (main route)
##   ksr_reply_route(msg)        every incoming SIP reply (global onreply)
##   ksr_onsend_route(msg)       just before a message leaves the wire
##   ksr_onreply_manage(msg)     per-transaction onreply (set via t_on_reply)
##   ksr_failure_manage(msg)     per-transaction failure (set via t_on_failure)
##   ksr_xhttp_event(msg, ev)    HTTP on the TLS socket -> WebSocket handshake
##   ksr_websocket_event(msg,ev) websocket:closed etc.
##
## Call model:
##   - agent browser registers over WSS; we authenticate SIP digest against the
##     `subscriber` table (mirrored read-only from VICIdial by
##     tools/sync_subscribers.py) and store the contact in usrloc.
##   - dialer (av*/pd* Asterisk, source IP in the trs `address` allowlist) sends
##     INVITE to SIP/<ext>; we look up the agent's WSS contact and bridge media
##     through rtpengine (plain RTP <-> DTLS-SRTP, no transcoding).
##   - agent-originated INVITE (login conference dial-in, DTMF) is relayed to the
##     target box the client addressed, gated by the same trs allowlist.

import KSR

# --- constants that MUST match kamailio.cfg (#!define / #!trydef) ---
# KEMI cannot read cfg defines, so these are duplicated here by design.
AUTH_REALM = "avatar.tech"     # must equal AUTH_REALM in kamailio.cfg
TRS_GROUP = 1                  # must equal TRS_GROUP  (address table group)

# message flags (match FLT_/FLB_ in kamailio.cfg)
FLT_NATS = 5
FLB_NATB = 6
FLB_NATSIPPING = 7

# rtpengine media-transform flags, one per direction. rtpengine is stateful per
# call-id: we describe the transports on the INVITE (offer); the reply (answer)
# reuses the stored transforms. `direction=A direction=B` means received-on-A,
# send-to-B, where pub/int are the interfaces defined in rtpengine.conf.
#
# to Asterisk: strip everything WebRTC, hand it plain RTP/AVP.
RTPE_TO_ASTERISK = ("RTP/AVP ICE=remove rtcp-mux-demux SDES-off DTLS=off "
                    "replace-origin replace-session-connection trust-address "
                    "direction=pub direction=int")
# to agent: full WebRTC - DTLS-SRTP, ICE on our public interface, rtcp-mux.
RTPE_TO_AGENT = ("RTP/SAVPF ICE=force rtcp-mux-offer SDES-off DTLS=passive "
                 "replace-origin replace-session-connection trust-address "
                 "generate-mid direction=int direction=pub")


def mod_init():
    return kamailio()


class kamailio:

    def __init__(self):
        KSR.info("KamailioAgentEdge KEMI script loaded\n")

    def child_init(self, rank):
        return 0

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    def _from_agent(self, msg):
        """True when the request arrived over the WebSocket (agent leg)."""
        proto = KSR.pv.gete("$pr")
        return proto == "ws" or proto == "wss"

    def _from_dialer(self, msg):
        """True when the source IP is a known cluster telephony server."""
        return KSR.permissions.allow_source_address(TRS_GROUP) > 0

    # ------------------------------------------------------------------ #
    #  main request route                                                #
    # ------------------------------------------------------------------ #
    def ksr_request_route(self, msg):

        # -- sanity / max-forwards --
        if KSR.maxfwd.process_maxfwd(10) < 0:
            KSR.sl.sl_send_reply(483, "Too Many Hops")
            return 1

        if KSR.is_OPTIONS() and KSR.is_myself_ruri():
            KSR.sl.sl_send_reply(200, "Keepalive")
            return 1

        if KSR.sanity.sanity_check(17895, 7) < 0:
            KSR.info("malformed request from " + KSR.pv.gete("$si") + "\n")
            return 1

        # -- WebSocket source: record an alias so replies/inbound calls route
        #    back through the exact same ws connection --
        if self._from_agent(msg):
            KSR.setflag(FLT_NATS)
            KSR.nathelper.set_contact_alias()

        # -- in-dialog requests: follow the Route set --
        if KSR.siputils.has_totag() > 0:
            return self._route_withindlg(msg)

        # -- CANCEL --
        if KSR.is_CANCEL():
            if KSR.tm.t_check_trans() > 0:
                KSR.tm.t_relay()
            return 1

        # -- absorb retransmissions --
        if KSR.tmx.t_precheck_trans() > 0:
            KSR.tmx.t_check_trans()
            return 1
        if KSR.tm.t_check_trans() == 0:
            return 1

        # -- record-route dialog-forming requests so mid-dialog traffic and
        #    media teardown come back through us --
        if KSR.is_method_in("IS"):    # INVITE, SUBSCRIBE
            KSR.rr.record_route()

        if KSR.is_REGISTER():
            return self._route_register(msg)

        if KSR.is_INVITE():
            return self._route_invite(msg)

        # ACK for a 2xx we forwarded is handled statelessly by loose_route above;
        # anything else initial and non-INVITE is not expected on this edge.
        if KSR.is_ACK():
            return 1

        KSR.sl.sl_send_reply(405, "Method Not Allowed Here")
        return 1

    # ------------------------------------------------------------------ #
    #  REGISTER: digest auth then save to usrloc                         #
    # ------------------------------------------------------------------ #
    def _route_register(self, msg):
        if self._from_agent(msg):
            KSR.nathelper.fix_nated_register()

        # digest against the mirrored subscriber table, edge realm.
        if KSR.auth_db.auth_check(AUTH_REALM, "subscriber", 1) < 0:
            # no creds or bad creds -> (re)challenge with a fresh nonce
            KSR.auth.auth_challenge(AUTH_REALM, 1)
            return 1
        KSR.auth.consume_credentials()

        # Newest-wins: one station = one active binding. A browser reload or
        # reconnect registers a *new* contact (new ws port); with max_contacts=1
        # that would 503 ("too many contacts") until the stale one expires. Drop
        # any prior binding for this AoR first so the latest REGISTER always wins.
        KSR.registrar.unregister("location", KSR.pv.gete("$tu"))

        # KEMI: save() flags argument is an integer, not a string.
        if KSR.registrar.save("location", 0) < 0:
            KSR.sl.sl_reply_error()
        return 1

    # ------------------------------------------------------------------ #
    #  INVITE                                                            #
    # ------------------------------------------------------------------ #
    def _route_invite(self, msg):
        KSR.tm.t_on_reply("ksr_onreply_manage")
        KSR.tm.t_on_failure("ksr_failure_manage")

        if self._from_dialer(msg):
            # dialer -> agent: find the registered WSS contact, anchor media as
            # WebRTC toward the browser.
            if KSR.registrar.lookup("location") < 0:
                KSR.sl.sl_send_reply(404, "Agent Not Registered")
                return 1
            KSR.nathelper.handle_ruri_alias()
            KSR.rtpengine.rtpengine_offer(RTPE_TO_AGENT)
            self._relay(msg)
            return 1

        if self._from_agent(msg):
            # agent -> box: the client addressed its own cluster box (a
            # conference/feature extension). Only allow trs destinations so the
            # edge can't be used to dial arbitrary hosts.
            if self._ruri_host_in_trs(msg) <= 0:
                KSR.sl.sl_send_reply(403, "Destination Not Allowed")
                return 1
            KSR.rtpengine.rtpengine_offer(RTPE_TO_ASTERISK)
            self._relay(msg)
            return 1

        # neither a registered agent nor a known dialer
        KSR.sl.sl_send_reply(403, "Forbidden")
        return 1

    def _ruri_host_in_trs(self, msg):
        """Return the address-table group of the R-URI host, or -1 if none.

        A trs destination returns TRS_GROUP (>0); anything else <=0."""
        return KSR.permissions.allow_address_group(KSR.pv.gete("$rd"), 0)

    # ------------------------------------------------------------------ #
    #  in-dialog                                                         #
    # ------------------------------------------------------------------ #
    def _route_withindlg(self, msg):
        if KSR.rr.loose_route() > 0:
            if self._from_agent(msg):
                KSR.nathelper.handle_ruri_alias()
            if KSR.is_BYE():
                KSR.rtpengine.rtpengine_delete()
            elif KSR.is_INVITE():
                # re-INVITE (hold/resume): re-run the offer for this leg
                KSR.rtpengine.rtpengine_manage("")
            self._relay(msg)
            return 1

        # ACK to a 2xx we absorbed / no Route set
        if KSR.is_ACK():
            if KSR.tm.t_check_trans() > 0:
                KSR.tm.t_relay()
            return 1

        KSR.sl.sl_send_reply(404, "Not Here")
        return 1

    # ------------------------------------------------------------------ #
    #  relay                                                             #
    # ------------------------------------------------------------------ #
    def _relay(self, msg):
        if KSR.tm.t_relay() < 0:
            KSR.sl.sl_reply_error()
        return 1

    # ------------------------------------------------------------------ #
    #  per-transaction reply / failure                                   #
    # ------------------------------------------------------------------ #
    def ksr_onreply_manage(self, msg):
        # anchor media on provisional/final answers that carry SDP. (KSR.siputils
        # has no has_body in 6.1, so detect SDP via the Content-Type header.)
        ct = KSR.hdr.get("Content-Type") or ""
        if ct.find("application/sdp") >= 0:
            KSR.rtpengine.rtpengine_manage("")
        return 1

    def ksr_failure_manage(self, msg):
        if KSR.tm.t_is_canceled() > 0:
            return 1
        return 1

    # global onreply (all replies) - keep light
    def ksr_reply_route(self, msg):
        return 1

    def ksr_onsend_route(self, msg):
        return 1

    # ------------------------------------------------------------------ #
    #  WebSocket handshake + events                                      #
    # ------------------------------------------------------------------ #
    def ksr_xhttp_event(self, msg, evname):
        # The only HTTP we accept on the TLS socket is the SIP-over-WS upgrade.
        if KSR.pv.gete("$hdr(Upgrade)").lower().find("websocket") >= 0 \
                and KSR.pv.gete("$hdr(Connection)").lower().find("upgrade") >= 0:
            if KSR.websocket.handle_handshake() >= 0:
                return 1
        KSR.xhttp.xhttp_reply(404, "Not Found", "text/plain",
                              "KamailioAgentEdge: WebSocket only\n")
        return 1

    def ksr_websocket_event(self, msg, evname):
        # usrloc expiry cleans up the AoR; just trace connection churn.
        KSR.dbg("websocket event: " + evname + "\n")
        return 1
