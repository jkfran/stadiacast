# Protocol notes

What the Stadia Controller's Wi-Fi firmware (`ota.282115`, codename Gotham) does on the
network, and how the receiver answers. Everything here was established by reading the firmware
and by live runs against a real controller; the last section separates what is confirmed from
what is assumed. The protobuf schemas referenced throughout live in
`src/stadiacast/schemas/` (see its README for provenance and naming).

Vocabulary: the **controller** is the device; the **display** is the party it connects to (a
Chromecast running Stadia in the original design, the receiver here); the **display link** is
the WebRTC session they share; the **game session** is the second session the controller opens
for game streaming.

## 1. Boot over HTTPS

The firmware speaks HTTP/1.1 only (`%s %s HTTP/1.1\r\nHost: %s:%d`), keeps connections alive,
posts protobuf bodies as `application/x-protobuf` and treats any status other than 200 as an
RPC failure. Its HTTPS requests are serialized through one client. In order:

| request | host | the receiver answers |
|---|---|---|
| `GET /generate_204` | `connectivitycheck.gstatic.com` | 204 |
| `GET /cast/chromecast/device/stadiaconfig?…` | `clients3.google.com` | `--stadiaconfig`: `passthrough` to Google (needs the `passthrough` extra), `file:PATH` or `json:{…}`; falls back to `{}` |
| `POST /service/update2` (Omaha XML) | `update.googleapis.com` | `<updatecheck status="noupdate"/>` per app |
| `POST /log/batch` (Clearcut `BatchedLogRequest`) | `play.googleapis.com` | a `LogResponse` with a 30 min wait, then `Connection: close` (see §7) |
| `POST /v1:FirmwareVersionCheck?key=…` | `cloudcast-pa.googleapis.com` | `requires_update = false` |
| `POST /v1:RegisterController?key=…` | `cloudcast-pa.googleapis.com` | see §2 |
| `POST /gmsg` | `cloudcast-gmsg-prod.googleapis.com` | see §3 |
| `POST /v1:DiscoverDisplay?key=…` | `cloudcast-pa.googleapis.com` | see §2 |
| `POST /v1:ResolveDiscovery?key=…` | `cloudcast-pa.googleapis.com` | empty 200 (see §5) |

The `v1:` requests carry `controller-device-id` and `controller-cloud-device-id` headers.

The receiver answers a request only when its host resolves to the receiver. Its DNS sink
redirects `*.googleapis.com` and, by default, `time.google.com` (`--dns-redirect`), and forwards
every other name to the real resolver: with the default settings the first two rows are fetched
from Google, and the receiver's answers for them apply once `connectivitycheck.gstatic.com` and
`clients3.google.com` are redirected to it too.

## 2. Discovery RPCs (`schemas/discovery_api.proto`)

The schemas were reconstructed from the firmware's nanopb field tables: numbers, wire types,
sizes and nesting are the firmware's, the names are ours. "Required" means the handler code
refuses to proceed without the field.

- **RegisterControllerRequest** `{device_id, cloud_device_id, attempt_id, discovery?,
  client_flag}`. The receiver answers **RegisterControllerResponse** with a placeholder
  `registration_id`, `poll_interval_ms = 1000` (required, must be > 0) and a
  `registration_token`. When the request carries a resumed `discovery` (the *autolinking*
  boot: `resume = true` plus the SDP offer, sent on every live boot so far, by a controller
  that was never linked too), the response must also carry the `display {display_id,
  display_token}` oneof, otherwise the controller logs to Clearcut and powers down. A request
  without a `discovery` must be answered without it (§11).
- **DiscoverDisplayRequest** `{device_id, typed_keys | resume, sdp_offer_bundle, app_id,
  attempt_id, …}`. Field 4 is a serialized `WebRtcSdpAndIceCandidates` with `sdp.type =
  SDP_OFFER`: **the controller is the WebRTC offerer.** The receiver records the offer, builds
  its answer bundle, queues it on GMsg and answers **DiscoverDisplayResponse** with placeholder
  ids (`display_token` is required and must be non-empty).
- **ResolveDiscoveryRequest** `{device_id, display_id, flag3, options}`: the body of the
  response is ignored; any 200 completes discovery (§5).

