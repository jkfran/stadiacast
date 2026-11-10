"""The virtual gamepad output: one pad per controller, XInput fields in, the game's rumble back."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import ClassVar

import pytest

from stadiacast.config import Config
from stadiacast.errors import SetupError
from stadiacast.gamepad import Button, GamepadState
from stadiacast.input_protocol import InputDecoder
from stadiacast.log import EventLog
from stadiacast.protos import Protos
from stadiacast.virtual_gamepad import BUTTON_BITS, MOTOR_SCALE, VirtualGamepads, wait_until_plugged_in
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, fake, make_receiver, no_log, single, wait_for


class _FakePad:
    """A pad that records what the receiver drives, in place of ViGEm."""

    created: ClassVar[list[_FakePad]] = []

    def __init__(self, label: str) -> None:
        self.label = label
        self.slot = len(_FakePad.created)
        self.applied: list[dict[str, int]] = []
        self.resets = 0
        self.closed = False
        self.rumble_cb = None
        _FakePad.created.append(self)

    def apply(self, xinput: dict[str, int]) -> None:
        self.applied.append(xinput)

    def reset(self) -> None:
        self.resets += 1

    def on_rumble(self, callback) -> None:
        self.rumble_cb = callback

    def close(self) -> None:
        self.closed = True


def _with_fake_pads(rx, factory=_FakePad):
    _FakePad.created.clear()
    rx.cfg.gamepad_deadzone = True
    rx.gamepads = VirtualGamepads(rx.cfg, rx.log, rx, factory)
    return rx


def test_both_channels_events_read_back_into_one_state() -> None:
    """The display link's event carries the buttons as an int and the triggers on their own; the game
    session's carries the buttons as a hex string and the triggers among the axes. Same report, same state."""
    want = GamepadState(buttons=int(Button.A | Button.STADIA), lx=20000, ly=-20000, rx=-5, ry=6, l2=255, r2=64)
    display = want.as_event(None)
    assert display["buttons"] == 0x11000 and display["triggers"] == {"l": 255, "r": 64}
    protos = Protos()
    payload = FakeController("127.0.0.1", 0, protos, log=no_log).gamepad_payload(
        buttons=want.buttons, lx=want.lx, ly=want.ly, rx=want.rx, ry=want.ry, lt=want.l2, rt=want.r2
    )
    (game,) = InputDecoder(protos).decode(payload)["events"]
    assert game["buttons"] == "0x00011000" and "triggers" not in game
    assert (game["axes"]["lt"], game["axes"]["rt"]) == (255, 64)
    assert GamepadState.from_event(display) == GamepadState.from_event(game) == want
    assert GamepadState.from_event({"axes": {}}) == GamepadState()  # what an event lacks is released or centred


def test_input_reaches_the_pad_and_rumble_comes_back(tmp_path) -> None:
    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path, start_game="auto", start_game_delay=0.05))
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        rumbles: list[tuple[int, int]] = []
        cs.game.set_rumble = lambda left, right, _ms=None, _framing=None: rumbles.append((left, right)) or 7  # type: ignore[method-assign]
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12, lx=20000, ly=-20000, lt=255)  # A, left stick, left trigger
            await wait_for(lambda: _FakePad.created and _FakePad.created[0].applied)
            pad = _FakePad.created[0]
            assert pad.label == "c0" and rx.log.counts["gamepad_created"] == 1
            x = pad.applied[-1]
            assert x["wButtons"] == 1 << 12 and x["bLeftTrigger"] == 255 and x["bRightTrigger"] == 0
            assert x["sThumbLX"] > 0 and x["sThumbLY"] < 0  # past the deadzone, no sign flip
            assert fc.send_input_rpc(buttons=0, lx=1000, ly=1000)  # inside XInput's left deadzone
            await wait_for(lambda: len(pad.applied) >= 2)
            assert pad.applied[-1]["wButtons"] == 0 and (pad.applied[-1]["sThumbLX"], pad.applied[-1]["sThumbLY"]) == (
                0,
                0,
            )
            assert rx.status()["gamepads"] == {"c0": {"slot": 0, "frames": 2, "rumbles": 0}}

            assert pad.rumble_cb is not None
            pad.rumble_cb(255, 128)  # the game's motors, as ViGEm reports them
            await wait_for(lambda: rumbles)
            assert rumbles == [(255 * MOTOR_SCALE, 128 * MOTOR_SCALE)] == [(65535, 32896)]
            assert rx.log.counts["gamepad_rumble"] == 1

            cs.reset_boot_state()  # a reboot lets go of everything
            assert pad.resets == 1
        finally:
            fc.close()
            await rx.stop()
        assert pad.closed

    asyncio.run(run())


