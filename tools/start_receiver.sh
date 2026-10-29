#!/usr/bin/env bash
# Start the receiver with the configuration proven end to end (--preset live), plus a packet
# capture when /dev/bpf* is readable, in a fresh captures/<stamp>/ run directory.
#
# The defaults describe the reference rig: a macOS Internet Sharing hotspot on bridge100 with
# the Mac at 192.168.2.1, the controller at 192.168.2.2 and the upstream resolver at 192.168.1.1.
# Override them with environment variables, or put KEY=value lines in a .env file at the repo
# root (see .env.example):
#   ADVERTISE_IP, DNS_UPSTREAM, CONTROLLER_IP, IFACE, PCAP=0, PCAP_FILTER, PYTHON
# Extra arguments are passed to the receiver and override the preset.  Stop with tools/stop_receiver.sh.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

ADVERTISE_IP=${ADVERTISE_IP:-192.168.2.1}
DNS_UPSTREAM=${DNS_UPSTREAM:-192.168.1.1}
CONTROLLER_IP=${CONTROLLER_IP:-192.168.2.2}
IFACE=${IFACE:-bridge100}
PCAP=${PCAP:-1}
PCAP_FILTER=${PCAP_FILTER:-}   # tcpdump filter; default: everything on IFACE (every controller)

# Interpreter: the project's own virtualenv when there is one (uv sync / pip install -e .),
# otherwise the system python3 with the source tree on its path.
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if pgrep -f -- '-m stadiacast( |$)|bin/stadiacast( |$)' >/dev/null; then
  echo "a receiver is already running; stop it first: tools/stop_receiver.sh" >&2
  exit 1
fi
if [ ! -f certs/fullchain.pem ] || [ ! -f certs/privkey.pem ]; then
  echo "certs/fullchain.pem and certs/privkey.pem are missing; see certs/README.md and docs/setup.md" >&2
  exit 1
fi

RUN=captures/$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN"

if [ "$PCAP" = 1 ]; then
  if [ -r /dev/bpf0 ] && [ -w /dev/bpf0 ]; then
    read -ra pcap_filter <<< "$PCAP_FILTER"
    nohup tcpdump -i "$IFACE" -n -e -s0 -U -w "$RUN/$IFACE.pcap" ${pcap_filter[@]+"${pcap_filter[@]}"} \
      > "$RUN/tcpdump.out" 2>&1 &
    echo $! > "$RUN/tcpdump.pid"
  else
    echo "capture skipped: /dev/bpf* not accessible (sudo chmod o+rw /dev/bpf* once, or PCAP=0)" >&2
  fi
fi

args=(--preset live --advertise-ip "$ADVERTISE_IP" --dns-upstream "$DNS_UPSTREAM" --controller-ip "$CONTROLLER_IP"
      -v --log-dir "$RUN")
nohup "$PYTHON" -m stadiacast "${args[@]}" "$@" > "$RUN/server.out" 2>&1 &
echo $! > "$RUN/server.pid"

for _ in $(seq 1 20); do
  grep -q '"event": "ready"' "$RUN/server.jsonl" 2>/dev/null && break
  sleep 0.5
done
if ! grep -q '"event": "ready"' "$RUN/server.jsonl" 2>/dev/null; then
  echo "receiver did not report ready; see $RUN/server.out" >&2
  exit 1
fi

cat <<MSG
receiver ready (preset live), run dir: $RUN
  wake the controller: ~1 s press of the Stadia button (release at the first vibration).
  a blinking light with no rumble is a report wake: press again.
  decoded input:  $PYTHON -m stadiacast.show_input $RUN
  stop:           tools/stop_receiver.sh
MSG