`--rpc-fields METHOD=JSON` overrides fields of a schema-built response; `--rpc-response
METHOD=FILE` replaces the whole body.

## 3. The GMsg push channel

The controller polls `POST /gmsg` with a `GmsgPollRequest {subscription, last_message{id}?,
ack}`, where `subscription` is the `display_token` it was given (confirmed live; the receiver
issues one per controller, `stadia-local-receiver-display-<n>`, and uses it to attribute the
poll). The receiver answers either an empty 200 (the firmware's "no message", event 10: it
polls again) or a `GmsgEnvelope {message {id, type = 42, [controller_command] {payload}}}`
where the payload is our `WebRtcSdpAndIceCandidates` answer bundle. `type` must be 42 and the
command rides in protobuf extension **269197544**. The controller acks a message by sending its
id as `last_message.id` on the next poll; the receiver re-sends until acked.

`--gmsg-hold SECONDS` holds an idle poll open waiting for a message (long-poll emulation; 20 s
in the `live` preset). `--gmsg-ack-release` can release the ACK poll early after DTLS connects;
measured live, that only made a queued Clearcut upload run sooner.

## 4. The WebRTC session (`schemas/webrtc_sdp.proto`, `rtc/`)

Signaling is protobuf, not text SDP: `RtcSdpOffer` / `RtcSdpAnswer` carry ICE ufrag and
password, the DTLS fingerprint, codecs, SSRCs and flags. The answer echoes what must match the
offer (`raw_dtls`, `delayed_acks`, `use_rtp_timestamps`, the first H264/VP9 video format and
the first audio format); `--boreal off` (in the `live` preset) answers without `use_boreal`.
Each bundle carries one ICE candidate `{sdp_mid "0", foundation "1", priority, ip, port}`; the
firmware also offers IPv6 link-local and ULA candidates, which the IPv4 socket skips.

- **ICE / STUN** (RFC 5389 subset): binding requests and responses with `MESSAGE-INTEGRITY`
  and `FINGERPRINT`; the first peer address that passes is selected. The receiver is the
  *controlled* agent when the controller offered. Our checks repeat every 2.5 s as keepalive;
  with `--ice-fast-checks S` (1 s in `live`) they repeat every `S` seconds until DTLS starts, because
  the controller's first DTLS flight has always followed one of our checks by about 0.12 s.
- **DTLS 1.2** with the `use_srtp` extension offering `SRTP_AES128_CM_SHA1_80`, keys exported
  with the `EXTRACTOR-dtls_srtp` label. The signaling has no `a=setup`; the firmware's own SDP
  template says `a=setup:passive`, yet on the display link the controller acts as the DTLS
  **client**, so the `live` preset uses `--dtls-role server`. `auto` becomes the server when a
  ClientHello arrives first and the client 0.5 s after the pair is verified otherwise.
- **SRTP** (RFC 3711 AES-CM/HMAC-SHA1 and RFC 7714 AES-GCM) is implemented for completeness;
  with `raw_dtls` (what the controller offers) no SRTP flows.

### WireHeader2 (`raw_dtls`)

With `raw_dtls` every DTLS application-data record is one message with an 8-byte header:

```
 byte 0      version nibble (high) must be 0; low nibble ignored
 byte 1      ignored
 bytes 2-3   message id, little endian
 bytes 4-6   total length including the header, little endian, 24 bit (must equal the record length)
 byte 7      ignored
```

