# Stadiacast

[![CI](https://github.com/jkfran/stadiacast/actions/workflows/ci.yml/badge.svg)](https://github.com/jkfran/stadiacast/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Status: works end to end](https://img.shields.io/badge/status-works%20end%20to%20end-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Google shut Stadia down in January 2023 and, with it, the cloud endpoints the Stadia
Controller's **Wi-Fi mode** depends on. Stadiacast is a small server you run on your own
network that stands in for those endpoints, so the controller you own keeps working with your
own PC: it joins your Wi-Fi, connects, stays on, streams every button, stick and trigger, and
vibrates on request.

It speaks the controller's stock protocol as is: **no firmware modification**, no Google
account, and nothing leaves your LAN.

> **Status:** works end to end (input and rumble) on the last public firmware, `ota.282115`,
> with several controllers at once. Automatic first-boot provisioning and a virtual gamepad
> driver are on the [roadmap](#roadmap). Expect rough edges; issues and pull requests are very
> welcome.

## How it works

```
 Stadia Controller (Wi-Fi)                      your PC / Mac running the receiver
 ─────────────────────────                      ──────────────────────────────────
 DNS  *.googleapis.com ─────────────────────▶   DNS sink: answers with the receiver's IP
 HTTPS boot: generate_204, stadiaconfig,
       Omaha update check, Clearcut logs ─────▶  boot-time endpoints (GTS certificate)
 HTTPS v1:RegisterController,
       v1:DiscoverDisplay (WebRTC offer) ─────▶  discovery RPCs, answer queued on GMsg
 HTTPS /gmsg long poll ◀──────────────────────   GMsg push channel: the WebRTC answer
 UDP   STUN + DTLS (display link) ◀──────────▶   RTC endpoint: display RPC handshake → READY
 DTLS  WireHeader2 0x74/0x75 RPCs, input ────▶   decoded input events (JSON Lines log)
 DTLS  second session for game streaming ◀───▶   game session: input + rumble (haptics)
```

1. **DNS.** The controller must use the receiver's machine as its DNS server. The receiver
   spawns a small DNS server that points the Google names it needs at itself and forwards
   everything else upstream.
2. **HTTPS.** The firmware validates the server certificate against its built-in Google Trust
   Services roots and never checks the hostname, so a free GTS certificate for any domain you
   own lets the receiver answer every Google name.
3. **WebRTC.** The controller offers a WebRTC session in its `DiscoverDisplay` request; the
   receiver answers over the GMsg push channel, terminates STUN and DTLS, and plays the
   display side of the post-DTLS RPC handshake until the controller reaches READY and starts
   reporting input.
4. **Game session.** On `StartGame` the controller opens a second session for game streaming.
   That session carries input too, and it is the only path to the controller's rumble motors.

[docs/protocol.md](docs/protocol.md) describes the protocol in detail, and what is confirmed
versus assumed; [docs/architecture.md](docs/architecture.md) explains how the code is put
together.

## Requirements

- Python 3.11 or newer.
- A macOS or Linux host on the controller's network. The reference rig is a macOS Internet
  Sharing hotspot (macOS lets an unprivileged process bind ports 53, 80, 123 and 443; on Linux
  you need root or `CAP_NET_BIND_SERVICE`).
- A certificate chain issued by Google Trust Services for a domain you own. It is free and
  never needs renewing, because the firmware does not check the validity dates; see
  [docs/setup.md](docs/setup.md#certificate).
- Optional: `tcpdump` for packet captures, the `ble` extra for first-time Wi-Fi provisioning
  over Bluetooth, the `passthrough` extra to proxy the `stadiaconfig` request to Google.

## Install

```sh
git clone https://github.com/jkfran/stadiacast.git
cd stadiacast
uv sync --all-extras                 # creates .venv with the receiver, its extras and the dev tools
```

Without [uv](https://docs.astral.sh/uv/):

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[ble,passthrough]" --group dev
```

This installs three commands: `stadiacast`, `stadiacast-show-input` and
`stadiacast-provision` (`pip` 25.1 or newer understands `--group`). The shell scripts under
`tools/` find the virtualenv on their own.

## Quick start

1. **Network.** Put the controller and the receiver on one network where the receiver is the
   controller's DNS server. The simplest way is a hotspot hosted by the receiver's machine
   ([docs/setup.md](docs/setup.md#network)).
2. **Certificate.** Copy your GTS chain to `certs/fullchain.pem` and its key to
   `certs/privkey.pem` ([how to get one](docs/setup.md#certificate)). They are ignored by git.
3. **First boot only: Wi-Fi provisioning.** A controller fresh out of the box, or one that was
   reset, waits in Bluetooth setup mode for its Wi-Fi credentials:

   ```sh
   stadiacast-provision scan
   stadiacast-provision provision --address <ADDR> --ssid <your SSID>
   ```

4. **Start the receiver** with the configuration proven against the real controller. The
   script writes a fresh run directory `captures/<stamp>/` with the event log and, when
   `/dev/bpf*` is readable, a packet capture:

   ```sh
   tools/start_receiver.sh                 # see .env.example for the rig settings
   ```

   Or directly:

   ```sh
   stadiacast --preset live --advertise-ip 192.168.2.1 --dns-upstream 192.168.1.1 -v
   ```

5. **Wake the controller** with a ~1 s press of the Stadia button (release at the first
   vibration). Two short rumbles and a steady light mean it matched the receiver. Wakes
   alternate between full boots and short "report wakes" (a blinking light, no rumble): just
   press again.
6. **Watch the input.** The console shows one line per report; `stadiacast-show-input
   captures/<stamp>` summarizes a run. `GET http://<receiver>/_status` shows the session state.
7. **Rumble.** Start with `tools/start_receiver.sh --start-game`; the game session comes up
   about a second after READY:

   ```sh
   curl -d '{"left":40000,"right":40000,"ms":1500}' http://192.168.2.1/_rumble   # pulse
   curl -d '{"left":65535,"right":20000}' http://192.168.2.1/_rumble              # hold
   curl -d '{"left":0,"right":0}' http://192.168.2.1/_rumble                     # stop
   ```

   Levels are 0 to 65535 per motor. The controller stops its motors 5 s after the last level
   it received, so the receiver re-sends a held level every 2 s.
8. **More controllers.** Every controller that connects gets its own session, labelled `c0`,
   `c1`, ... in the log and in `/_status`. With more than one connected, name the target of a
   control request: `/_rumble?controller=c1` (or `"controller": "c1"` in the JSON body).
9. **Stop:** `tools/stop_receiver.sh`.

`--preset live` is the flag set proven end to end; explicit flags override it.
`stadiacast --help` lists every knob, grouped by protocol phase.

## Input

In READY the controller reports its state on the display link whenever something changes.
The receiver decodes it into events with named buttons, stick axes (-32768 to 32767, Y up)
and triggers (0 to 255). Bits 0 to 15 of the button mask are the XInput `wButtons` layout, so
`GamepadState.to_xinput()` in `stadiacast.gamepad` converts a report straight into
`XINPUT_GAMEPAD` fields. The full layout is in
[docs/protocol.md](docs/protocol.md#input-report-controllertodisplay-3).

## Repository layout

| path | what |
|---|---|
| `src/stadiacast/cli.py` | command line, `main()` |
| `src/stadiacast/config.py` | `Config` and the `live` preset |
| `src/stadiacast/receiver.py` | the receiver: HTTP routing, boot-time endpoints, the registry of controller sessions |
| `src/stadiacast/controller.py` | one controller's session: discovery RPCs, GMsg queue, signaling, display link, input |
| `src/stadiacast/game_session.py` | the game-streaming session: StartGame, proxied RPCs, input, rumble |
| `src/stadiacast/http_server.py` | the HTTP/1.1 server shaped for the firmware's client |
| `src/stadiacast/rtc/` | STUN, DTLS (pyOpenSSL), SRTP, RTP and WireHeader2 framing, one module each; `RtcEndpoint` ties them to a UDP socket |
| `src/stadiacast/signaling.py` | the protobuf SDP offer / answer / candidate messages |
| `src/stadiacast/protos.py` | runtime access to the protobuf schemas, the GMsg and RPC envelopes, a schema-less wire dumper |
| `src/stadiacast/input_protocol.py` | the `InputProtocolMessage` stream decoder (input on the game session) |
| `src/stadiacast/gamepad.py` | input report layout, button names, XInput conversion |
| `src/stadiacast/certs.py` | certificate handling (the served chain, the lab pair, the clock pin) |
| `src/stadiacast/log.py` | the JSON Lines event log |
| `src/stadiacast/ble_provisioner.py` | BLE Wi-Fi provisioning (`stadiacast-provision`) |
| `src/stadiacast/show_input.py` | run summary (`stadiacast-show-input`) |
| `src/stadiacast/helpers/` | the DNS sink and SNTP server the receiver spawns |
| `src/stadiacast/schemas/` | protobuf schemas: `.descriptor` (runtime) + `.proto` (readable), see its README |
| `tests/` | unit and end-to-end tests; `fake_controller.py` stands in for the controller |
| `tools/` | `start_receiver.sh`, `stop_receiver.sh` |
| `docs/` | setup guide, protocol notes, architecture overview |
| `certs/`, `captures/` | your certificate and run directories (both ignored by git) |

## Development

```sh
uv run pytest                                        # the suite drives the whole path against the fake controller
uv run ruff check . && uv run ruff format --check .  # lint and formatting
uv run mypy                                          # type check (strict)
uv run python -m stadiacast.schema_compiler --check   # the .descriptor files match their .proto sources
make check                                           # all of the above, plus shellcheck on tools/
make coverage                                        # the suite with a line and branch coverage report
```

No hardware is needed for the tests: `tests/fake_controller.py` reproduces the firmware's
network behaviour (HTTPS boot, signaling, STUN, DTLS, SRTP, the display RPC state machine,
the game session and the haptics watchdog). It also runs by hand against a receiver started
in lab mode:

```sh
uv run stadiacast --self-signed --https-port 8443 --http-ports 8080 --udp-port 40000 \
    --game-udp-port 40001 --bind 127.0.0.1 --advertise-ip 127.0.0.1 --start-game
uv run python tests/fake_controller.py --host 127.0.0.1 --https-port 8443 --game 20
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, including how to test against a real
controller safely.

## Roadmap

1. **First-boot autoconnect:** detect a controller in BLE setup mode and provision it onto the
   network automatically (today `stadiacast-provision` is the manual version).
2. **Virtual gamepad:** feed the decoded input to a virtual game controller on the PC.

## Security and privacy

The receiver listens on your LAN with no authentication and is meant for a trusted network
such as a hotspot you host. It never contacts Google except for the optional `stadiaconfig`
passthrough. See [SECURITY.md](SECURITY.md).

## Legal

This is an independent interoperability project for owners of the Stadia Controller. It is
not affiliated with, endorsed by or supported by Google. Stadia is a trademark of Google LLC.

Stadiacast contains no Google source code and neither includes nor distributes the
controller's firmware: the firmware stays on your controller and the receiver speaks its
stock network protocol as-is. The protocol was recovered by observing the controller and by
analysing the firmware it runs, for the sole purpose of interoperability. Three protobuf
schemas are reproduced from descriptors embedded in that firmware; they are Google's, and
[NOTICE](NOTICE) lists them. Please do not post firmware images, decompiled code or whole
packet captures in issues or pull requests.

Stadiacast is released under the [MIT License](LICENSE), with the exception stated in
[NOTICE](NOTICE).