def test_input_on_the_game_session_reaches_the_pad(tmp_path) -> None:
    """``--gamepad`` switches StartGame on, so in a game the input arrives on the game session, whose
    events carry the buttons as a hex string and the triggers among the axes. The pad gets the same
    XInput fields as from the display link, and the report is decoded and acked, not dropped."""

    async def run() -> None:
        rx = _with_fake_pads(
            make_receiver(
                tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1, start_game="auto", start_game_delay=0.05
            )
        )
        await rx.start()
        fc = await connect_firmware_flow(rx, game_mode=True, game_framing="raw")
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            await wait_for(lambda: single(rx).session["game"]["state"] == "game-started", 8)
            seq = fc.send_game_input(buttons=(1 << 12) | (1 << 16), lx=20000, ly=-20000, lt=255, rt=64)  # A, Stadia
            assert await fc.wait_game_ack(5) == seq
            await wait_for(lambda: _FakePad.created and _FakePad.created[0].applied)
            pad = _FakePad.created[0]
            x = pad.applied[-1]
            assert x["wButtons"] == (1 << 12) | 0x0400  # Stadia is XInput's guide bit
            assert (x["bLeftTrigger"], x["bRightTrigger"]) == (255, 64)
            assert x["sThumbLX"] > 0 and x["sThumbLY"] < 0
            seq = fc.send_game_input(buttons=0)
            assert await fc.wait_game_ack(5) == seq
            await wait_for(lambda: len(pad.applied) >= 2)
            assert pad.applied[-1]["wButtons"] == 0 and pad.applied[-1]["bLeftTrigger"] == 0
            assert rx.log.counts.get("game_input_undecodable", 0) == 0
            assert rx.status()["gamepads"] == {"c0": {"slot": 0, "frames": 2, "rumbles": 0}}
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_a_failed_pad_shows_its_error_and_is_retried_after_a_reboot(tmp_path) -> None:
    """A pad ViGEm refuses stays refused for that boot (not retried on every report) and says why in
    ``/_status``; the controller's reboot forgets the failure, and its next report gets a pad."""
    attempts: list[str] = []

    def refused_once(label: str) -> _FakePad:
        attempts.append(label)
        if len(attempts) == 1:
            raise RuntimeError("Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN")
        return _FakePad(label)

    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path), refused_once)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12)
            await wait_for(lambda: rx.log.counts.get("gamepad_failed"))
            error = "RuntimeError('Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN')"
            assert rx.status()["gamepads"] == {"c0": {"error": error}}
            assert fc.send_input_rpc(buttons=0)
            await wait_for(lambda: single(rx).session["display"]["input_rpc"] == 2)
            assert attempts == ["c0"] and rx.status()["gamepads"] == {"c0": {"error": error}}

            fc.close()
            fc = await connect_firmware_flow(rx)  # the controller boots again
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert rx.status()["gamepads"] == {}
            assert fc.send_input_rpc(buttons=1 << 13)
            await wait_for(lambda: _FakePad.created and _FakePad.created[0].applied)
            assert attempts == ["c0", "c0"] and _FakePad.created[0].applied[-1]["wButtons"] == 1 << 13
            assert rx.status()["gamepads"] == {"c0": {"slot": 0, "frames": 1, "rumbles": 0}}
            assert rx.log.counts["gamepad_failed"] == 1 and rx.log.counts["gamepad_created"] == 1
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_a_pad_windows_never_plugs_in_names_the_missing_driver(tmp_path) -> None:
    """The plug-in wait giving up is a ``SetupError``, shown as its plain text in the log and in
    ``/_status``, not as the repr of the driver's error."""

    def never_plugged(label: str) -> _FakePad:
        def refused() -> None:
            raise RuntimeError("Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN")

        wait_until_plugged_in(refused, timeout=0.0, sleep=lambda _: None)
        raise AssertionError(f"the wait returned for {label}")

    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path), never_plugged)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12)
            await wait_for(lambda: rx.log.counts.get("gamepad_failed"))
            error = rx.status()["gamepads"]["c0"]["error"]
            head = "Windows did not plug the pad in within 0 s (Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN)"
            assert error.startswith(head)
            assert error.endswith("which desktop Windows 10 and 11 have and Windows Server lacks")
            record = next(r for r in rx.log.records if r["event"] == "gamepad_failed")
            assert record["controller"] == "c0" and record["error"] == error
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_two_controllers_get_two_pads(tmp_path) -> None:
    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1))
        await rx.start()
        fa, fb = fake(rx, "fake-a"), fake(rx, "fake-b")
        try:
            await asyncio.gather(fa.boot(), fb.boot())
            await asyncio.gather(fa.start_udp(), fb.start_udp())
            await asyncio.gather(fa.discover(), fb.discover())
            await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg())
            await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg())
            fa.connect()
            fb.connect()
            await asyncio.gather(fa.wait_connected(10), fb.wait_connected(10))
            await asyncio.gather(
                asyncio.wait_for(fa.display_ready.wait(), 5), asyncio.wait_for(fb.display_ready.wait(), 5)
            )
            await asyncio.gather(fa.keepalive(), fb.keepalive())
            assert fa.send_input_rpc(buttons=1 << 13) and fb.send_input_rpc(buttons=1 << 15)
            await wait_for(lambda: len(_FakePad.created) == 2 and all(p.applied for p in _FakePad.created))
            by_label = {p.label: p.applied[-1]["wButtons"] for p in _FakePad.created}
            a, b = rx.find("fake-a"), rx.find("fake-b")
            assert a is not None and b is not None
            assert by_label == {a.label: 1 << 13, b.label: 1 << 15}
        finally:
            fa.close()
            fb.close()
            await rx.stop()

    asyncio.run(run())


