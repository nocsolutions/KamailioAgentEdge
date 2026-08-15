#!/bin/bash
# Push the edge config to a host and reload. Validates with `kamailio -c` before
# touching the running service, and never overwrites kamailio-local.cfg (the
# per-host secrets) - it seeds the .example only if none exists yet.
#
#   EDGE_HOST=10.4.100.147 EDGE_PORT=9999 EDGE_USER=alexandruc deploy/deploy.sh
#
# Files pushed:
#   etc/kamailio/kamailio.cfg  kamailio.py  tls.cfg  kamctlrc  -> /etc/kamailio/
#   etc/default/kamailio                                       -> /etc/default/
#   etc/rtpengine/rtpengine.conf                               -> /etc/rtpengine/
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${EDGE_HOST:-10.4.100.147}"
PORT="${EDGE_PORT:-9999}"
USER="${EDGE_USER:-alexandruc}"
SSH="ssh -p ${PORT} ${USER}@${HOST}"
SCP="scp -P ${PORT}"

echo ">> staging tarball"
TB="$(mktemp /tmp/agentedge-XXXX.tgz)"
tar -C "${HERE}" -czf "${TB}" \
    etc/kamailio/kamailio.cfg \
    etc/kamailio/kamailio.py \
    etc/kamailio/tls.cfg \
    etc/kamailio/kamctlrc \
    etc/kamailio/kamailio-local.cfg.example \
    etc/default/kamailio \
    etc/rtpengine/rtpengine.conf

echo ">> copying to ${HOST}"
${SCP} -q "${TB}" "${USER}@${HOST}:/tmp/agentedge.tgz"
rm -f "${TB}"

echo ">> installing + validating on ${HOST}"
# shellcheck disable=SC2087
${SSH} 'bash -s' <<'REMOTE'
set -euo pipefail
tmp="$(mktemp -d)"
tar -C "$tmp" -xzf /tmp/agentedge.tgz
sudo install -o root -g kamailio -m 0644 "$tmp/etc/kamailio/kamailio.cfg" /etc/kamailio/kamailio.cfg
sudo install -o root -g kamailio -m 0644 "$tmp/etc/kamailio/kamailio.py"  /etc/kamailio/kamailio.py
sudo install -o root -g kamailio -m 0644 "$tmp/etc/kamailio/tls.cfg"      /etc/kamailio/tls.cfg
sudo install -o root -g root     -m 0644 "$tmp/etc/kamailio/kamctlrc"     /etc/kamailio/kamctlrc
sudo install -o root -g root     -m 0644 "$tmp/etc/default/kamailio"      /etc/default/kamailio
sudo install -d /etc/rtpengine
sudo install -o root -g root     -m 0644 "$tmp/etc/rtpengine/rtpengine.conf" /etc/rtpengine/rtpengine.conf
# seed local secrets file only if absent
if ! sudo test -f /etc/kamailio/kamailio-local.cfg; then
  sudo install -o root -g kamailio -m 0640 "$tmp/etc/kamailio/kamailio-local.cfg.example" /etc/kamailio/kamailio-local.cfg
  echo "!! seeded kamailio-local.cfg from example - edit it (DBURL, IPs) before starting"
fi
rm -rf "$tmp" /tmp/agentedge.tgz

echo ">> kamailio -c (config check)"
sudo kamailio -c -f /etc/kamailio/kamailio.cfg
REMOTE

echo
echo ">> config valid. To apply:"
echo "   ${SSH} 'sudo systemctl restart rtpengine kamailio'"
echo "   (restart, not reload - KEMI script changes need a full restart)"
