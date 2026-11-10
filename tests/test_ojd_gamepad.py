"""OJD's wire contract and real pipe/process lifecycle, without macOS or a HID driver."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from stadiacast.cli import parse_args
from stadiacast.config import Config
from stadiacast.errors import SetupError
from stadiacast.gamepad import Button, GamepadState
from stadiacast.ojd_gamepad import MAX_PENDING, OJDPad, find_ojd, to_ojd_frame
from stadiacast.virtual_gamepad import VirtualGamepads
from tests.helpers import no_log


def eventually(check, timeout=3):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true")
        time.sleep(0.005)


@pytest.fixture
def feed(tmp_path, monkeypatch):
    """A CLI peer that records frames, emits feedback, and can fail while idle."""
    script = tmp_path / "feed.py"
    script.write_text(
        """import json, os, sys, threading, time
from pathlib import Path
frames = Path(sys.argv[1])
exit_file = frames.with_suffix('.exit')
feedback_file = frames.with_suffix('.feedback')
poll_file = frames.with_suffix('.poll')
pending = []
lock = threading.Lock()
def control():
    last = ''
    while True:
        if exit_file.exists():
            print('Input Monitoring denied for the virtual HID device', file=sys.stderr, flush=True)
            os._exit(77)
        if feedback_file.exists():
            value = feedback_file.read_text()
            if value and value != last:
                if poll_file.exists():
                    with lock:
                        pending.append(value)
                else:
                    print(value, flush=True)
                last = value
        time.sleep(0.005)
threading.Thread(target=control, daemon=True).start()
for line in sys.stdin:
    json.loads(line)
    with frames.open('a') as f:
        f.write(line)
    with lock:
        for value in pending:
            print(value, flush=True)
        pending.clear()
