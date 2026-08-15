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

# rtpengine media-transform flags. The flags of a message describe the SDP
# rtpengine is about to PRODUCE, i.e. what the RECEIVER of that SDP will see;
# the incoming body describes the sender. `direction=A direction=B` is
# received-on-A / send-to-B and is OFFER-ONLY - on an answer the roles are
# reversed and rtpengine would rebind the media to the wrong interface mid-call.
#
# The reply route MUST call rtpengine_answer(), never rtpengine_manage(): on a
# reply, rtpengine_manage() only emits OP_ANSWER when the internal FL_SDP_BODY
# flag was stamped on the transaction's request, and that flag is set *only* by
# rtpengine_manage() itself handling the INVITE (rtpengine.c:5579/5583/5599).
# Because we call rtpengine_offer() on the INVITE, rtpengine_manage() on the
# reply silently issued a second OP_OFFER with the from/to tags swapped - which
# is what made rtpengine inherit the browser's DTLS/SAVPF onto the Asterisk leg
# and fire ClientHellos at av994 until "DTLS error: read timeout expired".
#
# replace-session-connection is deliberately absent: mr26 accepts it only for
# compatibility and logs "not supported anymore" on every offer and answer.

# OFFER toward Asterisk (agent-originated INVITE: browser -> Asterisk)
RTPE_TO_ASTERISK = ("RTP/AVP ICE=remove rtcp-mux-demux SDES-off DTLS=off "
                    "DTLS-reverse=passive replace-origin trust-address "
                    "direction=pub direction=int")
# OFFER toward agent (dialer-originated INVITE: Asterisk -> browser)
RTPE_TO_AGENT = ("UDP/TLS/RTP/SAVPF ICE=force rtcp-mux-require SDES-off "
                 "DTLS=passive generate-mid replace-origin trust-address "
                 "direction=int direction=pub")
# ANSWER toward Asterisk (the reply came FROM the browser)
RTPE_ANSWER_TO_ASTERISK = ("RTP/AVP ICE=remove rtcp-mux-demux SDES-off "
                           "DTLS=off replace-origin trust-address")
