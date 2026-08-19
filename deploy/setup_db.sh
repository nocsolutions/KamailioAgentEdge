#!/bin/bash
# Create the kamailio DB (subscriber/location/address + standard tables) using
# kamdbctl, non-interactively. Passwords come from tools/.env, not kamctlrc, so
# no secret is committed. Idempotent-ish: kamdbctl create fails loudly if the DB
# already exists - drop it first if you really want a clean slate.
#
#   sudo deploy/setup_db.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENVFILE="${HERE}/tools/.env"
[ -f "$ENVFILE" ] || { echo "missing ${ENVFILE} (cp tools/.env.example)"; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENVFILE"; set +a

: "${KAM_DB_PASS:?set KAM_DB_PASS in tools/.env}"

# kamdbctl reads kamctlrc for engine/host/name/users, and DBRWPW/DBROPW from env.
export DBRWPW="${KAM_DB_PASS}"
export DBROPW="${KAM_DB_RO_PASS:-${KAM_DB_PASS}}"

echo "Creating kamailio database via kamdbctl (MySQL) ..."
# 'y' to create, 'y' to standard tables. Extra table groups are off in kamctlrc.
yes y | kamdbctl create

echo
echo "Tables present:"
sudo mysql "${KAM_DB_NAME:-kamailio}" -e "SHOW TABLES;" | \
    grep -E "subscriber|location|address|version" || true

echo
echo "Done. Next:"
echo "  mysql kamailio < db/trusted-networks.sql   # trust the internal network"
echo "  tools/sync_subscribers.py    # mirror webphone credentials"