"""
    )
    import stadiacast.ojd_gamepad as module

    real_popen = module.subprocess.Popen
    processes = []
    commands = []

    def popen(command, *, executable, **kwargs):
        commands.append((command, executable))
        path = tmp_path / f"frames-{len(processes)}.jsonl"
        process = real_popen([sys.executable, str(script), str(path)], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    pads = []

    def create(label="c0"):
        pad = OJDPad(label, "/signed/app", "sdl-compatible")
        pads.append(pad)
        path = tmp_path / f"frames-{len(processes) - 1}.jsonl"
        eventually(path.exists)
        return pad, path

    create.commands = commands
    yield create
    for pad in pads:
        pad.close()
    assert all(p.poll() is not None for p in processes)
    assert all(not thread.is_alive() for pad in pads for thread in pad._threads)


def reports(path):
    states = []
    for line in path.read_text().splitlines():
        state = json.loads(line)
        if not states or states[-1] != state:
            states.append(state)
    return states


def publish_feedback(path, message):
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(message))
    for attempt in range(50):
        try:
            temporary.replace(path.with_suffix(".feedback"))
            return
        except PermissionError:  # Windows, while the peer has the file open to read it
            if attempt == 49:
                raise
            time.sleep(0.002)


@pytest.mark.parametrize(
    ("button", "field", "name"),
    [
        (Button.A, "buttons", "South"),
        (Button.B, "buttons", "East"),
        (Button.X, "buttons", "West"),
        (Button.Y, "buttons", "North"),
        (Button.MENU, "buttons", "Start"),
        (Button.OPTIONS, "buttons", "Back"),
        (Button.L1, "buttons", "LeftShoulder"),
        (Button.R1, "buttons", "RightShoulder"),
        (Button.L3, "buttons", "LeftStickClick"),
        (Button.R3, "buttons", "RightStickClick"),
        (Button.STADIA, "buttons", "Guide"),
        (Button.DPAD_UP, "dpad", "DpadUp"),
        (Button.DPAD_DOWN, "dpad", "DpadDown"),
        (Button.DPAD_LEFT, "dpad", "DpadLeft"),
        (Button.DPAD_RIGHT, "dpad", "DpadRight"),
    ],
)
def test_positional_button_mapping(button, field, name):
    frame = to_ojd_frame(GamepadState(buttons=int(button)).to_xinput())
    assert frame[field] == [name]
    assert frame["dpad" if field == "buttons" else "buttons"] == []


def test_axis_endpoints_y_direction_and_independent_triggers():
    frame = to_ojd_frame(GamepadState(lx=-32768, ly=32767, rx=32767, ry=-32768, l2=255, r2=64).to_xinput())
    assert frame["axes"] == {
        "LeftStickX": -1,
        "LeftStickY": -1,
        "RightStickX": 1,
        "RightStickY": 1,
        "LeftTrigger": 1,
        "RightTrigger": 64 / 255,
    }
    neutral = to_ojd_frame(GamepadState().to_xinput())
    assert all(value == 0 for value in neutral["axes"].values())
    assert neutral["buttons"] == neutral["dpad"] == []
    assert to_ojd_frame(GamepadState(lx=100, ly=-100).to_xinput(7849, 8689)) == neutral


def test_brief_button_and_trigger_taps_reach_pipe_in_order(feed):
    pad, path = feed()
    pad.apply(GamepadState(buttons=int(Button.A)).to_xinput())
    pad.apply(GamepadState(l2=255).to_xinput())
    pad.apply(GamepadState().to_xinput())
    eventually(lambda: len(reports(path)) >= 4)
    states = reports(path)
    assert states[1]["buttons"] == ["South"]
    assert states[2]["buttons"] == [] and states[2]["axes"]["LeftTrigger"] == 1
    assert states[3]["axes"]["LeftTrigger"] == 0
    command, executable = feed.commands[0]
    assert command[0] == "ojd" and executable == "/signed/app"
    assert command[-2:] == ["--as", "sdl-compatible"] and "--json" in command


def test_stick_flood_coalesces_and_button_overflow_fails_explicitly(feed):
    pad, _ = feed()
    with pad._condition:  # prevent the consumer from draining while we simulate a burst
        pad._pending.clear()
        for value in range(1000):
            pad.apply(GamepadState(lx=value).to_xinput())
        assert len(pad._pending) == 1 and pad._pending[-1]["sThumbLX"] == 999
        for value in range(MAX_PENDING - 1):
            pad.apply(GamepadState(buttons=int(Button.A) if value % 2 == 0 else 0).to_xinput())
        with pytest.raises(SetupError, match="queue is full"):
            pad.apply(GamepadState(buttons=int(Button.B)).to_xinput())


def test_rumble_levels_duration_cancellation_and_stop(feed):
    pad, path = feed()
    rumbles = []
    pad.on_rumble(lambda left, right: rumbles.append((left, right)))
    feedback = path.with_suffix(".feedback")
    feedback.write_text(
        json.dumps(
            {
                "kind": "RumbleCommand",
                "type": "SetRumble",
                "intensities": {"lowFrequency": 65535, "highFrequency": 32896},
                "durationMilliseconds": 40,
            }
        )
    )
    eventually(lambda: len(rumbles) >= 2)
    assert rumbles == [(255, 128), (0, 0)]
    pad._feedback({"type": "SetRumble", "intensities": {"lowFrequency": 65535}, "durationMilliseconds": 100})
    old_generation = pad._rumble_generation
    pad._feedback({"type": "SetRumble", "intensities": {"highFrequency": 65535}})
    pad._stop_rumble(old_generation)
    assert rumbles[-1] == (0, 255)  # an old expiry cannot stop a newer held effect
    pad._feedback({"type": "StopRumble"})
    assert rumbles[-1] == (0, 0)


def test_exit_between_reports_reports_permission_error_and_reaps_process(feed):
    pad, path = feed()
    errors = []
    failed = threading.Event()

    def failure(error):
        errors.append(error)
        failed.set()

    pad.on_error(failure)
    path.with_suffix(".exit").touch()
    assert failed.wait(3)
    assert "Input Monitoring denied" in errors[0]
    with pytest.raises(SetupError, match="Input Monitoring denied"):
        pad.apply(GamepadState().to_xinput())
    pad.close()
    pad.close()  # idempotent even after the child died


def test_idle_held_input_keeps_rumble_renewals_flowing_without_pulsing(feed):
    """OJD exchanges feedback on frames; an idle CLI otherwise batches it at 500 ms.

    Simulate a game's bounded effects, each renewed before it ends (Chrome renews its
    100 ms ones every 100 ms; these last longer, so that a slow machine running the
    test does not let one lapse). With no new physical input, the motor must stay on
    and the held A button must stay held.
    """
    pad, path = feed()
    path.with_suffix(".poll").touch()
    pad.apply(GamepadState(buttons=int(Button.A), lx=20000).to_xinput())
    eventually(lambda: reports(path)[-1]["buttons"] == ["South"])
    rumbles = []
    pad.on_rumble(lambda left, right: rumbles.append((left, right)))
    for index in range(5):
        publish_feedback(
            path,
            {
                "type": "SetRumble",
                "intensities": {"lowFrequency": 65535},
                "durationMilliseconds": 400,
                "sequence": index,
            },
        )
        eventually(lambda index=index: len(rumbles) > index)
        assert all(value == (255, 0) for value in rumbles)
        time.sleep(0.1)
    assert all(value == (255, 0) for value in rumbles)
    assert reports(path)[-1]["buttons"] == ["South"]
    publish_feedback(path, {"type": "StopRumble"})
    eventually(lambda: rumbles[-1] == (0, 0))


def test_a_lost_bounded_rumble_renewal_still_expires(feed):
    pad, path = feed()
    path.with_suffix(".poll").touch()
    rumbles = []
    pad.on_rumble(lambda left, right: rumbles.append((left, right)))
    publish_feedback(
        path,
        {"type": "SetRumble", "intensities": {"lowFrequency": 65535}, "durationMilliseconds": 100},
    )
    eventually(lambda: rumbles == [(255, 0), (0, 0)])


def test_close_neutralizes_held_input_and_two_pads_are_isolated(feed):
    first, first_path = feed("c0")
    second, second_path = feed("c1")
    first.apply(GamepadState(buttons=int(Button.A)).to_xinput())
    second.apply(GamepadState(buttons=int(Button.B)).to_xinput())
    eventually(lambda: len(reports(first_path)) >= 2 and len(reports(second_path)) >= 2)
    assert reports(first_path)[-1]["buttons"] == ["South"]
    assert reports(second_path)[-1]["buttons"] == ["East"]
    first.close()
    assert reports(first_path)[-1] == to_ojd_frame(GamepadState().to_xinput())
    assert second._process.poll() is None


def test_flags_discovery_and_platform_error(tmp_path, monkeypatch):
    cfg = parse_args(["--gamepad", "ojd", "--gamepad-profile", "generic", "--ojd-path", "/some/ojd"])
    assert (cfg.gamepad, cfg.gamepad_profile, cfg.ojd_path) == ("ojd", "generic", "/some/ojd")
    assert parse_args([]).gamepad == "off"
    assert parse_args([]).gamepad_profile == "game-controller-compatible"
    with pytest.raises(SetupError, match="not found"):
        find_ojd(str(tmp_path / "missing"))
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SetupError, match="macOS only"):
        VirtualGamepads.create(Config(cert="x", key="y", gamepad="ojd"), no_log, None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("stadiacast.ojd_gamepad.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    with pytest.raises(SetupError, match="signed app"):
        find_ojd()


def test_feedback_that_cannot_be_read_costs_a_rumble_and_not_the_pad(feed):
    pad, path = feed()
    errors = []
    pad.on_error(errors.append)
    rumbles = []
    pad.on_rumble(lambda left, right: rumbles.append((left, right)))
    for line in ("not json", "[1, 2]", json.dumps({"type": "SetRumble", "intensities": {"lowFrequency": 70000}})):
        path.with_suffix(".feedback").write_text(line)
        time.sleep(0.05)
    publish_feedback(path, {"type": "SetRumble", "intensities": {"lowFrequency": 65535}})
    eventually(lambda: rumbles == [(255, 0)])
    pad.apply(GamepadState(buttons=int(Button.A)).to_xinput())  # the pad still takes input
    eventually(lambda: reports(path)[-1]["buttons"] == ["South"])
    assert errors == []


@pytest.mark.skipif(sys.platform == "win32", reason="a child that inherits the feed's pipes, the POSIX way")
def test_close_does_not_wait_for_whatever_else_holds_the_feeds_pipes(tmp_path, monkeypatch):
    """Something the driver's command started may keep its output pipes open after it has gone.
    Closing the pad then leaves the two readers to end with it, and returns."""
    script = tmp_path / "feed.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'])  # inherits stdout and stderr\n"
        "for line in sys.stdin:\n"
        "    pass\n"
    )
    import stadiacast.ojd_gamepad as module

    real_popen = module.subprocess.Popen

    def popen(_command, **kw):
        kw.pop("executable")
        return real_popen([sys.executable, str(script)], **kw)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    pad = OJDPad("c0", "/signed/app", "generic")
    time.sleep(0.3)
    started = time.monotonic()
    pad.close()
    assert time.monotonic() - started < 2.5
    assert pad._process.poll() == 0
