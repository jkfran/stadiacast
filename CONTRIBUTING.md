# Contributing

Thanks for helping keep the Stadia Controller alive. This page covers the development
workflow; [docs/architecture.md](docs/architecture.md) explains how the code is organised and
[docs/protocol.md](docs/protocol.md) what the firmware does and why the code answers the way
it does.

## Setting up

The workflow runs on [uv](https://docs.astral.sh/uv/), `make check` included:

```sh
git clone https://github.com/jkfran/stadiacast.git
cd stadiacast
uv sync --all-extras          # .venv with the receiver, every extra and the dev tools
uvx pre-commit install        # optional: ruff, shellcheck and a private-key check on every commit
```

On Windows, set `VGAMEPAD_SKIP_VIGEMBUS_INSTALL=true` before the `uv sync`
(`$env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = "true"` in PowerShell): installing the `gamepad` extra
otherwise launches the ViGEmBus driver's installer.

`make help` lists the day-to-day commands. Before you push:

```sh
make check        # ruff check + ruff format --check + shellcheck + mypy + schema check + pytest
make coverage     # the suite with a line and branch coverage report
```

CI runs the same on Linux and macOS with Python 3.11 to 3.14, and the tests once more with
every direct dependency at the lowest version `pyproject.toml` allows; the manual Windows lab
workflow runs the suite, the Windows-only drivers and the Windows executables on a Windows
runner, and the manual Linux lab workflow the Linux pad against a real kernel. On a Linux
machine of your own, `STADIACAST_LINUX_LAB=1 uv run pytest tests/test_linux_lab.py` does the
same, as a user who may open `/dev/uinput`: a pad appears on the system while it runs, and A,
the left stick and the left trigger are what it presses.

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

The fake also runs by hand against a receiver started in lab mode, on loopback with a
self-signed certificate (written into the run directory, never under `certs/`) and high ports:

```sh
uv run stadiacast --self-signed --https-port 8443 --http-ports 8080 --udp-port 40000 \
    --game-udp-port 40001 --bind 127.0.0.1 --advertise-ip 127.0.0.1 --start-game
uv run python tests/fake_controller.py --host 127.0.0.1 --https-port 8443 --game 20
```

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
- New configuration is a `Config` field with a comment, a CLI flag in the matching argument
  group of `cli.build_parser`, its entry in the `Config(...)` call of `cli.parse_args`, which
  maps and renames the parsed flags (a flag left out of it is parsed and ignored), and a line in
  the CHANGELOG.

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
- CI must be green. Pull requests are rebase-merged, so every commit lands on `main` as it
  is.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Security issues go
through [SECURITY.md](SECURITY.md), not the public tracker.

## Releasing

1. Set the version in `src/stadiacast/__init__.py`, turn the *Unreleased* section of
   `CHANGELOG.md` into that version's section with the date, and update the comparison links
   at the bottom of the file: a line for the new version, and *Unreleased* compared from its
   tag.
2. Commit, then make an annotated tag and push both:
   `git tag -a vX.Y.Z -m "Stadiacast X.Y.Z" && git push origin main vX.Y.Z`.
3. The tag runs the Release workflow. It builds the executables for Windows, macOS and Linux,
   the sdist and the wheel, and creates a draft release of the tag with all of them attached
   and the CHANGELOG section as its notes. Do not create or publish a release by hand before
   it is done: publishing runs the Publish workflow, which takes the sdist and the wheel from
   the release and finds nothing while they are not attached yet.
4. Look the draft over and publish it (`gh release edit vX.Y.Z --draft=false --latest`, or in
   the web interface). The Publish workflow then uploads the release's sdist and wheel to PyPI
   as the trusted publisher registered there for the project (this repository, `publish.yml`,
   environment `pypi`; no token). When that run failed, for example because the release was
   published before its files were attached, `gh workflow run publish.yml -f tag=vX.Y.Z` runs
   it again once they are there; files already on PyPI are skipped.
5. `make exe` builds the executables for the current platform into `dist/`, for a check before
   tagging; `gh workflow run release.yml` builds all three as workflow artifacts without a tag.

These steps are this repository's. The Release workflow creates a release, and the Publish
workflow uploads to PyPI, only here; in a fork a tag builds the same files and keeps them as
workflow artifacts. A fork that publishes builds of its own gives them a version of its own,
for example a local label such as `1.1.1+name`, so that two different programs never carry the
same name and version.
