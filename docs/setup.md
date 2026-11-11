# Setup

How to put a Stadia Controller and the receiver on one network, what certificate the
receiver serves, how to run it and how to read what it logs.

## The reference rig

```
 Internet ── router (192.168.1.1) ── Ethernet ── Mac (192.168.2.1 on bridge100) ~~ Wi-Fi hotspot ~~ controller (192.168.2.2)
```

The Mac shares its Ethernet connection over Wi-Fi (System Settings → General → Sharing →
Internet Sharing). macOS hands out `192.168.2.x` addresses on `bridge100`, with the Mac as
gateway and DNS server. The receiver runs on the Mac, spawns the DNS sink on port 53 and the
SNTP server on port 123, and listens on 443, 80 and 8008 (HTTPS and plaintext HTTP) plus UDP
40000 and 40001 for the two WebRTC sessions.

`tools/start_receiver.sh` defaults to this rig: the upstream resolver `192.168.1.1`, the
controller at `192.168.2.2` and the capture on `bridge100` (`.env.example` changes them).
`stadiacast` itself listens on the ports above and assumes the controller at `192.168.2.2`,
and it detects its own address, the one on the interface that routes to the controller. It has
no default resolver: it runs the DNS sink only with `--dns-upstream`, and the SNTP server only
with `--sntp`, which `--preset live` turns on.

## Network

The only hard requirement is that **the controller resolves names through the receiver**.
`--dns-upstream <resolver>` spawns the DNS sink: it answers `*.googleapis.com`,
`googleapis.com` and `time.google.com` with the receiver's IP (A records; AAAA gets an empty
answer so the firmware stays on IPv4) and forwards every other name to that resolver.
`--dns-redirect` sets the names redirected besides the `googleapis.com` ones (comma
separated, default `time.google.com`: keep it in the list when you add a name). The
firmware's two other names, `connectivitycheck.gstatic.com` (its connectivity check,
`/generate_204`) and `clients3.google.com` (its configuration, `stadiaconfig`), are forwarded
like any other, so the controller still makes those two requests to Google at boot.

Options, from simplest to most involved:

- **Hotspot hosted by the receiver's machine** (the reference rig). The machine is the
  controller's DNS server automatically.
- **Your existing Wi-Fi.** Make the router's DHCP hand out the receiver's IP as the DNS
  server to the controller alone (a static lease or a per-client DNS option). Not to the whole
  LAN: the sink answers every device alike, so the others would get `*.googleapis.com` and
  `time.google.com` pointed at the receiver and their Google services would fail.
- **Standalone DNS sink**, for example on a different host, from any install:

  ```sh
  stadiacast --helper dns_sink --upstream <router> --redirect-ip <receiver> --log dns.jsonl \
      --redirect '*.googleapis.com' --redirect googleapis.com --redirect time.google.com
  ```

  Without `--redirect` it redirects only the two `googleapis.com` patterns, not
  `time.google.com`.

On a network other than `192.168.2.x`, give the receiver the controller's address, or any
address on its network, with `--controller-ip` (default `192.168.2.2`): the receiver takes its
own address from the interface that routes there, and the DNS and SNTP logs mark that
address's queries. When that picks the wrong interface, `--advertise-ip` sets the address the
DNS sink answers with and the WebRTC answers carry. `tools/start_receiver.sh` reads both from
`.env` (`CONTROLLER_IP`, `ADVERTISE_IP`).

The controller's Wi-Fi speaks WPA2 only: give the network it joins WPA2-Personal (AES) with
protected management frames off, not WPA3.

