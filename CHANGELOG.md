# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- A virtual gamepad on Linux: `--gamepad uinput` turns each controller into a wired Xbox 360
  controller for the games on the receiver's PC, through the kernel's uinput device, with
  nothing to install. SDL, which Steam and most Linux games read gamepads through, takes it
  for one, and a game's rumble comes back to the controller. The pad appears with the
  controller's first input and goes when the controller is switched off.
- `GET /_input`: what every controller is doing right now, in one small document: the state of
  its link, its address, battery and virtual pad, and the latest report with the buttons held
  by name, the sticks and the triggers. `GET /_events` sends the same document as server-sent
  events, one whenever something in it changes, for a program that shows or uses the input
  without reading the log.
- `POST /_shutdown` stops the receiver as Ctrl-C does, helper processes, headset streams and
  virtual pads included. It is taken over the loopback address only, so from the receiver's
  own computer, and is the way to stop a receiver that runs in the background on Windows.

### Fixed
- On Linux, `--audio-source system` sent the microphone to the headset instead of what the
  computer plays, on systems without ALSA's PulseAudio plugin. It now records the default
  output's monitor through the sound server's own `parec`, on PulseAudio and on PipeWire, in
  the stream's own format, and also when the receiver runs with `sudo`.
- `--audio-devices` ended in a traceback on Linux when PortAudio could not reach a sound
  server. It says so in one line, and so does a `device:NAME` source in the log.

## [1.1.1] - 2026-11-10

A wake connects about 15 seconds sooner: the controller no longer waits on the analytics
upload that the receiver refuses.

### Fixed
- A wake took about 15 s longer than it had to. The receiver refuses the controller's analytics
  upload, and it did so by closing the connection, after which the firmware waits 12 to 15 s
  before its next request. `--clearcut-block tls` (the `live` preset) now answers that upload's
  ClientHello with the TLS alert it was meant to get, and the controller goes on at once: from
  its first packet to ready, about 4 s where it took 19. The upload's retries, one a minute
  while a controller is on, no longer hold up the controller's other requests either.

## [1.1.0] - 2026-11-10

A virtual gamepad on macOS, provisioning that says how it ended and works without the
controller's Wi-Fi scan, a receiver that lets go of a controller that was switched off, and
limits on what one request, datagram or device on the network can cost it.

### Added
- A virtual gamepad on macOS: `--gamepad ojd` turns each controller into an Xbox controller for
  Mac games through the OpenJoystickDriver app (0.6.0-alpha.1), and the game's rumble comes back
  to the controller. `--gamepad-profile` chooses what the pad says it is and `--ojd-path` names
  the driver's executable. The driver is alpha software and this output is new.
- `stadiacast-provision provision` ends with the outcome: `Done` once the controller reported
  that it joined or dropped the Bluetooth link (which it does on leaving setup mode), and an
  error that says what to check when it reported a failure or gave no sign of joining.
- `--service-data-match` finds the controller without `--address`.
- A `tls_alert` record in the log when a client refuses the certificate, with the alert it sent
  (`UNKNOWN_CA` for a chain the controller does not accept).
- The protocol notes describe the Bluetooth setup service.

### Changed
- `stadiacast-provision provision` no longer has the controller scan for Wi-Fi networks first:
  it assumes WPA2-Personal (AES) when a password is given and an open network otherwise, and
  the controller finds the network by itself. This works on controllers that drop the Bluetooth
  link a few seconds after that scan. `--wifi-scan` brings the scan back, for networks with
  other security; `--wpa-auth` and `--wpa-cipher` set it by hand.
- `stadiacast-provision` exits with status 1 when the provisioning failed and 130 when
  interrupted, and refuses credentials no network can have (a name over 32 bytes, a WPA
  password outside 8 to 63 characters) before it touches Bluetooth.
- A controller that is switched off closes its link, and the receiver now lets go of it: no
  more reports and checks to its address, its headset stream stops and its virtual pad is
  unplugged, until its next boot.
- The control endpoints (`/_rumble`, `/_start_game`, `/_audio`) refuse what a browser sends on
  behalf of a web page: any request other than `GET` that carries an `Origin` header.
- A `file:` audio source has to be a file of this computer; a URL is refused.
- `--self-signed` with `--cert` and `--key` creates the lab pair only when both files are
  missing. One of them without the other is an error instead of being written over.
