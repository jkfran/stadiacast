# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/jkfran/stadiacast/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jkfran/stadiacast/releases/tag/v0.1.0
