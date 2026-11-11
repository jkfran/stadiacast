# Architecture

How the receiver is put together, for people who want to change it. [protocol.md](protocol.md)
explains *what* the controller does on the network; this page explains *where* the code that
answers it lives and how the pieces talk to each other.

## Processes

One `stadiacast` process serves every controller. It spawns two helpers next to itself, both
logging JSON Lines into the run directory. `stadiacast.helpers` is their launcher:
`helper_command()` builds `python -m stadiacast.helpers.<name>`, or, in a standalone executable
that has no interpreter to run `-m` with, the executable itself with `--helper <name>`, which
`run_helper()` dispatches.

| process | module | why separate |
|---|---|---|
| the receiver | `stadiacast.cli` → `stadiacast.receiver` | HTTPS/HTTP listeners, every controller's UDP sockets, the event log |
| DNS sink | `stadiacast.helpers.dns_sink` (wire format: `helpers.dnswire`) | binds port 53; answers `*.googleapis.com` and the `--dns-redirect` names with the receiver's IP, forwards everything else |
| SNTP server | `stadiacast.helpers.sntp_server` | binds port 123; answers `time.google.com` with the host clock |

The helpers are plain blocking servers in their own processes so they keep answering while the
receiver's event loop is busy, and so the privileged-looking ports are isolated. The receiver
watches them: one that exits is logged (`helper_exited`) with the last lines it wrote.

Two more commands run on their own: `stadiacast-provision` (`ble_provisioner.py`) sends a
controller in Bluetooth setup mode its Wi-Fi credentials, and `stadiacast-show-input`
(`show_input.py`) summarizes the input recorded in a run directory.

## Objects

```
Receiver                                   one per process (receiver.py)
 ├── HttpServer                            HTTPS :443 + HTTP :80/:8008, reads each ClientHello (http_server.py)
 ├── TLS credential                        built-in, your own or a lab pair; the clock pin (certs.py)
 ├── Protos                                the protobuf schemas, loaded once (protos.py)
 ├── EventLog                              server.jsonl + console echo (log.py)
 ├── VirtualGamepads                       --gamepad: one virtual pad per controller (virtual_gamepad.py)
 └── ControllerSession  c0, c1, ...        one per controller (controller.py)
      ├── RtcEndpoint "input"              UDP socket of the display link (rtc/endpoint.py)
      │    ├── StunMessage / ICE checks    rtc/stun.py
      │    ├── DtlsSession + DtlsCertificate   rtc/dtls.py
      │    ├── SrtpContext (in / out)      rtc/srtp.py
      │    ├── RtpPacket parse / build     rtc/rtp.py
      │    └── WireHeader2 framing         rtc/wire.py
      ├── display RPC state machine        GetControllerState → READY → input (controller.py)
      ├── InputDecoder                     InputProtocolMessage stream (input_protocol.py)
      ├── AudioSender                      Opus to the headset jack, paced RTP on the media pair (audio.py)
      │    └── BlockCapture                PCM for system / device:NAME (capture.py; loopback.py on Windows)
      └── GameSession                      StartGame, proxied RPCs, rumble (game_session.py)
           ├── RtcEndpoint "game"          second UDP socket, own certificate
           └── InputDecoder
```

**`Receiver`** owns what is shared and nothing that belongs to one controller. It routes HTTP
requests (`_route`), answers the boot-time endpoints itself (Omaha, Clearcut, and the
connectivity check and `stadiaconfig` when their names are redirected to it, which the default
DNS sink does not do) and hands everything that identifies a controller to that controller's
session. It is also the registry: `controller_for()` finds or creates the session for a request.
Its own address towards the controller and the `stadiaconfig` fetch come from `netutil.py`,
which the helpers share for the message on a port that is taken. `live_state()` is the
`/_input` document, and whatever changes something in it calls `live_changed()`, which wakes
the `/_events` clients (a `LiveStream` each).

**`HttpServer`** frames HTTP/1.1 and owns the listeners. The HTTPS one accepts its connections
itself (`HelloServer`) and reads the server name in each ClientHello before the TLS layer does:
a ClientHello for the Clearcut host is answered with a TLS alert and closed (protocol.md §7),
and every other connection is handed to asyncio's TLS with nothing read from it. asyncio's own
TLS listener cannot do that, since it closes a refused handshake without sending the alert.
A response can carry a `stream`, a body that is written piece by piece as it is produced and
ends the connection: the `/_events` streams are the one use.

