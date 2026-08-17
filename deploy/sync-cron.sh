#!/bin/bash
# Cron wrapper for sync_subscribers.py.
#
# Installed on the edge as /opt/KamailioAgentEdge/deploy/sync-cron.sh and driven
# from crontab. Keeps cron quiet (all output goes to the log, so cron only mails
# if the wrapper itself dies), takes a lock so a slow run cannot overlap the next
# one, and records the timestamp of the last SUCCESSFUL run so monitoring can
# alert on staleness rather than on a single failure.
#
# The sync itself fails closed - a short read, an implausibly small source or an
# over-budget delete all abort without touching subscriber - so a failed run
# leaves the last good credentials in place. That is why the alert to build is
# "last success is old", not "a run failed".
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

{
    echo "--- $(date -Is) starting"
    python3 "$BASE/tools/sync_subscribers.py" --env "$BASE/tools/.env" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        date -Is > "$STAMP"
        echo "$(date -Is) ok"
    else
        echo "$(date -Is) FAILED (rc=$rc) - subscriber left unchanged"
    fi
} >> "$LOG" 2>&1

# Keep the log from growing without bound.
if [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    tail -c 2000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
