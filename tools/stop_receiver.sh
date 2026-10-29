#!/usr/bin/env bash
# Stop what tools/start_receiver.sh started: the receiver, its DNS/SNTP helpers and the packet
# capture.  Falls back to matching the process names when the pid files are gone.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
stopped=0
for pidfile in captures/*/server.pid captures/*/tcpdump.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  if kill "$pid" 2>/dev/null; then stopped=$((stopped + 1)); fi
  rm -f "$pidfile"
done
pkill -f -- '-m stadiacast( |$)|bin/stadiacast( |$)' 2>/dev/null && stopped=$((stopped + 1))
sleep 1
pkill -f 'stadiacast\.helpers\.(dns_sink|sntp_server)' 2>/dev/null
echo "stopped $stopped process(es)"
