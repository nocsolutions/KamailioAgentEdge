#!/bin/bash
# Self-signed TLS cert for the WSS listener (lab/sipp only - browsers will warn).
# For production use the real cert for the edge FQDN instead.
#
#   sudo deploy/gen_selfsigned_cert.sh [CN]
set -euo pipefail

CN="${1:-$(hostname -f 2>/dev/null || hostname)}"
DIR=/etc/kamailio/tls
sudo mkdir -p "$DIR"

sudo openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${DIR}/edge-key.pem" \
    -out    "${DIR}/edge-cert.pem" \
    -days 825 -subj "/CN=${CN}"

sudo chgrp kamailio "${DIR}"/edge-*.pem
sudo chmod 640 "${DIR}/edge-key.pem"
sudo chmod 644 "${DIR}/edge-cert.pem"
echo "self-signed cert for CN=${CN} written to ${DIR}"
