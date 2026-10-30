# Setup

How to put a Stadia Controller and the receiver on one network, obtain a certificate the
firmware accepts, run the receiver and read what it logs.

## The reference rig

```
 Internet ── router (192.168.1.1) ── Ethernet ── Mac (192.168.2.1 on bridge100) ~~ Wi-Fi hotspot ~~ controller (192.168.2.2)
```

The Mac shares its Ethernet connection over Wi-Fi (System Settings → General → Sharing →
Internet Sharing). macOS hands out `192.168.2.x` addresses on `bridge100`, with the Mac as
gateway and DNS server. The receiver runs on the Mac, spawns the DNS sink on port 53 and the
SNTP server on port 123, and listens on 443, 80 and 8008 (HTTPS and plaintext HTTP) plus UDP
40000 and 40001 for the two WebRTC sessions.

Every number above is a default of `tools/start_receiver.sh` and of `stadiacast`; see
`.env.example` and `stadiacast --help` to change them.

## Network

The only hard requirement is that **the controller resolves names through the receiver**.
The firmware looks up `*.googleapis.com`, `clients3.google.com`, `connectivitycheck.gstatic.com`,
`update.googleapis.com`, `play.googleapis.com` and `time.google.com`; the DNS sink answers the
redirected names with the receiver's IP (A records; AAAA gets an empty answer so the firmware
stays on IPv4) and forwards everything else to the real resolver (`--dns-upstream`).

Options, from simplest to most involved:

- **Hotspot hosted by the receiver's machine** (the reference rig). The machine is the
  controller's DNS server automatically.
- **Your existing Wi-Fi.** Make the router's DHCP hand out the receiver's IP as the DNS
  server for the controller (a static lease / per-client DNS option), or for the whole LAN:
  the DNS sink forwards every other name upstream, so the rest of the network keeps working.
- **Standalone DNS sink.** `python -m stadiacast.helpers.dns_sink --upstream <router>
  --redirect-ip <receiver> --log dns.jsonl` runs it without the receiver, for example on a
  different host.