- An HTTP request has the idle timeout for its body and for its response to be taken, not only
  for its head, and the receiver serves 128 connections at a time.
- In the log: a run of `rtc_socket_error` records is written three times and then once in a
  hundred, `rtc_stats` only when something came in, and a request body is decoded up to its
  first 16 KiB (the whole body is in `bodies/` as before).
- pyOpenSSL 24.3 or newer is required; older versions failed at the first DTLS session.
- `tools/start_receiver.sh` lets the receiver detect its own address unless `ADVERTISE_IP` is
  set, shows the settings in effect and cleans up after a receiver that does not come up.
  `tools/stop_receiver.sh` stops the processes its pid files name, after checking what they
  are, and no longer every receiver on the machine.

### Fixed
- The setup guide tied the boot that never connects after provisioning to controllers that were
  never linked. Every controller does it, and the guide now says to turn the controller off and
  wake it instead of waiting for it to switch itself off.
- `stadiacast-provision` failed on Linux at every provisioning, refusing the credentials as too
  large for a Bluetooth packet size that BlueZ does not report.
- With `--wifi-scan`, `stadiacast-provision` reads the controller's public key before it starts
  the scan: some controllers drop the Bluetooth link a few seconds after that scan, and the key
  was the longest read left after it.
- `stadiacast-provision` reads windows of up to 24 pages instead of 16. What used to end in a
  traceback ends in one line: a window that arrives cut short, a Bluetooth failure, a key that
  is not an RSA key, scan results of another shape.
- A password typed at the prompt keeps its leading and trailing spaces.
- With eight controllers known, a ninth got an error on its first request, and the session
  forgotten to make room was the one in use, since only HTTP requests counted as activity.
  Requests of one controller that arrived together could open several sessions for it.
- The receiver's memory grew with every input event for as long as it ran, about 100 MiB per
  controller and hour of play.
- With `--dtls-role auto`, the default without the preset, the handshake could not complete
  when the controller's first packet came more than half a second after the pair was selected.
- On Windows, Ctrl-C left the helper processes, a running headset stream and the virtual pads
  behind; so did a start that failed half way on any system.
- The headset stream: a volume change while the stream was starting could leave two streams
  running; a controller that rebooted during a restart left a stream nobody could stop; a
  stream stopped while its sound device was opening left the device open; and an ffmpeg that
  could not run was started again twice a second for good.
- Malformed input could do more than be dropped: an Omaha request, a compressed input series
  or a DNS message could hold the receiver or its DNS sink for seconds to minutes, an ICE
  candidate with an invalid port closed that controller's socket for good, and a malformed
  STUN or RTP header raised an error instead of being ignored.
- A certificate that could not be loaded was reported as port 443 being held by another
  program; the error names the files. A port that needs privileges says so.
- UDP ports held by another program were found only when a controller showed up, which then
  never connected: the receiver now stops at start with one line that names the port.
- A log file that could no longer be written (a full disk) broke every link; the receiver now
  gives the file up and goes on.
- `stadiacast-provision --help` and `stadiacast-show-input --help` are plain text, and a bad
  value for `--http-ports`, `--rpc-wire-id`, `--rpc-fields` or `--offer-json` is reported with
  the flag it was given to.
- On PyPI, the links in the project description lead to the files of the release on GitHub.
- The README and the setup guide said that nothing leaves the network: the controller still
  makes two requests to Google at boot, its connectivity check and its configuration, which the
  DNS sink forwards. They also say now what to set on a network other than `192.168.2.x`.

## [1.0.1] - 2026-11-07

Fixes from the first run on a real Windows PC: the headset stream no longer pauses while the
computer is silent, a helper that cannot start is reported with the reason, and a lab run keeps
its certificate out of `certs/`.

### Fixed
- On Windows the headset stream stopped whenever the computer fell silent and resumed with the
  next sound, because WASAPI feeds a loopback only while something plays: the capture now
  renders silence on the speaker while it runs, and the stream is continuous as on the other
  platforms.
- A helper process that ends (the DNS sink, the SNTP server) is logged as `helper_exited` with
  the last lines it wrote, instead of passing for running; a helper whose port is taken says
  who usually holds it. On Windows that is the Windows Time service on UDP 123, which has to
  be stopped for the SNTP server to start.