**`ControllerSession`** is one controller as the receiver sees it: the discovery RPCs
(`register`, `discover`), the GMsg queue and long poll (`queue_gmsg`, `gmsg_poll`), the
signaling answer, the display-link endpoint and its callbacks, the post-DTLS RPC state machine
(`_display_rpc_loop`, `_on_rpc`, `_answer_request`) and the input events. Its `session` dict is
what `/_status` publishes; the `display` sub-dict is reset on every new boot. With
`--audio-source` it also owns an `AudioSender` (`audio.py`): started once the display link is
READY, it paces Opus frames onto the display link's media pair under the stream's own SSRC. The
session keeps the stream's gain and libopus application across starts, since a sender only
lives while it plays. ffmpeg is only the convenient encoder: a front end with its own capture
and encoder starts the pacer alone with `audio_start(None)` and hands it Opus frames through
`audio_feed()`.

**`AudioSender`** runs ffmpeg as its encoder and starts it again when it exits. For `system` and
`device:NAME` it also runs a `BlockCapture` (`capture.py`): a `CaptureDevice` says which PCM
format its device delivers and makes a fresh capture for every ffmpeg run, and the capture hands
over 20 ms blocks through a bounded queue. `PortAudioCapture` (macOS, Linux) and the WASAPI
loopback's `Capture` (`loopback.py`, Windows) only open their device and push blocks; the
loopback also renders silence on its speaker, since WASAPI feeds a loopback only while something
plays. One
worker-thread call per block reads it, scales it by the Windows volume (`sysvolume.py`) and
measures its level, so no per-sample work runs on the event loop, which then writes the block to
ffmpeg's stdin.

**`GameSession`** is the game phase of one controller: it sends `StartGame`, answers the
`InitializationRequest` the controller proxies through the display link, runs the second
ICE/DTLS endpoint, decodes input on sub-channel `0x66` and drives the rumble motors with the
haptics-watchdog refresh. It talks to its controller through `ctl.send_rpc`, `ctl.mark_input`
and `ctl.emit_input_event`.

**`VirtualGamepads`** (`--gamepad`) belongs to the `Receiver`, which hands it every input event
it publishes (`publish_event`), from the display link and the game session alike
(`GamepadState.from_event` reads both shapes). It creates one pad per controller label in a
worker thread, forwards the motor levels a game sets to that controller's `GameSession`, and
lets go of the pad's state when the controller reboots, which is also when a pad that could not
be created is tried again.

The macOS pad (`ojd_gamepad.py`, `--gamepad ojd`) is a process: `ojd virtual feed`, the command
line of the OpenJoystickDriver app, which holds the entitlement a virtual HID device needs. Each
controller has one. Three threads keep its pipes off the event loop: one writes a JSON frame
per line, at most every 10 ms, one reads the rumble commands the driver prints, and one keeps
the last lines of its error output for the message when it ends. Reports queue in order; one
that only moves a stick replaces the one waiting, and a button or trigger change never does.
The driver hands rumble over when a frame arrives and otherwise every 500 ms, so the held state
is written again every 20 ms: a game that renews a 100 ms effect would pulse without it. A
rumble with a duration is stopped by a timer 40 ms past its end unless it was renewed. Sticks
turn from XInput's Y-up to the driver's Y-down, and motor levels from 16 bits to the 8 the
shared code takes. A feed that ends by itself says so through `on_error`, with no report needed
to find out. `VirtualGamepads` closes a feed when its controller goes away, which frees one of
the driver's four, and does it in a worker thread, since closing waits for the process.

**`RtcEndpoint`** is one UDP socket and one peer. It demultiplexes datagrams (STUN / DTLS /
RTP), runs the connectivity checks and keepalives, drives the DTLS handshake through pyOpenSSL
memory BIOs, exports SRTP keys, and delivers application data (`raw_dtls` sessions) or decrypted
RTP to callbacks. Callbacks are isolated: an exception in one is logged, the socket lives on.

## Life of one controller

1. **DNS and boot.** The controller resolves the Google names through the DNS sink and reaches
   the receiver over HTTPS. The boot endpoints are answered by `Receiver` without a session.
2. **Identification.** The first `v1:` RPC carries the controller's ids; `controller_for()`
   creates a `ControllerSession` with the next free slot, two UDP sockets (base port + 2 × slot),
   its own DTLS certificates and the display token `stadia-local-receiver-display-<slot>`.