Caveat: when a redirected name gets NXDOMAIN the firmware falls back to `8.8.8.8` and reaches
the real host through the NAT, which is why `--clearcut-block tls` (fail the TLS handshake)
is preferred over `--clearcut-block dns` (see [protocol.md](protocol.md#7-clearcut-and-the-power-off)).

Ports used by the receiver (all configurable):

| port | protocol | purpose |
|---|---|---|
| 53 TCP/UDP | DNS | the DNS sink (`--dns-upstream` spawns it) |
| 123 UDP | SNTP | time for the controller (`--sntp`, on in the `live` preset) |
| 443 TCP | HTTPS | every Google endpoint the firmware talks to |
| 80, 8008 TCP | HTTP | the local control endpoints `/_status`, `/_rumble`, `/_start_game` |
| 40000 UDP | STUN/DTLS | the display link of the first controller (`c0`) |
| 40001 UDP | STUN/DTLS | the game-streaming session of the first controller |
| 40002, 40003 UDP, ... | STUN/DTLS | the same pair for the second controller (`c1`), and so on: base port + 2 per controller |

On Linux, binding the ports below 1024 needs root, or `CAP_NET_BIND_SERVICE` granted to the
interpreter binary (`setcap cap_net_bind_service=+ep` on the file `.venv/bin/python3` links to).

## Certificate

The firmware validates the server chain against its built-in **Google Trust Services (GTS)**
roots and never checks the hostname, so a GTS certificate for any domain you own works for
every name the receiver impersonates. Self-signed certificates are rejected with the TLS alert
`unknown_ca`; the tests use one anyway (`--self-signed`) because the fake controller does not
check.

You need a domain name whose DNS records you can edit: the ACME challenge below is a TXT
record. Google's public CA issues certificates for free over ACME, with External Account
Binding (EAB) credentials you create once in any Google Cloud project (Cloud Shell works):

```sh
gcloud services enable publicca.googleapis.com          # once, in any GCP project
gcloud publicca external-account-keys create             # prints keyId + b64MacKey
brew install certbot                                     # or: pip install certbot
certbot certonly --manual --preferred-challenges dns \
    --server https://dv.acme-v02.api.pki.goog/directory \
    --email <you@example.org> --agree-tos --no-eff-email \
    --eab-kid=<keyId> --eab-hmac-key=<b64MacKey> \
    -d receiver.example.org --config-dir ./le --work-dir ./le --logs-dir ./le
```

Notes:

- GTS rejects accounts without a contact email, and the EAB flags need the `=` form: a
  `b64MacKey` starting with `-` is otherwise parsed as an option.
- Pass the **full chain** (`fullchain.pem`); the intermediate matters.
- Copy the result to `certs/fullchain.pem` and `certs/privkey.pem` (`chmod 600` the key).
  Both are ignored by git; never commit them. The `le/` directory certbot wrote can be deleted
  afterwards, or kept if you want to issue again later.

**No renewal needed.** The firmware checks that the chain leads to a GTS root; it does not
check the certificate's validity dates (confirmed on a real controller). Issue the certificate
once and keep the two files. `--clock-pin` is unrelated to this: it serves the controller a
chosen clock when you want one.

**The certificate's terms.** The receiver serves this certificate for Google's host names,
not for the domain it was issued to. Google Trust Services' policy (CPS §9.6.3) has
subscribers install TLS certificates only on servers reachable at the names in the
certificate, and the CA revokes certificates whose subscriber breaches the agreement. Use a
domain name dedicated to the receiver, so that a revocation would affect nothing else.
Whether the firmware checks revocation at all has not been tested; it checks neither the
hostname nor the validity dates.

## Running

`tools/start_receiver.sh` starts the receiver with `--preset live`, a fresh run directory
`captures/<stamp>/` and a packet capture of the whole hotspot interface when `/dev/bpf*` is
readable (`sudo chmod o+rw /dev/bpf*` once; `PCAP=0` skips it, `PCAP_FILTER` narrows it). It
reads the rig settings from the environment or from a `.env` file at the repo root (see
`.env.example`), and passes any extra arguments on to the receiver, where they override the
preset:

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
| `bridge100.pcap` | the packet capture, when enabled |

`GET http://<receiver>/_status` returns the live session state as JSON. `-v` echoes the bulky
log fields (request bodies, decoded protobufs) to the console too.

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
| `tls_handshake_failed` with `UNKNOWN_CA` | the controller rejected the certificate: not a GTS chain, or the key does not match |
| no `tcp_accept` at all | the controller is not resolving names through the receiver (check `dns.jsonl`) |
| `gmsg_unattributed` | a `/gmsg` poll from a controller that never registered with this receiver process (it will register on its next boot) |
| `http_request` to `/log/batch` then silence | the Clearcut power-off; make sure the `live` preset (`clearcut_block=tls`) is active |
| `display_rpc_unanswered` | the controller never answered `GetControllerStateRequest`; see protocol.md on the ResolveDiscovery trigger |
| `rpc_out_failed` | an RPC was attempted without a DTLS session |
| `game_start_failed` | the controller refused `StartGame` (it was not in BROWSE) |
| `rumble_refresh_failed` | the game session dropped while a rumble was held |

## Wi-Fi provisioning over Bluetooth

A controller that has never been set up (or was factory reset) advertises a Bluetooth LE
setup service and waits for Wi-Fi credentials: its status light blinks orange. If it does
not, hold the Options (⋯) and Google Assistant buttons together until it does.
`stadiacast-provision` then sends the credentials the way the Stadia app did:

```sh
stadiacast-provision scan                                 # find the controller (Fast Pair 0xFE2C)
stadiacast-provision provision --address <ADDR> --ssid <SSID>      # prompts for the password
```

The tool triggers a Wi-Fi scan on the controller, copies the security parameters it reports
for your network, encrypts the password with the controller's own RSA public key and writes
the credentials. The BLE link dropping right after the write is normal: the controller leaves
setup mode.

**What to expect on the first boot** (seen live on firmware `ota.282115`): the controller joins
the Wi-Fi, blinks white, resolves the Google names through the receiver and registers with it
(`controller_new` in the log), but a controller that has never been linked stops there. It
registers a second time, then idles (pinging the gateway every two seconds) until it powers
itself off after a few minutes. Wake it again: the second boot takes the normal autolinking
path and connects (two short rumbles, steady white). No link code has to be typed.

On macOS, Bluetooth addresses are CoreBluetooth UUIDs that can change between runs; `--service-data-match HEX` accepts the device by a fragment of its Fast Pair service data
instead (shown by `scan`). The password can also come from the `STADIA_WIFI_PW` environment
variable. `list-services` and `windows` dump the GATT table and the device's JSON windows for
debugging.
