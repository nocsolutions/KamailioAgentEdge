#!/bin/bash
# Call-id-level rtpengine leak test - immune to other calls coming and going.
# For each scenario: snapshot session call-ids, run it, snapshot again, and report
# which call-id (if any) was created by the scenario and survived.
#
#   AST_HOST=<asterisk-ip> EXT=<ext> leaktest2.sh <password>
PW="${1:?need 21382 password}"
SP="$(cd "$(dirname "$0")" && pwd)"
EDGE="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 9999 alexandruc@10.4.100.147"
AST="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 1818 alexandruc@${AST_HOST:-10.4.59.64}"

ids() { $EDGE 'rtpengine-ctl --ip 127.0.0.1 --port 9900 list sessions all 2>/dev/null' 2>/dev/null \
        | sed -n 's/.*ID: *\([^ |]*\).*/\1/p' | sort; }
ast() { $AST "sudo -n /usr/sbin/asterisk -rx '$1'" 2>/dev/null; }

start_agent() {
  pkill -f "node $SP/wsclient/agent.js" 2>/dev/null
  env $1 node "$SP/wsclient/agent.js" "wss://216.66.20.147:4443/ws" ${EXT:-21382} "$PW" avatar.tech \
      > /tmp/leak_agent.log 2>&1 &
  sleep 5
}
stop_agent() { pkill -f "node $SP/wsclient/agent.js" 2>/dev/null; sleep 1; }

scenario() { # $1 name  $2 agent-env  $3 seconds-before-hangup  $4 hangup(yes/no)
  echo "=== $1 ==="
  [ -n "$2" ] && start_agent "$2" || start_agent ""
  ids > /tmp/ids_before
  ast "channel originate SIP/${EXT:-21382} application Milliwatt" >/dev/null &
  sleep "$3"
  ids > /tmp/ids_during
  NEW=$(comm -13 /tmp/ids_before /tmp/ids_during)
  echo "  session created by this call: ${NEW:-<none>}"
  if [ "$4" = "yes" ]; then
    CH=$(ast "core show channels concise" | grep -a "SIP/${EXT:-21382}" | head -1 | cut -d'!' -f1)
    [ -n "$CH" ] && ast "channel request hangup $CH" >/dev/null && echo "  hung up $CH"
  fi
  sleep 8
  ids > /tmp/ids_after
  if [ -z "$NEW" ]; then
    echo "  RESULT: no session was created (nothing to leak)"
  elif echo "$NEW" | while read -r i; do grep -qxF "$i" /tmp/ids_after && echo FOUND; done | grep -q FOUND; then
    echo "  *** LEAK *** session still present after teardown"
  else
    echo "  PASS: session released"
  fi
  stop_agent
  echo
}

echo "sessions at start: $(ids | wc -l)"; echo
scenario "A: answered call, BYE from Asterisk" ""          8  yes
scenario "B: CANCEL while ringing"             "NOANSWER=1" 6  yes
echo "sessions at end: $(ids | wc -l)"