Caveat: when a redirected name gets NXDOMAIN the firmware falls back to `8.8.8.8` and reaches
the real host through the NAT, which is why `--clearcut-block tls` (fail the TLS handshake)
is preferred over `--clearcut-block dns` (see [protocol.md](protocol.md#7-clearcut-and-the-power-off)).

Ports used by the receiver:

| port | protocol | purpose |
|---|---|---|
| 53 TCP/UDP | DNS | the DNS sink (`--dns-upstream` spawns it) |
| 123 UDP | SNTP | time for the controller (`--sntp`, on in the `live` preset) |
| 443 TCP | HTTPS | the Google endpoints the DNS sink redirects |
| 80, 8008 TCP | HTTP | the local control endpoints `/_status`, `/_input`, `/_events`, `/_rumble`, `/_start_game`, `/_audio`, `/_shutdown` |
| 40000 UDP | STUN/DTLS | the display link of the first controller (`c0`) |
| 40001 UDP | STUN/DTLS | the game-streaming session of the first controller |
| 40002, 40003 UDP, ... | STUN/DTLS | the same pair for the second controller (`c1`), and so on: base port + 2 per controller |

On Linux, binding the ports below 1024 needs root, or `CAP_NET_BIND_SERVICE` granted to the
interpreter binary (`setcap cap_net_bind_service=+ep` on the file `.venv/bin/python3` links to).
Port 53 is often held already, by systemd-resolved or dnsmasq (NetworkManager runs one for a
hotspot it shares): the DNS sink then exits, and its `helper_exited` record says so.

On Windows, the Windows Time service holds UDP 123 while it runs, and the SNTP server cannot
start until it is stopped: `Stop-Service w32time` in an administrator PowerShell (Windows
starts it again at the next boot). `--no-sntp` runs without the SNTP server instead; the
firmware also takes the time from the receiver's HTTP `Date` header and `GetCurrentTime`
replies ([protocol.md](protocol.md#8-time)).

On Windows, when the controller sits on another subnet than the PC (a dedicated Wi-Fi network
routed to it), Windows Defender Firewall drops the inbound traffic by default: allow UDP 53
and 123, TCP 443 and the UDP range of the WebRTC ports (40000 and up, two per controller). A
blocked game port shows as a steady white controller whose StartGame fails after a few seconds.

## Certificate

The firmware validates the server chain against its built-in Google Trust Services (GTS)
roots and checks nothing else: not the hostname, not the validity dates, not revocation. Any
GTS-issued chain therefore answers every Google name, and a self-signed one is rejected with
the TLS alert `unknown_ca` (the tests use one anyway, `--self-signed`, because the fake
controller does not check).

**The built-in certificate.** With none configured, the receiver serves a GTS chain issued for
a domain dedicated to the project and revoked, with reason key compromise, before its key was
published: exactly enough for the controller and useless anywhere else. The log records a
`builtin_credential` event with its subject, issuer and serial.

**Bringing your own.** Pass `--cert` (PEM chain, leaf first) and `--key`, or put the pair
under `certs/` as `fullchain.pem` and `privkey.pem`; a pair there wins over the built-in one,
and a file named on the command line that does not exist is an error. Google's public CA
issues certificates for free over ACME for a domain whose DNS records you can edit. Use a
domain dedicated to the receiver, since the certificate is served for Google's names, and
issue it once: the firmware does not check the dates, so it never needs renewing.

```sh
gcloud services enable publicca.googleapis.com          # once, in any GCP project
gcloud publicca external-account-keys create             # prints keyId + b64MacKey
certbot certonly --manual --preferred-challenges dns \
    --server https://dv.acme-v02.api.pki.goog/directory \
    --email <you@example.org> --agree-tos --no-eff-email \
    --eab-kid=<keyId> --eab-hmac-key=<b64MacKey> \
    -d receiver.example.org --config-dir ./le --work-dir ./le --logs-dir ./le
```

Copy `fullchain.pem` (the full chain: the intermediate matters) and `privkey.pem`
(`chmod 600`) to `certs/`; both are ignored by git.

## Running

`tools/start_receiver.sh` starts the receiver in the background with `--preset live` and a
fresh run directory `captures/<stamp>/`. On macOS it adds a packet capture of the whole
hotspot interface (`IFACE`) when `/dev/bpf*` is readable: `sudo chmod o+rw /dev/bpf*` makes it
so until the next boot. `PCAP=0` skips the capture, `PCAP_FILTER` narrows it. It reads the rig
settings from the environment or from a `.env` file at the repo root (see `.env.example`), and
passes any extra arguments on to the receiver, where they override the preset:

```sh
tools/start_receiver.sh --start-game        # also start the game session once READY
tools/stop_receiver.sh
```

The run directory contains:

| file | what |
|---|---|
| `server.jsonl` | the event log: one JSON object per line, `ts` and `event` plus fields |
| `server.out` | the console echo of the log (one compact line per event) |
| `bodies/` | every request body the controller sent, as raw bytes |
| `dns.jsonl`, `sntp.jsonl` | the helper processes' logs |
| `<IFACE>.pcap` | the packet capture (macOS), when enabled |

`GET http://<receiver>/_status` returns the live state as JSON: per controller its session,
battery level, headset and headset stream (`playback`), and with `--gamepad` every virtual pad
under `gamepads`, with its XInput `slot` or the `error` it ended in; a macOS pad has no slot
and names its `kind` and `profile`. `-v` echoes the bulky log fields (request bodies, decoded
protobufs) to the console too.

`GET /_input` is the short version, for a program that shows or uses what the controllers do:
per controller the state of its link (`display_state`, `ready` or `input` once it is up), its
`ip`, the seconds since it was last heard from (`last_seen_s`), `battery_percent`, `headset`,
its virtual `gamepad`, and `input`, the latest report: the buttons held by name (`held`), the
sticks (`lx`, `ly`, `rx`, `ry`, -32768 to 32767, up is positive) and the triggers (`l2`, `r2`,
0 to 255). `GET /_events` sends the same document as server-sent events (`event: input`): one
at once, one whenever something in it changes, and one every 2 s otherwise. A program that
follows the controllers reads this stream instead of asking again and again, since every
request is a record in the log. Eight streams can be open at a time.

```sh
curl -s http://192.168.2.1/_input | jq '.controllers | map_values(.input.held)'
curl -N http://192.168.2.1/_events
```

`POST /_shutdown` stops the receiver as Ctrl-C does, with its helper processes, the headset
streams and the virtual pads. It is taken from the receiver's own computer only
(`curl -X POST http://127.0.0.1/_shutdown`) and is the way to stop a receiver that runs in the
background on Windows, where it cannot be sent a Ctrl-C.

`/_audio` controls the headset stream while the receiver runs: `GET` shows it, and a `POST`
names an action, `{"do": "start", "source": "system"}` (optionally with `volume` and
`application`), `{"do": "stop"}`, `{"do": "volume", "volume": 0.5}` or
`{"do": "mode", "mode": 2}`. With several controllers, `controller` names the target, as
for `/_rumble`.

### Waking the controller

Press the Stadia button for about one second and release at the first vibration. A full boot
takes roughly ten seconds: the light blinks while the controller joins the Wi-Fi and talks to
the receiver, then **two short rumbles and a steady light** mean it matched. Wakes alternate
between full boots and short *report wakes* (a blinking light, no rumble, a Clearcut upload
and back to sleep): press again. A 4 s hold of the Stadia button powers the controller off.

### Several controllers

Every controller that identifies itself gets its own session: the label `c0`, `c1`, ... on
every log line and in `/_status`, its own pair of UDP ports (see the table above), its own
DTLS certificates and GMsg queue. Controllers are told apart by the ids the firmware sends
(`controller-device-id` / `controller-cloud-device-id` headers, the `device_id` in request
bodies) and by the display token the receiver hands each of them, which the firmware echoes in
its GMsg polls; the address is the fallback. A controller that reboots keeps its label.

The control endpoints take the target when more than one controller is known:

```sh
curl http://192.168.2.1/_status | jq '.controllers | map_values({device_id, ip, display: .session.display.state})'
curl -d '{"left":40000,"right":40000,"ms":1500}' 'http://192.168.2.1/_rumble?controller=c1'
curl -d '{"left":0,"right":0,"controller":"c1"}' http://192.168.2.1/_rumble
```

`stadiacast-show-input` prints one section per controller.

### Reading the logs

```sh
stadiacast-show-input captures/<stamp>                       # every button press in order, axis ranges
jq -c 'select(.event=="input")' captures/<stamp>/server.jsonl
jq -c 'select(.event|test("rpc_|display_|game_"))' captures/<stamp>/server.jsonl
```

Events worth knowing when something does not work:

| event | meaning |
|---|---|
| `tls_alert` with `UNKNOWN_CA` | the controller rejected the certificate: not a GTS chain |
| no `tcp_accept` at all | the controller is not resolving names through the receiver (check `dns.jsonl`) |
| `gmsg_unattributed` | a `/gmsg` poll from a controller that never registered with this receiver process (it will register on its next boot) |
| `http_request` to `/log/batch` then silence | the Clearcut power-off; make sure the `live` preset (`clearcut_block=tls`) is active |
| `display_rpc_unanswered` | the controller never answered `GetControllerStateRequest`; see protocol.md on the ResolveDiscovery trigger |
| `rpc_out_failed` | an RPC was attempted without a DTLS session |
| `game_start_failed` | the controller refused `StartGame` (it was not in BROWSE) |
| `rumble_refresh_failed` | the game session dropped while a rumble was held |
| `audio_start_failed` | the `--audio-source` stream could not start (`spec` is the source, `error` says why): no ffmpeg with libopus on `PATH`, or the source was refused |
| `audio_source_invalid` | the `--audio-source` spelling is wrong, or the capture it names cannot be set up on this machine (no loopback device, missing extra) |
| `audio_capture`, `audio_capture_failed`, `audio_capture_ended` | the capture feeding ffmpeg (`system`, `device:`) started, could not start, or stopped (`error` says why); after a failure ffmpeg exits (`audio_ffmpeg_exited`) and both start again, until the stream is stopped |
| `gamepad_created`, `gamepad_failed` | `--gamepad`: a controller's virtual pad exists (`slot` is its XInput player on Windows; on macOS the driver's feed was started), or could not be created or stopped working (`error`, shown under `gamepads` in `/_status` too); the controller's next boot tries again |
| `gamepad_rumble_failed` | the game's rumble could not be forwarded (the game session was not up) |
| `controller_hardware` | one line at READY with what the controller says about itself: product, hardware and firmware builds, serial, headset |
| `controller_battery` | the battery gauge moved (`percent`); it steps down one percent at a time |
| `controller_audio_config` | the headset was plugged in or pulled (`headset`) |
| `port_busy` | port 80 or 8008 is held by another program; the receiver goes on without it, 443 is the only port the controller needs |
| `helper_exited` | the DNS sink or the SNTP server (`helper`) ended; `output` holds the last lines it wrote, which name the port another program holds |

## Wi-Fi provisioning over Bluetooth

A controller that has never been set up advertises a Bluetooth LE setup service and waits for
Wi-Fi credentials: its status light blinks orange. To put one that is already set up back into
setup mode, turn it off (hold the Stadia button until the light goes out), then hold Y and
press the Stadia button. `stadiacast-provision` then sends the credentials the way the Stadia
app did:

```sh
stadiacast-provision scan                                 # find the controller (Fast Pair 0xFE2C)
stadiacast-provision provision --address <ADDR> --ssid <SSID>      # prompts for the password
```

The tool reads the controller's RSA public key and writes the network's name and the password,
encrypted under that key; the controller then finds the network by itself. It assumes
WPA2-Personal (AES) when you give a password and an open network when you leave it blank. The
controller drops the Bluetooth link when it leaves setup mode with the credentials, and the
tool ends there with `Done`. If the controller cannot join (`STATE -> 0x4f`) or gives no sign
of joining, the tool ends with an error that says what to check.

For a network with other security, add `--wifi-scan`: the controller scans for Wi-Fi networks
first and the tool copies the security it reports for yours. Some controllers drop the BLE link
a few seconds after that scan; the tool then says so and names the options to give the security
by hand instead, without the scan: `--wpa-auth` and `--wpa-cipher` take the values listed in
`--help`.

**What to expect on the first boot:** the controller joins the Wi-Fi, blinks white, resolves
the Google names through the receiver and, within a minute of taking the credentials, registers
with it (`controller_new` in the log). That boot never connects, whether or not the controller
was linked before: the light keeps blinking white, and button presses only make it rumble. Give
it that minute, then turn the controller off (hold the Stadia button until the light goes out)
and wake it again: the second boot takes the normal autolinking path and connects (two short
rumbles, steady white). No link code has to be typed. Left alone, the controller turns itself
off about six minutes after it registered, and its next wake connects the same way.

On macOS, Bluetooth addresses are CoreBluetooth UUIDs that can change between runs;
`--service-data-match HEX` finds the device by a fragment of its Fast Pair service data instead
(shown by `scan`). The password can also come from the `STADIA_WIFI_PW` environment variable.
`list-services` and `windows` dump the GATT table and the device's JSON windows for debugging;
[protocol.md](protocol.md#11-wi-fi-provisioning-over-bluetooth-le-ble_provisionerpy) describes
the setup service.
