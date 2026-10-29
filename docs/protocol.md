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

## 2. Discovery RPCs (`schemas/discovery_api.proto`)

The schemas were reconstructed from the firmware's nanopb field tables: numbers, wire types,
sizes and nesting are the firmware's, the names are ours. "Required" means the handler code
refuses to proceed without the field.

- **RegisterControllerRequest** `{device_id, cloud_device_id, attempt_id, discovery?,
  client_flag}`. The receiver answers **RegisterControllerResponse** with a placeholder
  `registration_id`, `poll_interval_ms = 1000` (required, must be > 0) and a
  `registration_token`. When the request carries a resumed `discovery` (the *autolinking*
  boot of an already-linked controller: `resume = true` plus the SDP offer, seen on every
  live boot so far), the response must also carry the `display {display_id, display_token}`
  oneof, otherwise the controller logs to Clearcut and powers down.
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

## 4. The WebRTC session (`schemas/webrtc_sdp.proto`, `rtc.py`)

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
`--clearcut-block tls`: the TLS handshake for SNI `play.googleapis.com` is failed within
milliseconds of the ClientHello. (`--clearcut-block dns` answers NXDOMAIN instead, but the
firmware then falls back to `8.8.8.8` and reaches the real host.) The receiver also closes the
Clearcut connection after the `LogResponse`, because a kept-alive connection there blocked
the serial HTTP client from sending `ResolveDiscovery`.

## 8. Time

The firmware syncs its clock from `time.google.com` over SNTP (the receiver's SNTP server
answers with the host clock), and from the HTTP `Date` header and `GetCurrentTime` replies.
It does not check the server certificate's validity dates (confirmed on a real controller),
only that the chain leads to a GTS root. `--clock-pin auto|ISO-DATE` serves a chosen time
instead of the real one (for example the midpoint of the certificate's validity window).

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

The firmware identifies itself in three places, all confirmed live:

| where | what |
|---|---|
| `controller-device-id` header on `v1:` requests | a short serial-like id (14 characters) |
| `controller-cloud-device-id` header | a UUID |
| `device_id` in `RegisterController`, `DiscoverDisplay`, `ResolveDiscovery` bodies | the same UUID |
| `GmsgPollRequest.subscription` | the `display_token` the receiver answered with |

A controller that has never been linked sends `client_flag = true` in its registration (a
linked one sends `false`). On its first boot after Wi-Fi provisioning it registers with the
embedded offer, registers again without it, and then idles until it powers off; the next boot
proceeds like a linked controller's autolinking boot and connects. No link code is involved.

The receiver keeps one session per controller, created at the first identified request and
found again by any of those ids, by the display token, or by the peer address as a last
resort. Each session has its own pair of UDP sockets (base port + 2 per controller), ICE
credentials and DTLS certificates, so nothing in the RTC layer has to demultiplex peers; the
candidate in each controller's answer bundle points it at its own port. A controller that
reboots is matched to its existing session, which is reset; other sessions are untouched.

## 11. Confirmed versus assumed

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
- Omaha and Clearcut message formats.

**Assumed (logged as `assumption=` on the record; changed with flags):**

- Field *meanings* in the hand-written schemas are inferred from how the firmware uses them;
  the placeholder ids and tokens in `RegisterControllerResponse` and `DiscoverDisplayResponse`
  only need to satisfy the firmware's required-field checks.
- The answer bundle delivery and re-send policy on GMsg.
- The input RTP payload type (96) and the ack format (`InputProtocolMessage` with only
  `ack_sequence_number`, sent on our own SSRC).
- What the game session's RPC requests mean (they are answered with an empty OK).