def test_the_vigem_backend_is_windows_only_and_the_flags_parse() -> None:
    from stadiacast.cli import parse_args

    cfg = parse_args(["--gamepad", "vigem", "--gamepad-deadzone", "off"])
    assert (cfg.gamepad, cfg.gamepad_deadzone) == ("vigem", False)
    assert parse_args([]).gamepad == "off" and parse_args([]).gamepad_deadzone is True
    if sys.platform != "win32":
        with pytest.raises(SetupError, match="Windows only"):
            VirtualGamepads.create(Config(cert="x", key="y", gamepad="vigem"), EventLog(None, echo=False), None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown --gamepad"):
        VirtualGamepads.create(Config(cert="x", key="y", gamepad="bogus"), EventLog(None, echo=False), None)  # type: ignore[arg-type]
    assert set(BUTTON_BITS) == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15}


def test_a_missing_driver_or_extra_is_a_clear_error(tmp_path, monkeypatch) -> None:
    """On Windows, importing vgamepad connects to the ViGEm bus: without the driver the import itself
    raises. Both that and a missing package end in a message saying what to install."""
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "vgamepad.py").write_text("raise RuntimeError('Error: VIGEM_ERROR_BUS_NOT_FOUND')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "vgamepad", raising=False)
    cfg = Config(cert="x", key="y", gamepad="vigem")
    with pytest.raises(SetupError, match=r"ViGEmBus driver installed .*BUS_NOT_FOUND"):
        VirtualGamepads.create(cfg, EventLog(None, echo=False), None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "vgamepad", None)
    with pytest.raises(SetupError, match="gamepad extra"):
        VirtualGamepads.create(cfg, EventLog(None, echo=False), None)  # type: ignore[arg-type]


def test_the_first_report_waits_for_the_plug_in() -> None:
    """ViGEm refuses reports until Windows has plugged the new pad in; other errors are not retried."""
    calls: list[int] = []
    naps: list[float] = []

    def update() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN")

    wait_until_plugged_in(update, timeout=5.0, sleep=naps.append)
    assert len(calls) == 3 and naps == [0.1, 0.1]

    def broken() -> None:
        raise RuntimeError("Error: VIGEM_ERROR_BUS_NOT_FOUND")

    with pytest.raises(RuntimeError, match="BUS_NOT_FOUND"):
        wait_until_plugged_in(broken, timeout=5.0, sleep=naps.append)

    def never() -> None:
        raise RuntimeError("Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN")

    expected = r"within 0 s \(Error: VIGEM_ERROR_TARGET_NOT_PLUGGED_IN\).*Windows Server"
    with pytest.raises(SetupError, match=expected):
        wait_until_plugged_in(never, timeout=0.0, sleep=naps.append)


def test_brief_tap_survives_async_pad_creation(tmp_path) -> None:
    async def run() -> None:
        started, finish = threading.Event(), threading.Event()

        def delayed(label):
            started.set()
            assert finish.wait(3)
            return _FakePad(label)

        rx = _with_fake_pads(make_receiver(tmp_path), delayed)
        pads = rx.gamepads
        try:
            pads.on_event({"controller": "c0", **GamepadState(buttons=int(Button.A)).as_event(None)})
            await wait_for(started.is_set)
            pads.on_event({"controller": "c0", **GamepadState().as_event(None)})
            finish.set()
            await wait_for(lambda: _FakePad.created and len(_FakePad.created[0].applied) == 2)
            assert [x["wButtons"] for x in _FakePad.created[0].applied] == [int(Button.A), 0]
        finally:
            finish.set()
            tasks = list(pads._creating.values())
            pads.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_shutdown_during_creation_reaps_the_late_pad(tmp_path) -> None:
    async def run() -> None:
        started, finish = threading.Event(), threading.Event()

        def delayed(label):
            started.set()
            assert finish.wait(3)
            return _FakePad(label)

        rx = _with_fake_pads(make_receiver(tmp_path), delayed)
        pads = rx.gamepads
        pads.on_event({"controller": "c0", **GamepadState().as_event(None)})
        await wait_for(started.is_set)
        pads.close()
        finish.set()
        await pads.wait_closed()
        assert len(_FakePad.created) == 1 and _FakePad.created[0].closed
        assert pads.status() == {}

    asyncio.run(run())


def test_release_during_creation_does_not_replay_old_input(tmp_path) -> None:
    async def run() -> None:
        started, finish = threading.Event(), threading.Event()

        def delayed(label):
            started.set()
            assert finish.wait(3)
            return _FakePad(label)

        rx = _with_fake_pads(make_receiver(tmp_path), delayed)
        pads = rx.gamepads
        try:
            pads.on_event({"controller": "c0", **GamepadState(buttons=int(Button.A)).as_event(None)})
            await wait_for(started.is_set)
            pads.release("c0")
            pads.on_event({"controller": "c0", **GamepadState(buttons=int(Button.B)).as_event(None)})
            finish.set()
            await wait_for(lambda: len(_FakePad.created) == 2 and _FakePad.created[1].applied)
            assert _FakePad.created[0].closed and not _FakePad.created[0].applied
            assert _FakePad.created[1].applied[0]["wButtons"] == int(Button.B)
        finally:
            finish.set()
            tasks = list(pads._creating.values())
            pads.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_async_backend_error_is_visible_without_another_input_report(tmp_path) -> None:
    class FalliblePad(_FakePad):
        def on_error(self, callback):
            self.fail = callback

    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path), FalliblePad)
        pads = rx.gamepads
        try:
            pads.on_event({"controller": "c0", **GamepadState().as_event(None)})
            await wait_for(lambda: _FakePad.created and _FakePad.created[0].applied)
            pad = _FakePad.created[0]
            pad.fail("OJD service stopped")
            await wait_for(lambda: pads.status().get("c0", {}).get("error"))
            assert pads.status() == {"c0": {"error": "OJD service stopped"}}
            assert pad.closed
            pads.release("c0")
            assert pads.status() == {}
        finally:
            pads.close()

    asyncio.run(run())