| id | meaning |
|---|---|
| `0x66` | "streamer-input": one RTP packet (or, on the game session, a bare `InputProtocolMessage`) |
| `0x74` | an `RpcMessage` from the controller on the display link |
| `0x75` | an `RpcMessage` from the display (the receiver's `--rpc-wire-id`) |
| `0xFFFA` | the game session's own RPC channel id. **Dropped on the display link**: the controller only accepts ids 1 to 255 and the keepalive ids `0xFFFE` / `0xFFFF` ("Failed to parse WireHeader2") |

## 5. The display link after DTLS (`schemas/rpc_message.proto`, `schemas/controller_message.proto`)

Messages are `RpcMessage {request | response | notification}` envelopes with a `ticket` and a
`payload`; responses carry an absl `StatusCode` and an `error_message`. Payloads are
`DisplayToController` (what the display sends) or `ControllerToDisplay` (what the controller
sends), nanopb messages whose names are ours.

The sequence after `dtls-connected`:

1. The controller runs **OnConnected** about 0.5 s later: it registers its RPC reader and
   posts **`v1:ResolveDiscovery`**. A display RPC sent *before* that point is dropped and, in
   one live run, stalled the controller for good. The receiver therefore starts its RPCs when
   ResolveDiscovery arrives (`--display-rpc-trigger resolve`, with an 8 s fallback) or, as in
   the `live` preset, 0.6 s after DTLS (`--display-rpc-trigger dtls --display-rpc-delay 0.6`).
2. Our 200 ends discovery: the connection is handed to the controller's app
   (ACCEPT_COMMANDS).
3. The display sends **`GetControllerStateRequest`** (`DisplayToController` #2) until the
   controller answers with **`GetControllerStateResponse`** (`ControllerToDisplay` #2:
   `device_id`, …). A request that arrives before the hand-off is queued; re-sends in that
   window get `INTERNAL` (13) and are retried every 1.5 s.
4. **READY.** The controller now sends `GetCurrentTimeRequest` (#18) every 3 s and an empty
   keepalive (#14) every 1.5 s. The time reply must **echo `controller_time_us` unchanged**
   (the firmware computes `rtt = now - echo` and `CHECK`-fails when negative) plus
   `display_time_us`; `NOT_FOUND` (5) makes it stop syncing. Unknown requests are answered
   `UNIMPLEMENTED` (12), the controller's own reaction to requests it does not implement.
5. In READY the controller reports **input** (#3, below) and accepts **`StartGame`** (§6).
   `DisconnectRequest` (#8) is answered with an empty OK.

### Input report (`ControllerToDisplay` #3)

Sent as an `RpcNotification` whenever something changes; decoded by `stadiacast.gamepad`.

| field | meaning |
|---|---|
| `f1` buttons | bit 0-3 D-pad up / down / left / right, 4 Menu (≡, XInput START), 5 Options (⋯, XInput BACK), 6 L3, 7 R3, 8 L1, 9 R1, 10 Assistant, 11 Capture, 12 A, 13 B, 14 X, 15 Y, 16 Stadia |
| `f2`, `f3` | left stick X, Y: -32768 to 32767, Y positive = up |
| `f4`, `f5` | right stick X, Y: same range |
| `f6`, `f7` | L2, R2 triggers: 0 to 255 |

Bits 0 to 15 are the XInput `wButtons` layout; Stadia maps to the (undocumented) guide bit
`0x0400`; Assistant and Capture have no XInput equivalent.

### What the controller reports about itself

Three things the controller volunteers once the link is up, all seen on our rig; the receiver
keeps them in `/_status`.

- **`GetControllerStateResponse`** (the answer to #2) describes the hardware: `f12` the
  manufacturer (`Google, Inc.`), `f14` the product and region code (`GA00722-GB`), `f15` the
  hardware build (`gotham_pvt_3`), `f16` the firmware build (`282115`), `f17` the serial
  number (the one the Omaha request carries too), `f4` whether a plug is in the headset jack
  at that moment, and `device_info`, the sub-table below. The receiver logs them once as
  `controller_hardware` when the link reaches READY, and falls back to `f17` for the serial
  when no `controller-device-id` header came.
- **The battery gauge, `ControllerToDisplay` #10** (`notification10`, a float): the charge as a
  fraction. The first sample comes a few minutes after READY, then one step down per percent
  (0.76, 0.75, 0.74, ... every few minutes on a live controller) and never up while it runs
  on the battery. `/_status` shows it as `battery_percent`; every change is a
  `controller_battery` event.
- **The audio configuration, `ControllerToDisplay` #4** (`audio_configuration_changed`): sent
  within a second of the headset going in (`mode` 1) or out (`mode` 0); `/_status` shows it
  as `headset`. Its `device_info` is the controller advertising an audio receiver of its
  own: `f2` is the controller's IP address, `f6` a UDP port (9989) and `f4` a 16-byte key.
  That receiver is the path behind `SetAudioMode(mode=4)` (Opus, encrypted with that key); it
  is reported to stay silent when fed, so the receiver leaves it alone. The route that plays is
  the next section.

### Audio to the headset jack

The jack is fed by a normal RTP stream on the ICE pair the handshake selected, not through the
`0x66` sub-channel. Two things have to be true, and neither is visible in the SDP:

1. **`SetAudioModeRequest` (`DisplayToController` #16) with `mode=2, f3=true`.** The firmware's
   mode table maps 1, 2 and 4 to its audio paths and rejects 3 as
   `Unsupported audio mode: UDP_PCM`; only mode 2 (the WebRTC Opus route) reaches the jack.
   Send it before the first frame: without it the pad takes the packets and plays nothing.
2. **Media on the media 5-tuple.** `0x66` carries input reports and RPCs. Audio pushed into it
   is accepted by the socket and silently dropped by the firmware (measured on hardware).

```
Opus (20 ms, PT 111, 48 kHz stereo)
  -> RTP        own SSRC and sequence numbers, timestamp = slot * 960
  -> SRTP       srtp_out.protect_rtp(), keys from EXTRACTOR-dtls_srtp
  -> UDP        the candidate ICE selected (endpoint.send_rtp_packet)
```

Three more details, each found by getting it wrong on a real controller:

- **Own SSRC and sequence.** The input ACKs we send use `local_ssrc` / `tx_seq`. Sharing them
  makes the controller see one stream whose sequence jumps backwards, which it counts as loss.
- **Timestamps from the slot index.** ffmpeg starts at a random timestamp and does not hand
  frames over on a 20 ms grid. `slot * 960` keeps the timeline the jitter buffer expects: a
  slot with no frame stays a hole for it to conceal, instead of pulling the following frames
  forward (which is audible as crackle).
- **Pacing in the sender.** Frames are sent one per 20 ms slot, polling every 4 ms. Windows'
  default timer granularity is 15.6 ms, so a plain `sleep(0.02)` degenerates into 20/31 ms.

## 6. The game session (`schemas/streamer_message.proto`, `game_session.py`)

Game streaming is a second WebRTC session, and it is the only path to the rumble motors.

1. The display sends **`StartGame`** (`DisplayToController` #8 `{value: key (max 23 bytes),
   number: 1}`) once READY (`--start-game`, or `POST /_start_game`). The controller's app moves
   BROWSE → STARTING_GAME and holds the reply until GAME_STARTED (OK) or GAME_STOPPED (error);
   a `StartGame` outside BROWSE is refused with `FAILED_PRECONDITION`.
2. The controller's StreamingClient sends **`ProxyRequest`** (`ControllerToDisplay` #1 `{f1: 1,
   f2: key, body}`), whose body is an inner `RpcMessage` request carrying
   `StreamerRequest {initialization {version: 1, offer: RtcSdpOffer (fresh per-session
   certificate), f3, f4}}`.
3. The receiver answers (`DisplayToController` #1 `ProxyResponse {data}`) with an inner
   `RpcMessage` response holding `StreamerResponse {initialization {answer: RtcSdpAnswer,
   candidate: RtcIceCandidate}}` for its **game endpoint**: a second UDP socket, certificate
   and ICE credentials. The controller's decoder limits strings: ufrag 7, password 31,
   fingerprint 127; `sdp_mid` 7, `foundation` 11, IP 39 (an IP literal, not a name).
4. The controller opens the new UDP flow, runs STUN (or starts DTLS straight away) and
   completes DTLS as **client**. `dtls-connected` is its OnSessionConnected → GAME_STARTED,
   and the held `StartGame` is answered OK on the display link.
5. **Input** arrives on sub-channel `0x66`, as RTP in WireHeader2 or as the bare
   `InputProtocolMessage` (`--game-framing`, mirrored by default), and is acked with an
   `InputProtocolMessage {ack_sequence_number}` the same way.
6. **Rumble.** The display sends `InputProtocolMessage {input_message_series {messages [{
   input_feedback_message_v2 {gamepad_vibration {id, left_motor_speed, right_motor_speed}}}]}}`
   on `0x66`. Levels are absolute (0 to 65535) and held until changed, but the firmware's
   **haptics watchdog stops the motors 5 s after the last write**, so the receiver re-sends a
   held level every `--rumble-refresh` seconds (2 s) and ends a timed pulse with an explicit
   0/0. `POST /_rumble {"left", "right", "ms"?, "framing"?}` drives it.

Requests on the game session's RPC channel (`0xFFFA`, or `0x74`) are answered with an empty OK
(`--game-rpc-reply`).

## 7. Clearcut and the power-off

Every wake uploads analytics to `play.googleapis.com/log/batch` around 6 to 9 s of uptime. In
every logged session the upload was followed by radio silence within about 0.3 s: the
controller powers off before discovery completes. When the upload *fails fast*, the batcher
requeues it 60 s later and the boot continues. The `live` preset therefore uses
`--clearcut-block tls`: a ClientHello for SNI `play.googleapis.com` is answered within
milliseconds with a fatal `handshake_failure` alert and the close. (`--clearcut-block dns`
answers NXDOMAIN instead, but the firmware then falls back to `8.8.8.8` and reaches the real
host.) The receiver also closes the Clearcut connection after the `LogResponse`, because a
kept-alive connection there blocked the serial HTTP client from sending `ResolveDiscovery`.

The alert is what makes the failure fast. After it the firmware's HTTP client sends its next
request within a fraction of a second. When the connection is closed on the ClientHello with no
alert, the client sends nothing for 12 to 15 s, wherever in the boot the upload falls: first,
before `FirmwareVersionCheck`, or later, between DTLS and `ResolveDiscovery`. Measured on one
controller from its first packet to READY, a wake took 19 s with the bare close and 4.3 s with
the alert, which was 7.4 s after the controller got its address. The retries show the same
wait: one every 60 s after an alert, one every 75 s after a bare close.

## 8. Time

The firmware syncs its clock from `time.google.com` over SNTP (the receiver's SNTP server
answers with the host clock), and from the HTTP `Date` header and `GetCurrentTime` replies.
It does not check the server certificate's validity dates (confirmed on a real controller),
only that the chain leads to a GTS root. Revocation is not checked either: the ClientHello
carries no `status_request` extension, no CRL or OCSP traffic has ever been seen, and a
certificate revoked at the CA with reason keyCompromise was accepted exactly like a valid one
(confirmed on a real controller through boot, autolink, input and rumble). `--clock-pin
auto|ISO-DATE` serves a chosen time instead of the real one (`auto` is the midpoint of the
certificate's validity window); `--clock-pin real`, the default, serves the host clock.

## 9. The `live` preset

| setting | why |
|---|---|
| `clearcut_block = tls` | otherwise every wake powers off ~0.3 s after its first Clearcut upload (§7) |
| `rpc_wire_id = 0x75` | `0xFFFA` is dropped on the display link (§4) |
| `dtls_role = server` | the controller is the DTLS client on the display link |
| `display_rpc_trigger = dtls`, `display_rpc_delay = 0.6` | start the display RPCs 0.6 s after DTLS, when the controller's RPC reader exists |
| `ice_fast_checks = 1.0` | an ICE check every second until DTLS starts (the controller's ClientHello follows one of our checks) |
| `gmsg_hold = 20` | long-poll `/gmsg` so the answer bundle is delivered as soon as it is queued |
| `boreal = off` | answer without `use_boreal` |
| `rtcp_interval = 1.0` | display-side RTCP receiver reports once SRTP keys exist |
| `sntp = true` | answer the controller's SNTP queries |

## 10. Controller identity and several controllers

The firmware identifies itself in these places, all confirmed live:

| where | what |
|---|---|
| `controller-device-id` header on `v1:` requests | a short serial-like id (14 characters) |
| `controller-cloud-device-id` header | a UUID |
| `device_id` in `RegisterController`, `DiscoverDisplay`, `ResolveDiscovery` bodies | the same UUID |
| `GmsgPollRequest.subscription` | the `display_token` the receiver answered with |

A controller that has never been linked sends `client_flag = true` in its registration (a
linked one sends `false`). Its boots are otherwise the same, the one after Wi-Fi provisioning
included (§11).

The receiver keeps one session per controller, created at the first identified request and
found again by any of those ids, by the display token, or by the peer address as a last
resort. Each session has its own pair of UDP sockets (base port + 2 per controller), ICE
credentials and DTLS certificates, so nothing in the RTC layer has to demultiplex peers; the
candidate in each controller's answer bundle points it at its own port. A controller that
reboots is matched to its existing session, which is reset; other sessions are untouched.

## 11. Wi-Fi provisioning over Bluetooth LE (`ble_provisioner.py`)

A controller with no network to join (never set up, reset, or its network gone) pulses its
light orange and advertises a setup service over Bluetooth LE, which the Stadia app used to
hand it Wi-Fi credentials. `stadiacast-provision` does the same. The advertisement carries Fast
Pair service data (UUID `0xFE2C`).

The setup service (`0000fea0-…`) and what the tool uses of it:

| characteristic | use |
|---|---|
| `0328fe40-002f-11e5-87d0-0002a5d5c51b` | write: the `connect_to_network` JSON, below |
| `0228fe40-…` | write: settings JSON; `{}` starts a Wi-Fi scan |
| `0428fe40-…` | a JSON response stream (not used) |
| `0c7c6160-fab2-11e4-9fbb-0002a5d5c51b` | read and notify: the setup state, one byte |
| `0d7c6160-…` | read: the controller's IP address, `127.0.0.1` while it has none |
| `127c6160-…` | read and notify: OTA status JSON |

**Windows.** Larger values are read through pairs of characteristics, `XX6c6160-…` (control)
and `XX7c6160-…` (data): the page index goes to the control characteristic as two little-endian
bytes, the data characteristic is then read, 512 bytes a page, until a page comes back short or
empty. The pages together are gzip-compressed JSON. Window `1a` holds the crypto bundle, whose
`device_info.public_key` is the controller's RSA public key (2048 bits on the controllers seen;
SubjectPublicKeyInfo DER in base64); it has taken up to 13 pages. Window `15` holds the results
of the Wi-Fi scan, a list of `{ssid, bssid, frequency, signal_level, wpa_auth, wpa_cipher}`;
`19` holds device information and `1c` the device state.

**Credentials.** One JSON object, written with response to the connect characteristic:

```json
{"ssid": "…", "wpa_auth": 7, "wpa_cipher": 4, "enc_passwd": "…"}
```

`enc_passwd` is the password encrypted to the controller's key with RSA PKCS#1 v1.5, in
base64; an open network has none. `wpa_auth` and `wpa_cipher` name the network's security in
the controller's own enums (`wpa_auth` 1 open, 2 WEP, 5 WPA-PSK, 7 WPA2-PSK; `wpa_cipher`
1 none, 2 WEP, 3 TKIP, 4 CCMP) and have to match it. `bssid` and `frequency`, copied from a
scan entry, may be added; without them the controller finds the network by its name. A scan
has reported a macOS hotspot as `wpa_auth` 100 and `wpa_cipher` 100, and the controller joined
it with those values as it does with 7 and 4.

**States.** `0x14` and `0x16` are the start and the end of a scan. After the credentials come
`0x1e` (connecting), then `0x3c` (connected) or `0x4f` (the credentials were refused or the
join failed); `0x34` and `0x4e` report JSON the controller could not parse. A controller that
joined leaves setup mode and drops the Bluetooth link a few seconds after `0x1e`, often without
sending `0x3c` first, so the dropped link is the sign of success to rely on. When the link
lasts longer, an OTA status (`{"ota_status": 2, "ready_to_apply": false}`) and a state `0x46`
have followed `0x3c`; what `0x46` stands for is not known.

**The scan.** Some controllers (seen with firmware build 316382) drop the Bluetooth link a few
seconds after the scan that a write to the settings characteristic starts, before credentials
copied from its results can be written. `stadiacast-provision` therefore leaves the scan out
unless `--wifi-scan` asks for it, and reads the key before the scan when it does.

**The boot after provisioning.** With the credentials the controller joins and runs its network
check: `generate_204`, `stadiaconfig`, the Omaha update check, a Clearcut upload. About half a
minute later it joins again and a boot like any other begins: Clearcut,
`FirmwareVersionCheck`, `RegisterController` with the resumed discovery and the offer. This one
boot then leaves the usual path, whether or not the controller was ever linked. It resolves the
GMsg name but never polls it, and half a second after the first registration it registers
again without a `discovery` (`device_id`, a new `attempt_id`, `client_flag`). The answer to its
offer is never fetched. The light blinks white, and button presses make the controller rumble
and send nothing. Left alone it sends `ResolveDiscovery` 300 s after the registration and an
Omaha check 75 s later, then sleeps. Its next boot, after that or after it was turned off by
hand, is an ordinary autolinking boot and connects; no link code is involved.

That second registration has to be answered without the `display` oneof. In one live run with
the oneof in that response the controller polled GMsg at once and took the answer to the first
registration's offer. Its ICE checks then came from another UDP port than that offer's
candidate, it answered none of the receiver's checks and sent no DTLS record; 15 s later it
sent `ResolveDiscovery` and went back to setup mode (orange light, one rumble) while staying on
the network. The second registration, it seems, opens a peer connection of its own whose offer
is never sent.

## 12. Confirmed versus assumed

**Confirmed on the wire and in the firmware:**

- HTTP/1.1 only, protobuf bodies, status 200 for RPC success; the hosts, paths and API key
  query parameter of §1.
- The discovery RPC schemas: the three request types, the two response types whose required
  fields the firmware checks, the FirmwareVersionCheck pair, the GMsg poll and envelope
  (`type = 42`, extension 269197544) and the ack-by-id behaviour.
- The controller is the WebRTC offerer; the answer bundle reaches it as the GMsg payload.
- STUN with MESSAGE-INTEGRITY and FINGERPRINT; DTLS-SRTP with `SRTP_AES128_CM_SHA1_80` and
  the `EXTRACTOR-dtls_srtp` exporter; `raw_dtls` framing with WireHeader2 and the valid-id rule.
- The display RPC envelope and state machine of §5, including the ResolveDiscovery timing, the
  time echo check and the READY-only periodic requests.
- The input layout of `ControllerToDisplay` #3 (every button mapped live).
- The identifiers of §10, the autolinking boot (offer inside `RegisterController`) and the
  display token echoed as the GMsg subscription.
- The game session flow of §6, the InitializationResponse string limits, the haptics watchdog.
- Headset audio: Opus over SRTP on the media 5-tuple, gated by `SetAudioMode(mode=2)`, with
  its own SSRC, heard in the jack on a real controller.
- The battery gauge (#10) and the headset notification (#4) of §5, both seen live, and the
  hardware fields of `GetControllerStateResponse`.
- Omaha and Clearcut message formats, and the two ways a refused Clearcut handshake ends for
  the firmware (§7): at once after a TLS alert, after about 15 s without one.
- The Bluetooth setup service of §11: the windowed reads, the key, the credentials and the
  states, on networks with WPA2-PSK and CCMP.
- The boot after provisioning of §11, on a controller that was linked before and on one that
  never was.
- The certificate check: the chain must lead to a GTS root; hostname, validity dates and
  revocation are not checked (§8).

**Assumed (logged as `assumption=` on the record; changed with flags):**

- Field *meanings* in the hand-written schemas are inferred from how the firmware uses them;
  the placeholder ids and tokens in `RegisterControllerResponse` and `DiscoverDisplayResponse`
  only need to satisfy the firmware's required-field checks.
- The answer bundle delivery and re-send policy on GMsg.
- `DiscoverDisplay` as a request of its own, for a link code typed on the controller: no live
  boot has sent one. Every offer so far came inside `RegisterController`.
- The input RTP payload type (96) and the ack format (`InputProtocolMessage` with only
  `ack_sequence_number`, sent on our own SSRC).
- What the game session's RPC requests mean (they are answered with an empty OK).
- The security enums of §11 other than WPA2-PSK with CCMP (7 and 4), the one pair a controller
  has joined a network with here.