3. **Discovery.** `RegisterController` / `DiscoverDisplay` carry the controller's WebRTC offer;
   the session accepts it, builds the answer bundle (`signaling.py`) and queues it on GMsg.
4. **GMsg.** The controller polls `/gmsg` with the display token as `subscription`; the receiver
   attributes the poll by that token and the session delivers the envelope, re-sending until the
   controller acks the message id.
5. **ICE and DTLS.** The controller's checks and ClientHello arrive on the session's own port;
   `RtcEndpoint` selects the pair and completes DTLS as the server (`live` preset).
6. **Display RPCs.** After DTLS (and, by default, after `ResolveDiscovery`) the session sends
   `GetControllerStateRequest` until the controller answers, answers the controller's time and
   keepalive requests, and reaches READY. Input reports arrive as RPC notifications and become
   `input` events.
7. **Game session.** With `--start-game` the session sends `StartGame` once READY; the
   controller signals the second session through the display link, `GameSession` answers with
   its endpoint, the controller connects, and from then on input and rumble flow there too.
8. **Reboot.** A controller that boots again is matched to its existing session by id (or
   token, or address); the session resets its per-boot state and keeps its label and ports.

## Attribution

Requests are attributed to a controller in this order: the GMsg `subscription` token the
receiver issued, the ids the firmware sends (`device_id` in the body, the
`controller-cloud-device-id` and `controller-device-id` headers), then the peer address. A
request from a new address without any id creates a session keyed by the address; the ids seen
later adopt it. The UDP side needs no attribution: every session has its own sockets.

## The event log

Every component logs through the one `EventLog` (`self.log("event_name", key=value, ...)`),
sessions through a `BoundLog` that stamps `controller="c1"` on each record. Records are JSON
Lines in `captures/<stamp>/server.jsonl`; the console shows a compact line per record, with the
bulky fields (bodies, decoded messages) only under `-v`. Event names are stable identifiers:
tests assert on them and people grep for them, so new behaviour gets a new event rather than a
renamed one. `/_status` returns the live session dicts plus the event counts.

## Configuration

`Config` (`config.py`) is a flat dataclass: every knob of the receiver, with a comment giving
its accepted values. `cli.py` builds it from the command line, grouped by protocol phase. A
preset is a dict of `Config` fields applied as argparse defaults, so explicit flags override it;
`PRESETS["live"]` is the flag set proven against the real controller. New behaviour gets its own
field and flag, default off, so the preset keeps meaning "known to work".

## Lab and live

The whole path runs without hardware: `tests/fake_controller.py` reproduces the firmware's
network behaviour (HTTPS boot, signaling, STUN, DTLS, the display RPC state machine, the game
session and the haptics watchdog), and the end-to-end tests drive a receiver on loopback with
ephemeral ports and a self-signed certificate (`tests/helpers.py`). The same fake runs by hand
against a receiver started in lab mode ([CONTRIBUTING.md](../CONTRIBUTING.md#tests)). Against a
real controller the receiver runs with `--preset live` on ports 53/80/123/443, which
`tools/start_receiver.sh` sets up.

## Where to add things

| change | where |
|---|---|
| a new knob | a `Config` field, a flag in the matching group of `cli.build_parser`, its entry in the `Config(...)` call of `cli.parse_args` (which maps and renames the parsed flags; a flag left out of it is ignored), a CHANGELOG line |
| a new boot-time endpoint | `Receiver._route` and a handler next to `_omaha` / `_clearcut` |
| a new display RPC the controller sends | `ControllerSession._answer_request` |
| a new message on the game session | `GameSession._on_app_data` / `_on_rpc` |
| an output for the decoded input | consume `Receiver.wait_event()` / `publish_event()` events; `virtual_gamepad.py` is one |
| a new audio capture | a `CaptureDevice` and `BlockCapture` pair (`capture.py`; `loopback.py` is one), chosen in `Source.device` (`audio.py`) |
| a new helper process | a module under `helpers/` with a `main(argv)`, named in `HELPERS` (`helpers/__init__.py`), started by `Receiver._spawn_helper` |
| something the user must install or configure | raise `SetupError` (`errors.py`) with a message that says what to install; the command line prints it as one line |
| a schema change | the `.proto` under `schemas/`, then `python -m stadiacast.schema_compiler` |
