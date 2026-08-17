#!/bin/bash
# Remaining teardown scenarios: no-answer timeout and agent-side BYE.
#   AST_HOST=<asterisk-ip> EXT=<ext> leaktest3.sh <password>
PW="${1:?need 21382 password}"
SP="$(cd "$(dirname "$0")" && pwd)"
EDGE="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 9999 alexandruc@10.4.100.147"
AST="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 1818 alexandruc@${AST_HOST:-10.4.59.64}"

ids() { $EDGE 'rtpengine-ctl --ip 127.0.0.1 --port 9900 list sessions all 2>/dev/null' 2>/dev/null \
        | sed -n 's/.*ID: *\([^ |]*\).*/\1/p' | sort; }
ast() { $AST "sudo -n /usr/sbin/asterisk -rx '$1'" 2>/dev/null; }
stop_agent() { pkill -f "node $SP/wsclient/agent.js" 2>/dev/null; sleep 1; }

check() { # $1 name  $2 new-ids
  ids > /tmp/i_after
  if [ -z "$2" ]; then echo "  RESULT: no session created"; return; fi
  if echo "$2" | while read -r i; do grep -qxF "$i" /tmp/i_after && echo F; done | grep -q F; then
    echo "  *** LEAK *** $1"
  else
    echo "  PASS: $1 released"
  fi
}

echo "=== C: no-answer, caller gives up (originate timeout -> CANCEL) ==="
stop_agent
NOANSWER=1 node "$SP/wsclient/agent.js" "wss://216.66.20.147:4443/ws" ${EXT:-21382} "$PW" avatar.tech >/tmp/lt3a.log 2>&1 &
sleep 5
ids > /tmp/i_before
# originate with a short timeout so Asterisk itself gives up
ast "originate SIP/${EXT:-21382} application Milliwatt" >/dev/null 2>&1 &
sleep 7
ids > /tmp/i_during
NEW=$(comm -13 /tmp/i_before /tmp/i_during)
echo "  session created: ${NEW:-<none>}"
echo "  letting Asterisk time out (35s)..."
sleep 35
check "no-answer timeout" "$NEW"
stop_agent
echo

echo "=== D: agent-side BYE (browser hangs up) ==="
node "$SP/wsclient/agent.js" "wss://216.66.20.147:4443/ws" ${EXT:-21382} "$PW" avatar.tech >/tmp/lt3b.log 2>&1 &
sleep 5
ids > /tmp/i_before
ast "channel originate SIP/${EXT:-21382} application Milliwatt" >/dev/null 2>&1 &
sleep 9
ids > /tmp/i_during
NEW=$(comm -13 /tmp/i_before /tmp/i_during)
echo "  session created: ${NEW:-<none>}"
echo "  killing the agent's WebSocket (simulates browser close - no BYE at all)"
stop_agent
sleep 12
check "agent WS death (no BYE)" "$NEW"
CH=$(ast "core show channels concise" | grep -a "SIP/${EXT:-21382}" | head -1 | cut -d'!' -f1)
[ -n "$CH" ] && { echo "  (asterisk channel $CH still up - hanging up)"; ast "channel request hangup $CH" >/dev/null; sleep 6; check "after asterisk BYE" "$NEW"; }
echo
echo "final sessions: $(ids | wc -l)"
