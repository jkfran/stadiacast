# Contributing

Thanks for helping keep the Stadia Controller alive. This page covers the development
workflow; [docs/architecture.md](docs/architecture.md) explains how the code is organised and
[docs/protocol.md](docs/protocol.md) what the firmware does and why the code answers the way
it does.

## Setting up

```sh
git clone https://github.com/jkfran/stadiacast.git
cd stadiacast
uv sync --all-extras          # or: python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[ble,passthrough]" --group dev
uvx pre-commit install        # optional: ruff, shellcheck and a private-key check on every commit
```

`make help` lists the day-to-day commands. Before you push:

```sh
make check        # ruff check + ruff format --check + shellcheck + mypy + schema check + pytest
make coverage     # the suite with a line and branch coverage report
```

CI runs the same on Linux and macOS with Python 3.11 to 3.14.

## Tests

`uv run pytest` runs in about 20 seconds and needs no hardware. `tests/fake_controller.py`
reproduces the firmware's network behaviour, so the end-to-end tests exercise the real code
path: HTTPS boot, discovery RPCs, GMsg, signaling, STUN, DTLS, the display RPC state machine,
input on both channels, the game session, rumble and the haptics watchdog.

- A change in protocol behaviour comes with a test against the fake controller. When the fake
  does not model the behaviour yet, teach it first (it is the written-down version of what
  the firmware does).
- The end-to-end files follow the protocol phases: `test_boot_flow.py` (display link up to
  input), `test_clearcut.py`, `test_game_session.py`, `test_multi_controller.py`,
  `test_control_endpoints.py` and `test_signaling_legacy.py`; `tests/helpers.py` builds the
  loopback receiver and the fakes. Unit tests are named after the module they cover
  (`test_rtc.py`, `test_protos.py`, `test_http.py`, …).
- `tests/test_schemas.py` guards the protobuf schemas: the hand-written `.proto` files must
  compile byte for byte to the committed `.descriptor` files, and every `.proto` rendering
  must list exactly the descriptor's fields.

## Testing against a real controller

The tests prove the receiver matches *our model* of the firmware; only a real controller
proves the model. When you have one:

- The controller is operated by a person. Wake it, watch the light and the rumble, then read
  the run directory (`captures/<stamp>/server.jsonl`, `stadiacast-show-input`). Iterate on what
  the controller did, not on what the code intended.
- Start from `--preset live`, the configuration proven end to end. Put experiments behind a
  **new flag that defaults to off**, so the preset keeps meaning "known to work".
- Report live findings with the firmware version, the `ready` record, the relevant log
  events and what the controller did. Never attach `captures/` wholesale or anything from
  `certs/`: captures contain your network details and request bodies.

## Code

- Python 3.11+, formatted and linted by [ruff](https://docs.astral.sh/ruff/) (`make format`),
  type-checked by mypy in strict mode on `src/`. Line length is 120. Protobuf message classes
  are created at runtime and typed `Any`; everything else carries full annotations.
- Every component logs through the one `EventLog`: `self.log("event_name", key=value, …)`.
  Event names are stable identifiers people grep for and tests assert on; add new ones rather
  than renaming, and keep the bulky fields (bodies, decoded messages) in the `BULKY` set so the
  console stays readable.
- Behaviour that the firmware forced on us gets a comment saying *why* (the observation), not
  just what. Keep session-specific details (dates, run names) out of the code; conclusions go
  in `docs/protocol.md`.
- Wire-visible strings (placeholder ids, `Server` header, SDP build number) are part of the
  tested protocol surface; change them deliberately.
- New configuration is a `Config` field with a comment, a CLI flag in `cli.py` in the matching
  argument group, and a line in the CHANGELOG.

### Schemas

The hand-written `.proto` files under `src/stadiacast/schemas/` are the source of truth
for the `gotham.*` packages. Edit the `.proto`, run `python -m stadiacast.schema_compiler`
to regenerate the `.descriptor`, and keep the comment that says where a field's meaning comes
from. The firmware-extracted schemas (`input`, `webrtc_sdp`, `yeti_clientanalytics`) are not
edited: their descriptors are the authority. They are Google's, reproduced for interoperability
only, and the only Google-authored material in the repository (see [NOTICE](NOTICE)).

## What not to contribute

Firmware images or dumps, decompiled or disassembled firmware, and anything copied from
Google's software or services. Protocol knowledge arrives as observed behaviour: what the
controller sent, when, and what it did in response, with the log records that show it.

## Pull requests

- One topic per pull request, with a short description of the behaviour change and, for
  protocol work, the log evidence.
- By submitting a pull request you agree that your contribution is licensed under the
  [MIT License](LICENSE).
- Add an entry under *Unreleased* in [CHANGELOG.md](CHANGELOG.md).
- CI must be green. Maintainers squash-merge.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Security issues go
through [SECURITY.md](SECURITY.md), not the public tracker.
