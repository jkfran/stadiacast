<p align="center">
  <img alt="Stadiacast: a local server for the Stadia Controller's Wi-Fi mode" src="https://raw.githubusercontent.com/jkfran/stadiacast/main/docs/images/logo.svg" width="560">
</p>

<p align="center">
  <a href="https://github.com/jkfran/stadiacast/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jkfran/stadiacast/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&amp;logoColor=white">
  <a href="https://pypi.org/project/stadiacast/"><img alt="PyPI" src="https://img.shields.io/pypi/v/stadiacast"></a>
  <img alt="Status: works end to end" src="https://img.shields.io/badge/status-works%20end%20to%20end-brightgreen">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

Google shut Stadia down in January 2023 and, with it, the cloud endpoints the Stadia
Controller's **Wi-Fi mode** depends on. Stadiacast is a small server you run on your own
network that stands in for those endpoints, so the controller you own keeps working with your
own PC: it joins your Wi-Fi, connects, stays on, reports every button, stick and trigger,
vibrates on request and plays sound on its headset jack.

> **Status:** works end to end with controllers running their Wi-Fi firmware (tested with
> `ota.282115`), several at once. Expect rough edges. Questions go to
> [Discussions](https://github.com/jkfran/stadiacast/discussions), bugs to the
> [issues](https://github.com/jkfran/stadiacast/issues); pull requests are very welcome.
> Switched your controller to Bluetooth mode? It comes back, see [Requirements](#requirements).

## Key features

- 🛠️ **No firmware modification.** The receiver speaks the controller's stock protocol as is,
  and never flashes anything.
- 🔒 **No account, no cloud service.** Input, rumble and headset audio stay between the
  controller and your PC.
- 📜 **Nothing to obtain.** The receiver ships a certificate the controller accepts: unpack,
  run, wake the controller.
- 🎮 **A PC gamepad on Windows.** Every controller becomes an Xbox 360 controller for your
  games, and the game's rumble reaches the controller.
- 🍎 **macOS gamepad output.** An experimental OpenJoystickDriver bridge exposes each
  controller to Mac games, with selectable SDL and Apple GameController profiles.
- 🎧 **Headset audio.** What the PC plays, or any file, streams to a headset plugged into the
  controller.
- 👥 **Several controllers at once**, each one its own player.

<p align="center">
  <img alt="The controller talks to Stadiacast over Wi-Fi, and Stadiacast feeds your games as an Xbox 360 pad" src="https://raw.githubusercontent.com/jkfran/stadiacast/main/docs/images/flow.svg" width="680">
</p>

## Quick start

```sh
pip install 'stadiacast[ble,audio,gamepad]'           # or unpack an executable from the releases page
stadiacast --preset live --dns-upstream 192.168.1.1   # your router: the DNS sink forwards the rest there
```

1. **Network.** Put the controller and the receiver on one network where the receiver's
   machine is the controller's DNS server; a hotspot hosted by that machine is the simplest
   ([docs/setup.md](docs/setup.md#network)).
2. **First boot only.** A controller fresh out of the box, or one that was reset, waits in
   Bluetooth setup mode for its Wi-Fi credentials:

   ```sh
   stadiacast-provision scan
   stadiacast-provision provision --address <ADDR> --ssid <your SSID>
   ```

3. **Start the receiver** with the command above; on Windows add `--gamepad vigem` for the
   [virtual gamepad](#virtual-gamepad-windows), or on macOS use
   [`--gamepad ojd`](#virtual-gamepad-macos-experimental). From a source checkout,
   `tools/start_receiver.sh` does the same in the background, with the settings of a `.env`
   file (see `.env.example`).
4. **Wake the controller** with a ~1 s press of the Stadia button, released at the first
   vibration. Two short rumbles and a steady light mean it found the receiver; a blinking
   light with no rumble is a short "report wake", so press again.
5. **Watch the input.** The console shows one line per report,
   `GET http://<receiver>/_status` shows the session state, the battery level and whether a
   headset is plugged in, and with an install from PyPI or from source
   `stadiacast-show-input captures/<stamp>` summarizes a run.
6. **Rumble.** Start with `--start-game`, since the game session is the only path to the
   motors, then post levels from 0 to 65535:

   ```sh
   curl -d '{"left":40000,"right":40000,"ms":1500}' http://<receiver>/_rumble   # pulse
   curl -d '{"left":65535,"right":20000}' http://<receiver>/_rumble              # hold
   curl -d '{"left":0,"right":0}' http://<receiver>/_rumble                     # stop
   ```

7. **More controllers** each get their own session, `c0`, `c1`, ... in the log and in
   `/_status`; name the target of a control request with `?controller=c1` or a `"controller"`
   field in the JSON body.
8. **Stop** with Ctrl-C, or `tools/stop_receiver.sh` for a receiver the start script launched.

`--preset live` is the flag set proven end to end; explicit flags override it, and
`stadiacast --help` lists every knob.

## How it works

1. **DNS.** The controller must use the receiver's machine as its DNS server. The receiver
   spawns a small DNS server that points the Google names it needs at itself and forwards
   everything else upstream.
2. **HTTPS.** The firmware validates the server certificate against its built-in Google Trust
   Services roots and checks neither the hostname, nor the dates, nor revocation, so one GTS
   chain, the one the receiver ships, lets it answer every Google name.
3. **WebRTC.** The controller offers a WebRTC session in its `DiscoverDisplay` request; the
   receiver answers over the GMsg push channel, terminates STUN and DTLS, and plays the
   display side of the post-DTLS RPC handshake until the controller reaches READY and starts
   reporting input.
4. **Game session.** On `StartGame` the controller opens a second session for game streaming.
   That session carries input too, and it is the only path to the controller's rumble motors.

[docs/protocol.md](docs/protocol.md) describes the protocol in detail and
[docs/architecture.md](docs/architecture.md) how the code is put together.

## Requirements

- A Stadia Controller running its Wi-Fi firmware (tested with `ota.282115`). A controller
  switched to Bluetooth mode can be flashed back with community tools; Stadiacast itself never
  flashes anything.
- A Windows, macOS or Linux computer on the controller's network, acting as its DNS server.
  The reference rig is a macOS Internet Sharing hotspot. On Linux, binding ports 53, 80, 123
  and 443 needs root or `CAP_NET_BIND_SERVICE`; on Windows the firewall may have to let the
  controller in ([docs/setup.md](docs/setup.md#network)).
- Nothing else for the basics: the receiver ships a certificate the controller accepts
  ([serving your own](docs/setup.md#certificate) is optional), and the executables include
  Python. An install from PyPI or from source needs Python 3.11 or newer.
- Optional: ffmpeg for [headset audio](#headset-audio), the ViGEmBus driver for the
  [virtual gamepad](#virtual-gamepad-windows) on Windows, `tcpdump` for packet captures.

## Install

**Executables.** Every [release](https://github.com/jkfran/stadiacast/releases) carries an
archive for Windows x86_64, macOS (Apple silicon) and Linux x86_64 with two programs,
`stadiacast` and `stadiacast-provision`, as single files, Python included. Unpack it and run
`stadiacast` from a terminal. The builds are not signed: macOS blocks each program at its
first start until you allow it in System Settings → Privacy & Security → Open Anyway, or
clear the quarantine flag of both with
`xattr -d com.apple.quarantine stadiacast stadiacast-provision`; Windows SmartScreen may warn.

**From PyPI**, with Python 3.11 or newer: `pip install 'stadiacast[ble,audio,gamepad]'` (or
`uv tool install 'stadiacast[ble,audio,gamepad]'`); the extras are optional, see below.

**From source**, with [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/jkfran/stadiacast.git
cd stadiacast
uv sync --all-extras
```

or with pip, `pip install -e ".[ble,passthrough,audio,gamepad]"`. An install from PyPI or
from source has a third command, `stadiacast-show-input`. The `ble` extra is the Bluetooth
provisioning, `audio` and `gamepad` are the [headset audio](#headset-audio) capture and the
[virtual gamepad](#virtual-gamepad-windows), and `passthrough` lets the receiver fetch the
controller's configuration (`stadiaconfig`) from Google for it, which only happens when
`clients3.google.com` is redirected to the receiver (not by default).
`tools/start_receiver.sh` finds the virtualenv on its own.

## Input

In READY the controller reports every change on the display link. The receiver decodes it
into events with named buttons, stick axes (-32768 to 32767, Y up) and triggers (0 to 255),
with the button bits in the XInput layout. The full report is in
[docs/protocol.md](docs/protocol.md#input-report-controllertodisplay-3).

## Headset audio

The receiver can play sound on a headset plugged into the controller. ffmpeg with libopus
encodes every source, so it has to be on `PATH`:

```sh
stadiacast --audio-source tone          # 440 Hz, proves the path without any audio setup
stadiacast --audio-source file:x.ogg    # anything ffmpeg can decode
stadiacast --audio-source system        # what this computer plays
stadiacast --audio-source device:NAME   # one capture device; --audio-devices lists them
```

The stream starts once the display link reaches READY. `--audio-bitrate` sets the Opus bitrate
(64 kbit/s by default), `--audio-application` favours delay (`lowdelay`, the default) or
music (`audio`), and `--audio-volume` the gain. `/_status` reports the stream under `playback`
and `/_audio` starts, stops and adjusts it while the receiver runs
([docs/setup.md](docs/setup.md#running)).

`system` and `device:NAME` capture sound and need the `audio` extra, which the executables and
`uv sync --all-extras` include (Linux also needs the PortAudio library, `apt install
libportaudio2`). On macOS, send the sound to a virtual output such as BlackHole
(`brew install blackhole-2ch`), alone or in a Multi-Output Device next to the speakers; the
volume keys do not act on it, so set the loudness in the app or with `--audio-volume`. On
Windows the default output's loopback is captured and the volume slider and mute reach the
headset (`--audio-follow-volume off` sends the raw mix). On Linux the default output's
PulseAudio monitor is read. How the stream rides the display link is in
[docs/protocol.md](docs/protocol.md#audio-to-the-headset-jack).

## Virtual gamepad (Windows)

`--gamepad vigem` turns each controller into an ordinary Xbox 360 controller for the games on
the receiver's PC:

```sh
stadiacast --gamepad vigem              # one virtual pad per controller
```

Only a driver can create a device that Windows and its games see as a controller, so this
needs one extra install, once and with an admin prompt: the
[ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) driver, the same one behind
DS4Windows, Sunshine and the other tools of this kind. Its author archived the project in 2023
and 1.22.0 is the final version; it still installs and works on current Windows 10 and 11. It
has to be desktop Windows, since Windows Server lacks the Xbox 360 controller driver the pad
binds to. Without the driver, the receiver stops at start-up with one line that says so;
installing the `gamepad` extra, from PyPI or from source, offers to run the driver's
installer.

Every report reaches the pad as XInput fields with the standard stick deadzones
(`--gamepad-deadzone off` for raw sticks); the Stadia button is the guide button, Assistant
and Capture have no XInput equivalent. The game's rumble comes back to the controller through
the game session, which `--gamepad` starts for that reason. Two controllers are players 1 and
2, and `/_status` lists the pads under `gamepads`.

## Virtual gamepad (macOS, experimental)

`--gamepad ojd` feeds each controller to the signed **OpenJoystickDriver** app through its
`virtual feed` interface. Install
[OpenJoystickDriver 0.6.0-alpha.1](https://github.com/xsyetopz/OpenJoystickDriver/releases/tag/0.6.0-alpha.1),
the MIT-licensed release, into `/Applications`, and open it. It supports Apple silicon and Intel
on macOS 12 or later. Allow **OpenJoystickDriver** in System Settings → Privacy & Security →
**Input Monitoring**, and grant **Accessibility** if the app requests it. Keep the app running.
Later OJD releases use a different license; the version linked here is the tested protocol target.

```sh
uv run stadiacast --preset live --dns-upstream "$ROUTER" --advertise-ip "$MAC_IP" \
  --gamepad ojd --gamepad-profile game-controller-compatible
```

Stadiacast finds `ojd` on `PATH` or the app in `/Applications` or `~/Applications`; installing
OJD's command-line symlink is optional. `--ojd-path PATH` selects another executable.

Choose the profile for the application, then restart the receiver and wake the controller:

| `--gamepad-profile` | initial target |
|---|---|
| `game-controller-compatible` (default) | Apple's GameController framework and compatible SDL games; Xbox Wireless Controller identity |
| `sdl-compatible` | alternative for Steam and SDL-based emulators; Xbox Adaptive Controller identity |
| `generic` | applications with configurable raw-HID bindings |

Check the layout in the actual game or browser: recognition and mappings differ between input
APIs, including in CrossOver. OJD is alpha software. On macOS 15.8.2 arm64, synthetic input
worked through system HID, Apple's GameController framework, Chrome's standard Gamepad API,
and Moonlight's bundled SDL2. The default profile also returned SDL2 rumble. The SDL-compatible
profile enumerated but delivered no input through that SDL2 build; the generic profile worked with an explicit SDL
mapping but did not provide rumble. These probes do not establish compatibility with every game.

For an SDL application that needs an explicit mapping, install OJD's CLI from its Settings,
then obtain the selected profile's mapping with `ojd virtual export-mapping generic` (or the
profile you chose) and pass it to the application's `SDL_GAMECONTROLLERCONFIG` environment.

Buttons, D-pad, both sticks and separate analog triggers are forwarded. Stadia becomes Guide;
Assistant and Capture are omitted by the shared XInput mapping. `--gamepad-deadzone off` sends
raw sticks. Rumble comes back through the game session, which `--gamepad` starts automatically.

Each controller owns one feed (OJD allows four CLI feeds). The feed closes on disconnect and is
created again on the next boot/input. Pipe I/O runs off the receiver's event loop; stick-only
updates are coalesced during bursts, while button and trigger transitions retain their order.
OJD holds every frame for at least 8 ms; Stadiacast paces submissions at up to 100 Hz.
When the controls are still, their held state is repeated every 20 ms to keep rumble feedback
flowing: OJD's 500 ms idle feedback cadence otherwise batches Chrome's 100 ms effect renewals
into short pulses. Bounded effects allow 40 ms of renewal jitter; explicit stop commands stop
immediately.

`/_status` includes each pad's `kind`, `profile`, submitted `frames`, `rumbles`, and any `error`;
`slot` is `null` on macOS. A `gamepad_created` record means the feed process started; an OJD
permission or service failure produces `gamepad_failed`, even if no more input arrives. After
fixing the reported issue, reboot the controller to retry.

## Development

```sh
uv sync --all-extras      # the receiver, its extras and the dev tools
make check                # ruff, shellcheck, mypy, the schema check and the tests
```

The tests need no hardware: `tests/fake_controller.py` reproduces the firmware's network
behaviour. [CONTRIBUTING.md](CONTRIBUTING.md) has the workflow, how to run the fake by hand
against a receiver in lab mode, and how to test against a real controller safely.

## Roadmap

1. **First-boot autoconnect:** detect a controller in BLE setup mode and provision it onto the
   network automatically (today `stadiacast-provision` is the manual version).
2. **Virtual gamepad on Linux:** Windows has ViGEm and macOS has the experimental OJD bridge;
   Linux (uinput) is open. Broader macOS game compatibility testing is welcome.

## Security and privacy

The receiver listens on your LAN with no authentication and is meant for a trusted network
such as a hotspot you host. It never contacts Google itself, except for the `stadiaconfig`
passthrough above. The controller still makes two requests to Google at boot, its
connectivity check and its configuration, which the DNS sink forwards
([docs/setup.md](docs/setup.md#network)). See [SECURITY.md](SECURITY.md).

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
