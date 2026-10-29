# Architecture

How the receiver is put together, for people who want to change it. [protocol.md](protocol.md)
explains *what* the controller does on the network; this page explains *where* the code that
answers it lives and how the pieces talk to each other.

## Processes

One `stadiacast` process serves every controller. It spawns two helpers next to itself (both
`python -m stadiacast.helpers.<name>`, logging JSON Lines into the run directory):

| process | module | why separate |
|---|---|---|
| the receiver | `stadiacast.cli` → `stadiacast.receiver` | HTTPS/HTTP listeners, every controller's UDP sockets, the event log |
| DNS sink | `stadiacast.helpers.dns_sink` | binds port 53; answers the Google names with the receiver's IP, forwards everything else |
| SNTP server | `stadiacast.helpers.sntp_server` | binds port 123; answers `time.google.com` with the host clock |

The helpers are plain blocking servers in their own processes so they keep answering while the
receiver's event loop is busy, and so the privileged-looking ports are isolated.

## Objects

```
Receiver                                   one per process (receiver.py)
 ├── HttpServer                            HTTPS :443 + HTTP :80/:8008, SNI callback (http_server.py)
 ├── Protos                                the protobuf schemas, loaded once (protos.py)
 ├── EventLog                              server.jsonl + console echo (log.py)
 └── ControllerSession  c0, c1, ...        one per controller (controller.py)
      ├── RtcEndpoint "input"              UDP socket of the display link (rtc/endpoint.py)
      │    ├── StunMessage / ICE checks    rtc/stun.py
      │    ├── DtlsSession + DtlsCertificate   rtc/dtls.py
      │    ├── SrtpContext (in / out)      rtc/srtp.py
      │    └── WireHeader2 framing         rtc/wire.py
      ├── display RPC state machine        GetControllerState → READY → input (controller.py)
      ├── InputDecoder                     InputProtocolMessage stream (input_protocol.py)
      └── GameSession                      StartGame, proxied RPCs, rumble (game_session.py)
           ├── RtcEndpoint "game"          second UDP socket, own certificate
           └── InputDecoder
```

**`Receiver`** owns what is shared and nothing that belongs to one controller. It routes HTTP
requests (`_route`), answers the boot-time endpoints itself (connectivity check, `stadiaconfig`,
Omaha, Clearcut) and hands everything that identifies a controller to that controller's session.
It is also the registry: `controller_for()` finds or creates the session for a request.

**`ControllerSession`** is one controller as the receiver sees it: the discovery RPCs
(`register`, `discover`), the GMsg queue and long poll (`queue_gmsg`, `gmsg_poll`), the
signaling answer, the display-link endpoint and its callbacks, the post-DTLS RPC state machine
(`_display_rpc_loop`, `_on_rpc`, `_answer_request`) and the input events. Its `session` dict is
what `/_status` publishes; the `display` sub-dict is reset on every new boot.

**`GameSession`** is the game phase of one controller: it sends `StartGame`, answers the
`InitializationRequest` the controller proxies through the display link, runs the second
ICE/DTLS endpoint, decodes input on sub-channel `0x66` and drives the rumble motors with the
haptics-watchdog refresh. It talks to its controller through `ctl.send_rpc`, `ctl.mark_input`
and `ctl.emit_input_event`.

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
against a receiver started in lab mode (see the README). Against a real controller the receiver
runs with `--preset live` on ports 53/80/123/443, which `tools/start_receiver.sh` sets up.

## Where to add things

| change | where |
|---|---|
| a new knob | a `Config` field, a flag in the matching group of `cli.py`, a CHANGELOG line |
| a new boot-time endpoint | `Receiver._route` and a handler next to `_omaha` / `_clearcut` |
| a new display RPC the controller sends | `ControllerSession._answer_request` |
| a new message on the game session | `GameSession._on_app_data` / `_on_rpc` |
| an output for the decoded input | consume `Receiver.wait_event()` / `publish_event()` events |
| a schema change | the `.proto` under `schemas/`, then `python -m stadiacast.schema_compiler` |
