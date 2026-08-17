-- Per-cluster cutover: point every webphone peer at the KamailioAgentEdge.
--
-- Run against the cluster's VICIdial database. This rewrites the EXISTING
-- `webphone` conf template rather than adding a parallel one, because the
-- cutover is wholesale per cluster - a cluster's webphones all go to the edge
-- or none do, and a second template would force a legacy-vs-edge fork in the
-- DialerWeb agent screen.
--
-- Set EDGE_INT_IP to the edge's Asterisk-facing address before running.
--
--   mysql vicidial < webphone-template-edge.sql
--
-- VICIdial's cron (ADMIN_keepalive_ALL.pl, ~1 min) sees rebuild_conf_files='Y',
-- regenerates /etc/asterisk/sip-vicidial.conf on every telephony server in the
-- cluster (phones.server_ip='0.0.0.0' => cluster-wide) and reloads chan_sip.
--
-- CRITICAL - every WebRTC option is explicitly negated, not merely omitted.
-- chan_sip peer settings are STICKY across `sip reload` AND `module reload
-- chan_sip.so`: a peer that previously had dtlsenable=yes keeps offering
-- "m=audio <port> UDP/TLS/RTP/SAVP" with an a=fingerprint even after the option
-- disappears from the config. Verified live on av994 (2026-08-15) - the peer
-- reported "Encryption: No" while still offering DTLS, and the edge's plain
-- RTP/AVP answer did not match, so there was no audio. Only a full Asterisk
-- restart clears it, which a 650-box fleet cutover cannot do; the explicit "no"
-- values are what make a plain reload sufficient.
--
-- Rollback: restore the previous template_contents (keep a backup first:
--   SELECT template_contents FROM vicidial_conf_templates WHERE template_id='webphone';
-- ) and set rebuild_conf_files='Y' again.

UPDATE vicidial_conf_templates
SET template_contents='type=friend\nhost=agentedgeint.avatar.tech\nport=5060\nqualify=yes\ncontext=default\ndisallow=all\nallow=ulaw\nallow=opus\ndtmfmode=auto\ndirectmedia=no\nnat=force_rport,comedia\ntransport=udp\nencryption=no\navpf=no\nforce_avp=no\nicesupport=no\ndtlsenable=no\nrtcp_mux=no'
WHERE template_id='webphone';

-- Trigger regeneration + chan_sip reload on every server in the cluster.
UPDATE servers SET rebuild_conf_files='Y';

-- Verify afterwards (expect a plain RTP/AVP offer, no a=fingerprint):
--   grep -A20 '^\[<ext>\]' /etc/asterisk/sip-vicidial.conf
--   asterisk -rx 'sip show peer <ext>'
