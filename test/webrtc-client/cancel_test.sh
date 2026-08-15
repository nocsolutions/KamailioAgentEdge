#!/bin/bash
# Focused CANCEL test: verify the 487 carries a Via stack, arrives promptly, and
# the rtpengine session is released.
#   cancel_test.sh <password>
PW="${1:?need password}"
SP="$(cd "$(dirname "$0")" && pwd)"
EDGE="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 9999 alexandruc@10.4.100.147"
AST="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 1818 alexandruc@10.4.59.64"
ast() { $AST "sudo -n /usr/sbin/asterisk -rx '$1'" 2>/dev/null; }

pkill -f "node $SP/wsclient/agent.js" 2>/dev/null
$EDGE 'sudo rm -f /tmp/ct.pcap; sudo nohup timeout 60 tcpdump -i any -n -s0 -w /tmp/ct.pcap "udp port 5060" >/dev/null 2>&1 </dev/null & sleep 1; echo "capture: $(pgrep -fc "timeout 60 tcpdump")"' 2>/dev/null

NOANSWER=1 node "$SP/wsclient/agent.js" "wss://216.66.20.147:4443/ws" 21382 "$PW" avatar.tech >/tmp/ct_agent.log 2>&1 &
AP=$!
sleep 6
echo "agent: $(grep -ac REGISTERED /tmp/ct_agent.log) registered"
echo "sessions before: $($EDGE 'rtpengine-ctl --ip 127.0.0.1 --port 9900 list numsessions 2>/dev/null | awk "/sessions own/{print \$NF}"' 2>/dev/null)"

ast "channel originate SIP/21382 application Milliwatt" >/dev/null 2>&1 &
# wait until the channel actually exists
for i in $(seq 1 15); do
  CH=$(ast "core show channels concise" | grep -a "SIP/21382" | head -1 | cut -d'!' -f1)
  [ -n "$CH" ] && break
  sleep 1
done
echo "channel: ${CH:-NOT FOUND}"
[ -z "$CH" ] && { kill $AP 2>/dev/null; exit 1; }
sleep 2
echo "hanging up (-> CANCEL)"
ast "channel request hangup $CH" >/dev/null
sleep 12
echo "agent saw: $(grep -a 'CANCEL' /tmp/ct_agent.log | tail -1)"
echo "sessions after: $($EDGE 'rtpengine-ctl --ip 127.0.0.1 --port 9900 list numsessions 2>/dev/null | awk "/sessions own/{print \$NF}"' 2>/dev/null)"
kill $AP 2>/dev/null

$EDGE 'until ! pgrep -f "timeout 60 tcpdump" >/dev/null 2>&1; do sleep 2; done
       sudo cp /tmp/ct.pcap /tmp/ctr.pcap; sudo chmod 644 /tmp/ctr.pcap
       echo "=== ladder ==="
       python3 /tmp/sipflow.py /tmp/ctr.pcap 2>/dev/null | grep -avE "OPTIONS|Keepalive" | grep -a "s  " | head -12
       echo "=== 487 headers (Via present?) ==="
       python3 /tmp/sipdump.py /tmp/ctr.pcap "" "487" 2>/dev/null | head -12' 2>/dev/null