# ANSWER toward agent (the reply came FROM Asterisk)
RTPE_ANSWER_TO_AGENT = ("UDP/TLS/RTP/SAVPF ICE=force SDES-off DTLS=passive "
                        "replace-origin trust-address")


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

        # -- CANCEL -- release the media session too; the INVITE's failure route
        # also deletes on the resulting 487, and rtpengine_delete is idempotent.
        if KSR.is_CANCEL():
            if KSR.tm.t_check_trans() > 0:
                KSR.rtpengine.rtpengine_delete("")
                KSR.tm.t_relay()
            else:
                # No matching INVITE transaction - the transaction completed and
                # was freed. Two very different situations look identical here:
                # an ESTABLISHED call (transactions are short-lived, dialogs are
                # not) or an orphaned offer whose call never came up. Deleting
                # blindly would rip the audio out of a live call, which is why
                # this used to just answer and leave the media behind.
                # dlg_manage() gives us the discriminator: is_known_dlg() finds a
                # dialog only for a call that actually exists.
                if KSR.dialog.is_known_dlg() > 0:
                    KSR.info("late CANCEL for established dialog, media kept: "
                             + KSR.pv.gete("$ci") + "\n")
                else:
                    KSR.rtpengine.rtpengine_delete("")
                # Answer it either way: an unanswered CANCEL is retransmitted
                # every 500ms (observed 7x on av994).
                KSR.sl.sl_send_reply(481, "Call/Transaction Does Not Exist")
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
        # Track the dialog. Two things depend on it: ksr_dialog_event gets a
        # single guaranteed teardown hook, and the CANCEL branch can ask
        # is_known_dlg() whether a call is actually established before deciding
        # it is safe to tear the media down. Must come after record_route(),
        # which request_route already did for INVITE.
        KSR.dialog.dlg_manage()

        if self._from_dialer(msg):
            # dialer -> agent: find the registered WSS contact, anchor media as
            # WebRTC toward the browser.
            if KSR.registrar.lookup("location") < 0:
                KSR.sl.sl_send_reply(404, "Agent Not Registered")
                return 1
            KSR.nathelper.handle_ruri_alias()
            # A failed offer (ng timeout / node disabled - expected, we run with
            # rtpengine_allow_op=1) would otherwise relay Asterisk's untouched
            # plain-RTP SDP to the browser, which rejects it: the call would set
            # up 200/ACK with silent audio and nothing in the SIP trace.
            if KSR.rtpengine.rtpengine_offer(RTPE_TO_AGENT) < 0:
                KSR.err("rtpengine offer->agent failed for " +
                        KSR.pv.gete("$ci") + "\n")
                # -1 does NOT mean "nothing was allocated": the daemon may have
                # answered ok (ports pinned) and the module then failed splicing
                # the SDP back in, or the ng reply was simply lost after the
                # offer executed. The 500 below is STATELESS - no tm cell - so
                # ksr_failure_manage can never run. This delete is the only
                # teardown this call will ever get; without it the port pair is
                # pinned until silent-timeout (an offer-only session never sees
                # media, so the short `timeout` does not apply).
                # NOTE: needs aggressive_redetection=1 in kamailio.cfg, else the
                # delete cannot reach a node that was just marked disabled.
                if KSR.rtpengine.rtpengine_delete("") < 0:
                    KSR.err("ORPHANED rtpengine session (delete did not reach "
                            "the daemon) ci=" + KSR.pv.gete("$ci") + "\n")
                KSR.sl.sl_send_reply(500, "Media Anchor Failure")
                return 1
            self._relay(msg)
            return 1

        if self._from_agent(msg):
            # agent -> box: the client addressed its own cluster box (a
            # conference/feature extension). Only allow trs destinations so the
            # edge can't be used to dial arbitrary hosts.
            if self._ruri_host_in_trs(msg) <= 0:
                KSR.sl.sl_send_reply(403, "Destination Not Allowed")
                return 1
            if KSR.rtpengine.rtpengine_offer(RTPE_TO_ASTERISK) < 0:
                KSR.err("rtpengine offer->asterisk failed for " +
                        KSR.pv.gete("$ci") + "\n")
                # Same as the dialer leg above: the stateless 500 means no
                # failure route, so delete here or the ports leak until
                # silent-timeout. See the comment there.
                if KSR.rtpengine.rtpengine_delete("") < 0:
                    KSR.err("ORPHANED rtpengine session (delete did not reach "
                            "the daemon) ci=" + KSR.pv.gete("$ci") + "\n")
                KSR.sl.sl_send_reply(500, "Media Anchor Failure")
                return 1
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
            # Resolve the ws-client alias so in-dialog requests coming FROM the
            # dialer side (ACK/BYE, R-URI = sip:...@<rand>.invalid;alias=...) are
            # sent over the agent's WebSocket instead of DNS-resolving .invalid
            # and being dropped. No-op when the R-URI has no alias.
            KSR.nathelper.handle_ruri_alias()
            if KSR.is_BYE():
                KSR.rtpengine.rtpengine_delete("")
            elif KSR.is_INVITE():
                # re-INVITE (hold/resume): re-offer with the flags for whichever
                # side receives it, and re-arm the callbacks - t_on_reply and
                # t_on_failure are per transaction, so the initial INVITE's
                # arming does not carry over to this one.
                #
                # OP_OFFER is get-or-CREATE in rtpengine, so a re-INVITE for a
                # dialog whose session is already gone allocates a FRESH port
                # pair - and ksr_failure_manage's has_totag() guard (correctly)
                # refuses to delete on an in-dialog transaction, so nothing ever
                # reaps it. Re-arm the callbacks either way, but if the offer
                # fails, tear down what it may have just created.
                if self._from_agent(msg):
                    reoffer = KSR.rtpengine.rtpengine_offer(RTPE_TO_ASTERISK)
                else:
                    reoffer = KSR.rtpengine.rtpengine_offer(RTPE_TO_AGENT)
                if reoffer < 0:
                    KSR.err("rtpengine re-INVITE offer failed ci=" +
                            KSR.pv.gete("$ci") + "\n")
                    KSR.rtpengine.rtpengine_delete("")
                    KSR.sl.sl_send_reply(500, "Media Anchor Failure")
                    return 1
                KSR.tm.t_on_reply("ksr_onreply_manage")
                KSR.tm.t_on_failure("ksr_failure_manage")
            self._relay(msg)
            return 1

        # ACK to a 2xx we absorbed / no Route set
        if KSR.is_ACK():
            if KSR.tm.t_check_trans() > 0:
                KSR.tm.t_relay()
            return 1

        # In-dialog BYE that missed loose_route() (lost Route set): still release
        # the media session before answering, or the ports stay pinned.
        if KSR.is_BYE():
            KSR.rtpengine.rtpengine_delete("")

        KSR.sl.sl_send_reply(404, "Not Here")
        return 1

    # ------------------------------------------------------------------ #
    #  relay                                                             #
    # ------------------------------------------------------------------ #
    def _relay(self, msg):
        if KSR.tm.t_relay() < 0:
            # NOTE: tm already emits its own negative reply here (observed:
            # "477 ... (477/TM)" when a BYE could not be forwarded because the
            # agent's WebSocket was gone). Do NOT send another final reply -
            # an added 200 produced two finals for one BYE on av994. The 477 is
            # correct enough: the dialog ends either way and the media session
            # was already released by the BYE branch above.
            #
            # Only release media here, for the INVITE case: no transaction means
            # no failure route will fire to do it.
            if KSR.is_INVITE():
                KSR.rtpengine.rtpengine_delete("")
                KSR.sl.sl_reply_error()
        return 1

    # ------------------------------------------------------------------ #
    #  per-transaction reply / failure                                   #
    # ------------------------------------------------------------------ #
    def ksr_onreply_manage(self, msg):
        # anchor media on provisional/final answers that carry SDP. (KSR.siputils
        # has no has_body in 6.1, so detect SDP via the Content-Type header.)
        # The answer flags are direction-explicit: the SDP in this reply is
        # consumed by the party on the OTHER side of the proxy from the sender.
        ct = KSR.hdr.get("Content-Type") or ""
        if ct.find("application/sdp") >= 0:
            if self._from_agent(msg):
                # answer from the browser -> Asterisk gets plain RTP, no ICE/DTLS
                flags = RTPE_ANSWER_TO_ASTERISK
            else:
                # answer from Asterisk -> the browser gets WebRTC
                flags = RTPE_ANSWER_TO_AGENT
            # rtpengine_answer(), NOT rtpengine_manage() - see the flag comments.
            if KSR.rtpengine.rtpengine_answer(flags) < 0:
                # Cannot reject a reply; make it loud, the call will be silent.
                KSR.err("rtpengine answer failed for " +
                        KSR.pv.gete("$ci") + " - call will have no audio\n")
        # When the reply comes from the agent's WebSocket, rewrite its Contact to
        # the edge + alias so the far side sends in-dialog ACK/BYE back here (and
        # not to the unroutable sip:...@<rand>.invalid ws contact).
        if self._from_agent(msg):
            KSR.nathelper.set_contact_alias()
        return 1

    def ksr_failure_manage(self, msg):
        # A negative final on the INITIAL INVITE (including the 487 that follows
        # a CANCEL, and tm's own 408) ends the call: release the media session,
        # or the port pair stays pinned until silent-timeout (3600s). A dialer
        # produces far more unanswered than answered calls, so without this the
        # 30000-40000 range is exhausted and then EVERY call loses audio.
        #
        # NOTE: no t_is_canceled() early return - the 487 after a CANCEL is
        # exactly the case that must delete. The has_totag() guard keeps a
        # failed re-INVITE (e.g. 488 on hold) from tearing down a live call;
        # in FAILURE_ROUTE the current message is the request.
        if KSR.siputils.has_totag() <= 0:
            KSR.rtpengine.rtpengine_delete("")
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

    # ------------------------------------------------------------------ #
    #  dialog lifecycle                                                  #
    # ------------------------------------------------------------------ #
    def ksr_dialog_event(self, msg, evname):
        """dialog:start | dialog:end | dialog:failed (dialog module event_callback).

        A single guaranteed teardown hook: dialog:end covers a call that was
        answered and is now over, dialog:failed covers one that never came up
        (CANCEL, 4xx-6xx). rtpengine_delete is idempotent, so this happily
        overlaps the explicit deletes on the BYE/CANCEL/failure paths - those
        stay, because they run with the real message and are the fast path.

        IMPORTANT LIMIT: when the dialog module fires this from its own timer
        rather than from a request, it passes a FAKED message
        (dlg_run_event_route -> faked_msg_next(), dlg_handlers.c:1706/1864), and
        rtpengine only reads the Call-Id when the message is real SIP
        (rtpengine.c:3648 `if(IS_SIP(msg) || IS_SIP_REPLY(msg))`). So the delete
        below is a no-op for a pure dialog TIMEOUT. That case is still covered by
        rtpengine's own reaper (offer-timeout / silent-timeout, 180s) - dialogs
        do not replace it.
        """
        if evname == "dialog:end" or evname == "dialog:failed":
            KSR.rtpengine.rtpengine_delete("")
        return 1

    def ksr_websocket_event(self, msg, evname):
        # A closing WebSocket means that agent's browser is gone. usrloc expiry
        # clears the AoR, but any call still up produces NO BYE from the browser:
        # measured on av994, the rtpengine session survives until Asterisk's own
        # "lack of RTP activity" timer (61s) hangs up and sends a BYE, which then
        # releases it. Log it at NOTICE so that window is visible in the field;
        # closing the call properly would need dialog state (dlg module).
        KSR.info("websocket event: " + evname + " (agent connection gone; any "
                 "live call is released when the far end times out)\n")
        return 1
