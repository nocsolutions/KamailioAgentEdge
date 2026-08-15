#!/bin/bash
# Start the werift WebRTC test agent on the edge and begin captures.
#   run_agent.sh <ext> <password>
EXT="$1"
PASS="$2"
cd "$HOME/wsclient" || exit 1

pkill -f 'node agent.js' 2>/dev/null
sudo pkill -f 'tcpdump -i any' 2>/dev/null
sudo rm -f /tmp/sip5060.pcap /tmp/media.pcap /tmp/agent_live.log

nohup node agent.js "wss://edge.avatar.tech:4443/ws" "$EXT" "$PASS" avatar.tech \
    > /tmp/agent_live.log 2>&1 &
sleep 5

sudo nohup tcpdump -i any -n -s0 -w /tmp/sip5060.pcap 'udp port 5060' >/dev/null 2>&1 &
sudo nohup tcpdump -i any -n -s0 -w /tmp/media.pcap 'udp portrange 30000-40000' >/dev/null 2>&1 &
sleep 2

echo "--- agent ---"
grep -aE 'REGISTERED|WS error|error' /tmp/agent_live.log | head -3
echo "--- registrations ---"
sudo kamcmd ul.dump 2>/dev/null | grep -aE 'AoR:'
echo "--- captures ---"
pgrep -c tcpdump
