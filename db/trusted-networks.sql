-- Source-address trust for the edge's Asterisk-facing leg.
--
-- The KEMI script treats a request as coming from a dialer when its source
-- address is in address group 1 (permissions / allow_source_address). Every
-- telephony server reaches the edge over the internal network, so trusting that
-- network is the whole rule - one row.
--
-- This replaced a per-host list scraped from the fleet API. That list held each
-- telephony server's PUBLIC address, but boxes arrive on their INTERNAL one, so
-- INVITEs were answered 403 Forbidden (av54d: 216.66.18.147 was allowed, the
-- INVITE came from 10.4.18.147). Deriving the internal address per host would
-- have meant ~650 extra rows to keep in step with the fleet, for no more
-- security than trusting the range they all live in - which the edge's own
-- firewall already accepts.
--
-- The public side needs no entry here: agents come from anywhere on the
-- internet, and they are authenticated by SIP digest at REGISTER, not by
-- address.
--
--   mysql kamailio < trusted-networks.sql
--   kamcmd permissions.addressReload      # /usr/sbin/kamcmd - not on cron's PATH
--
-- Narrow the CIDR here if the internal range is ever segmented; the KEMI script
-- needs no change either way.

DELETE FROM address WHERE grp = 1;

INSERT INTO address (grp, ip_addr, mask, port) VALUES
  (1, '10.0.0.0', 8, 0);

SELECT ip_addr, mask FROM address WHERE grp = 1;
