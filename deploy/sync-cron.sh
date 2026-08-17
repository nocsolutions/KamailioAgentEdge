#!/bin/bash
# Cron wrapper for sync_subscribers.py. Runs every minute:
#
#   * * * * * /opt/KamailioAgentEdge/deploy/sync-cron.sh
#
# A run takes ~1.8s (measured on the edge: ssh+sqlite read of the master, then a
# batched upsert into the local MySQL), so a minute is comfortable. The flock
# below makes that safe regardless - if a run ever does overrun, the next one
# steps aside instead of piling up.
#
# LOGGING IS DELIBERATELY QUIET. At one run a minute an always-on log writes
# ~5800 no-op lines a day, which buries the events worth seeing. So a run that
# changes nothing logs nothing; only changes, conflicts and failures are written.
# Liveness is tracked separately by the last_success stamp, which is refreshed on
# every successful run - so "nothing in the log" means healthy, and staleness is
# still detectable.
#
# The sync itself fails closed - a short read, an implausibly small source or an
# over-budget delete all abort without touching subscriber - so a failed run
# leaves the last good credentials in place. Alert on the STAMP being stale, not
# on a single failed run:
#
#   test $(( $(date +%s) - $(date -d "$(cat /var/lib/coregears/last_success)" +%s) )) -lt 300
set -u

BASE=/opt/KamailioAgentEdge
LOG=/var/log/coregears/sync_subscribers.log
STAMP=/var/lib/coregears/last_success
LOCK=/var/lib/coregears/sync.lock

mkdir -p "$(dirname "$LOG")" "$(dirname "$STAMP")" 2>/dev/null

exec 9>"$LOCK" || exit 1
if ! flock -n 9; then
    echo "$(date -Is) skipped: previous run still going" >> "$LOG"
    exit 0
fi

out=$(python3 "$BASE/tools/sync_subscribers.py" --env "$BASE/tools/.env" 2>&1)
rc=$?

if [ "$rc" -eq 0 ]; then
    date -Is > "$STAMP"
    # Only speak up when the run actually did something, or when the source has
    # extensions it cannot serve. A steady state is silent.
    if echo "$out" | grep -qE '\+[1-9][0-9]* added|~[1-9][0-9]* updated|-[1-9][0-9]* removed' \
       || echo "$out" | grep -q '^!!'; then
        { echo "--- $(date -Is)"; echo "$out"; } >> "$LOG"
    fi
else
    { echo "--- $(date -Is) FAILED (rc=$rc) - subscriber left unchanged"
      echo "$out"; } >> "$LOG"
fi

# Keep the log from growing without bound.
if [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    tail -c 2000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