- `--self-signed` wrote its lab pair to `certs/`, where every later run found it and served it
  in place of the built-in certificate, which the controller refuses. The lab pair now stays in
  the run directory unless `--cert`/`--key` name a place for it.

## [1.0.0] - 2026-11-06

Nothing to configure before the first run, sound on the headset, a virtual gamepad on Windows
and ready-made executables.

### Added
- A built-in certificate the controller accepts, so there is nothing to obtain: the receiver
  serves it when none is configured. Your own still wins with `--cert`/`--key` or a pair under
  `certs/`.
- Headset audio: `--audio-source` plays a test tone, a file, what the computer plays
  (`system`) or one capture device on a headset plugged into the controller.
  `--audio-bitrate`, `--audio-application`, `--audio-volume` and `--audio-follow-volume` tune
  it; `/_audio` starts, stops and adjusts the stream while the receiver runs, and `/_status`
  reports it under `playback`.
- A virtual gamepad on Windows: `--gamepad vigem` turns each controller into an Xbox 360
  controller for the games on the PC, through the ViGEmBus driver, with the game's rumble
  sent back to the controller; `/_status` lists the pads under `gamepads`.
- Standalone executables for Windows, macOS (Apple silicon) and Linux attached to every
  release: `stadiacast` and `stadiacast-provision`, Python included.
- The battery level, whether a headset is plugged in and the controller's hardware
  description, in `/_status` and in the log.

### Changed
- A port 80 or 8008 held by another program no longer stops the receiver; 443 is the only
  port the controller needs.
- Whatever is missing on the computer (ffmpeg, an audio device, the `audio` or `gamepad`
  extra, the ViGEmBus driver, a Windows that cannot host a pad) stops the receiver with one
  line that says what to install, instead of a traceback.

## [0.1.0] - 2026-10-29

First public version of Stadiacast: a LAN-side stand-in for the Stadia cloud endpoints that
keeps the Stadia Controller's Wi-Fi mode working with your own PC. Everything below was
confirmed against real controllers running the last Wi-Fi firmware, `ota.282115`.

### Added
- The receiver (`stadiacast`): the HTTPS boot endpoints (`generate_204`, `stadiaconfig`,
  Omaha, Clearcut), the cloudcast discovery RPCs, the GMsg push channel, protobuf WebRTC
  signaling, STUN/ICE, DTLS-SRTP, WireHeader2 framing and the display RPC state machine up to
  READY, with every button, stick and trigger decoded into a JSON Lines event log.
- The game session: `StartGame`, the proxied `InitializationRequest`, a second ICE/DTLS
  endpoint, input on its `0x66` channel and rumble (`POST /_rumble`) kept alive across the
  controller's haptics watchdog.
- Several controllers at once: one session per controller (own UDP ports, DTLS certificates,
  GMsg queue and display link), labelled `c0`, `c1`, ... in the log and in `/_status`;
  `?controller=` selects the target of `/_rumble` and `/_start_game`.
- `--preset live`: the flag set proven end to end, including the Clearcut TLS block that keeps
  the controller from powering off after its first analytics upload.
- Helper processes spawned by the receiver: a redirecting DNS sink and an SNTP server.
- `stadiacast-provision`: Wi-Fi provisioning over Bluetooth LE for a controller in setup mode.
- `stadiacast-show-input`: a per-controller summary of the input recorded in a run.
- `tools/start_receiver.sh` / `tools/stop_receiver.sh`: rig settings in `.env`, a fresh run
  directory per start, an optional packet capture.
- The protobuf schemas with their provenance (`src/stadiacast/schemas/`) and a small proto2
  compiler that keeps the hand-written `.proto` files and their descriptors in sync.
- A fake controller (`tests/fake_controller.py`) that reproduces the firmware's network
  behaviour, and a test suite that drives the whole path over loopback without hardware.
- Documentation: setup guide, protocol notes, architecture overview, contributing guide,
  security policy, code of conduct.
- MIT License, with a `NOTICE` for the three protobuf schemas reproduced from the firmware.

[Unreleased]: https://github.com/jkfran/stadiacast/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/jkfran/stadiacast/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/jkfran/stadiacast/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/jkfran/stadiacast/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/jkfran/stadiacast/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/jkfran/stadiacast/releases/tag/v0.1.0
