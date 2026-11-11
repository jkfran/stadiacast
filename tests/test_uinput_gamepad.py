"""The Linux pad without a kernel: the numbers it speaks to uinput with, the mapping of a report
to keys and axes, and the force feedback bookkeeping. ``test_linux_lab.py`` has the real device."""

from __future__ import annotations

import asyncio
import ctypes
import os
import struct
import sys
from typing import ClassVar

import pytest

from stadiacast import uinput_gamepad as uinput
from stadiacast import virtual_gamepad
from stadiacast.errors import SetupError
from stadiacast.gamepad import Button, GamepadState
from stadiacast.uinput_gamepad import (
    ABS_HAT0X,
    ABS_HAT0Y,
    ABS_RX,
    ABS_RY,
    ABS_RZ,
    ABS_X,
    ABS_Y,
    ABS_Z,
    BTN_A,
    BTN_MODE,
    BTN_SELECT,
    BTN_START,
    BTN_TL,
    BTN_X,
    BTN_Y,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    EVENT,
    Rumble,
    effect_levels,
    ioc,
    pad_values,
    report,
)
from stadiacast.virtual_gamepad import VirtualGamepads
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for

GENERIC = "x86_64"  # an architecture with the common layout of a request number, as Arm and RISC-V have
OTHER_LAYOUTS = ("alpha", "mips", "ppc", "powerpc", "sparc", "parisc")


def test_the_requests_carry_the_kernels_numbers():
    """The values of linux/uinput.h on a 64-bit system, where a wrong size is a request the
    kernel does not know."""
    sizes = {
        "uinput_setup": ctypes.sizeof(uinput._Setup),
        "uinput_abs_setup": ctypes.sizeof(uinput._AbsSetup),
        "ff_effect": ctypes.sizeof(uinput._Effect),
        "uinput_ff_upload": ctypes.sizeof(uinput._Upload),
        "uinput_ff_erase": ctypes.sizeof(uinput._Erase),
    }
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    if wide:
        assert sizes == {
            "uinput_setup": 92, "uinput_abs_setup": 28, "ff_effect": 48, "uinput_ff_upload": 104, "uinput_ff_erase": 12
        }  # fmt: skip
        assert (uinput._Effect.u.offset, uinput._Upload.effect.offset, uinput._Upload.old.offset) == (16, 8, 56)
    if struct.calcsize("l") == 8:
        assert EVENT.size == 24
    assert ioc("", "U", 1, 0, GENERIC) == 0x5501
    assert ioc("w", "U", 3, 92, GENERIC) == 0x405C5503
    assert ioc("w", "U", 4, 28, GENERIC) == 0x401C5504
    assert [ioc("w", "U", n, 4, GENERIC) for n in (100, 101, 103, 107)] == [
        0x40045564,
        0x40045565,
        0x40045567,
        0x4004556B,
    ]
    assert ioc("w", "U", 108, 8, GENERIC) == 0x4008556C
    assert (ioc("rw", "U", 200, 104, GENERIC), ioc("w", "U", 201, 104, GENERIC)) == (0xC06855C8, 0x406855C9)
    assert (ioc("rw", "U", 202, 12, GENERIC), ioc("w", "U", 203, 12, GENERIC)) == (0xC00C55CA, 0x400C55CB)
    assert ioc("r", "U", 44, 64, GENERIC) == 0x8040552C
    assert ioc("", "U", 1, 0, "") == 0x5501  # a system without uname: the common layout, never used
    if wide and not uinput.machine_name().lower().startswith(OTHER_LAYOUTS):  # what the module itself worked out
        assert (uinput.UI_DEV_SETUP, uinput.UI_BEGIN_FF_UPLOAD, uinput.UI_GET_SYSNAME) == (
            0x405C5503,
            0xC06855C8,
            0x8040552C,
        )


@pytest.mark.parametrize("machine", ["ppc64le", "ppc", "mips64", "mips", "sparc64", "alpha", "powerpc"])
def test_four_architectures_have_three_direction_bits(machine):
    """Alpha, MIPS, PowerPC and SPARC: thirteen size bits, and "none" as a bit of its own."""
    assert ioc("", "U", 1, 0, machine) == 0x20005501
    assert ioc("w", "U", 100, 4, machine) == 0x80045564
    assert ioc("r", "U", 44, 64, machine) == 0x4040552C
    assert ioc("rw", "U", 200, 104, machine) == 0xC06855C8


@pytest.mark.parametrize("machine", ["parisc", "parisc64"])
def test_pa_risc_has_read_and_write_the_other_way_round(machine):
    assert ioc("", "U", 1, 0, machine) == 0x5501
    assert ioc("w", "U", 100, 4, machine) == 0x80045564
    assert ioc("r", "U", 44, 64, machine) == 0x4040552C
    assert ioc("rw", "U", 200, 104, machine) == 0xC06855C8


def test_a_report_becomes_the_keys_and_axes_of_an_xbox_360_pad():
    held = Button.A | Button.Y | Button.L1 | Button.MENU | Button.OPTIONS | Button.STADIA | Button.DPAD_UP
    state = GamepadState(buttons=int(held | Button.DPAD_RIGHT), lx=20000, ly=-20000, rx=-32768, ry=32767, l2=255, r2=7)
    values = pad_values(state.to_xinput())
    pressed = {code for (kind, code), value in values.items() if kind == EV_KEY and value}
    assert pressed == {BTN_A, BTN_Y, BTN_TL, BTN_START, BTN_SELECT, BTN_MODE}
    assert {code for kind, code in values if kind == EV_KEY} == set(uinput.KEYS.values())
    axes = {code: value for (kind, code), value in values.items() if kind == EV_ABS}
    assert axes == {
        ABS_X: 20000,
        ABS_Y: 19999,  # down on the stick is up the axis: the complement, as the kernel's driver has it
        ABS_RX: -32768,
        ABS_RY: -32768,
        ABS_Z: 255,
        ABS_RZ: 7,
        ABS_HAT0X: 1,
        ABS_HAT0Y: -1,
    }
    assert set(axes) == set(uinput.AXES)
    # Stadia's own two buttons have no place on the pad
    plain = pad_values(GamepadState(buttons=int(Button.ASSISTANT | Button.CAPTURE)).to_xinput())
    assert not any(value for (kind, _code), value in plain.items() if kind == EV_KEY)
    # the D-pad's other half, and a stick at the other end of its range
    other = pad_values(GamepadState(buttons=int(Button.DPAD_DOWN | Button.DPAD_LEFT | Button.X), ly=32767).to_xinput())
    assert (other[EV_ABS, ABS_HAT0X], other[EV_ABS, ABS_HAT0Y], other[EV_ABS, ABS_Y]) == (-1, 1, -32768)
    assert other[EV_KEY, BTN_X] == 1 and other[EV_KEY, BTN_Y] == 0


def test_values_out_of_range_are_brought_into_it():
    wild = {"wButtons": 0, "bLeftTrigger": 999, "bRightTrigger": -4, "sThumbLX": 70000, "sThumbLY": -70000}
    values = pad_values({**wild, "sThumbRX": 0, "sThumbRY": 0})
    assert (values[EV_ABS, ABS_Z], values[EV_ABS, ABS_RZ]) == (255, 0)
    assert (values[EV_ABS, ABS_X], values[EV_ABS, ABS_Y]) == (32767, 32767)


def test_only_what_changed_is_written_and_a_report_ends_with_a_sync():
    def events(data: bytes) -> list[tuple[int, int, int]]:
        return [EVENT.unpack_from(data, at)[2:] for at in range(0, len(data), EVENT.size)]

    neutral = pad_values(uinput.NEUTRAL)
    first = events(report(neutral, {}))
    assert len(first) == len(uinput.KEYS) + len(uinput.AXES) + 1 and first[-1] == (EV_SYN, 0, 0)
    assert (EV_ABS, ABS_Y, -1) in first  # a centred stick's complement
    assert report(neutral, neutral) == b""
    pressed = pad_values({**uinput.NEUTRAL, "wButtons": 1 << 12, "bLeftTrigger": 40})
    assert events(report(pressed, neutral)) == [(EV_KEY, BTN_A, 1), (EV_ABS, ABS_Z, 40), (EV_SYN, 0, 0)]
    assert events(report(neutral, pressed)) == [(EV_KEY, BTN_A, 0), (EV_ABS, ABS_Z, 0), (EV_SYN, 0, 0)]


def test_an_effect_shakes_for_its_length_after_its_delay():
    rumble = Rumble()
    rumble.upload(0, 0xC000, 0x4000, 200, 50, 9.0)
    assert rumble.levels(10.0) == (0, 0) and rumble.next_change(10.0) is None  # uploaded is not played
    rumble.play(0, 1, 10.0)
    assert rumble.levels(10.0) == (0, 0) and rumble.next_change(10.0) == pytest.approx(10.05)
    assert rumble.levels(10.05) == (0xC000, 0x4000) and rumble.next_change(10.05) == pytest.approx(10.25)
    assert rumble.levels(10.249) == (0xC000, 0x4000)
    assert rumble.levels(10.25) == (0, 0) and rumble.next_change(10.25) is None


def test_an_effect_without_a_length_shakes_until_it_is_stopped():
    rumble = Rumble()
    rumble.upload(3, 0xFFFF, 0, 0, 0, 5.0)
    rumble.play(3, 1, 5.0)
    assert rumble.levels(5.0) == (0xFFFF, 0) and rumble.next_change(5.0) is None
    assert rumble.levels(5000.0) == (0xFFFF, 0)
    rumble.play(3, 0, 5001.0)
    assert rumble.levels(5001.0) == (0, 0)
    rumble.play(9, 1, 5002.0)  # a slot nothing was uploaded to
    assert rumble.levels(5002.0) == (0, 0)


def test_an_effect_played_several_times_runs_that_often():
    rumble = Rumble()
    rumble.upload(1, 1000, 2000, 100, 0, 0.0)
    rumble.play(1, 3, 0.0)
    assert rumble.levels(0.0) == (1000, 2000) and rumble.next_change(0.0) == pytest.approx(0.3)  # no gap between runs
    assert rumble.levels(0.299) == (1000, 2000) and rumble.levels(0.3) == (0, 0)
    rumble.upload(2, 1000, 2000, 100, 100, 1.0)
    rumble.play(2, 2, 1.0)
    seen = [(rumble.levels(1.0 + at) != (0, 0), rumble.next_change(1.0 + at)) for at in (0.0, 0.1, 0.2, 0.3)]
    assert [shaking for shaking, _next in seen] == [False, True, False, True]
    assert [due for _shaking, due in seen] == [pytest.approx(due) for due in (1.1, 1.2, 1.3, 1.4)]
    assert rumble.levels(1.4) == (0, 0) and rumble.next_change(1.4) is None


def test_effects_add_up_the_gain_scales_them_and_an_erased_one_stops():
    rumble = Rumble()
    rumble.upload(0, 0x8000, 0x1000, 0, 0, 0.0)
    rumble.upload(1, 0x9000, 0x1000, 0, 0, 0.0)
    rumble.play(0, 1, 0.0)
    rumble.play(1, 1, 0.0)
    assert rumble.levels(1.0) == (0xFFFF, 0x2000)  # the sum stays in the motor's range
    rumble.gain = 0x7FFF
    assert rumble.levels(1.0) == (0x3FFF + 0x47FF, 0x7FF + 0x7FF)
    rumble.gain = 0xFFFF
    rumble.erase(1)
    assert rumble.levels(1.0) == (0x8000, 0x1000)
    rumble.upload(0, 0x100, 0x200, 0, 0, 1.0)  # replaced while it plays: it goes on with the new values
    assert rumble.levels(1.0) == (0x100, 0x200)
    rumble.erase(0)
    assert rumble.levels(1.0) == (0, 0)


def test_an_effect_changed_while_it_plays_starts_its_run_again():
    """What the kernel does for a pad with two motors: a program that keeps a short effect alive
    by sending it again gets rumble for as long as it does."""
    rumble = Rumble()
    rumble.upload(0, 0xFFFF, 0xFFFF, 300, 0, 0.0)
    rumble.play(0, 1, 0.0)
    rumble.upload(0, 0x8000, 0x8000, 300, 0, 0.2)
    assert rumble.levels(0.2) == (0x8000, 0x8000) and rumble.next_change(0.2) == pytest.approx(0.5)
    assert rumble.levels(0.3) == (0x8000, 0x8000)  # past the first upload's end
    assert rumble.levels(0.499) == (0x8000, 0x8000) and rumble.levels(0.5) == (0, 0)
    rumble.upload(0, 0x8000, 0x8000, 300, 0, 0.6)  # after it ran out: stored, not played
    assert rumble.levels(0.6) == (0, 0)

    rumble.upload(1, 0x1000, 0, 100, 100, 1.0)  # two runs with a delay before each
    rumble.play(1, 2, 1.0)
    rumble.upload(1, 0x2000, 0, 100, 100, 1.25)  # changed in the second run's delay: one run is left
    seen = [rumble.levels(1.25 + at)[0] for at in (0.0, 0.1, 0.19, 0.2)]
    assert seen == [0, 0x2000, 0x2000, 0] and rumble.next_change(1.45) is None


def test_a_periodic_effect_is_played_as_rumble_on_both_motors():
    def effect(kind: int, **fields: int) -> uinput._Effect:
        made = uinput._Effect(type=kind)
        for name, value in fields.items():
            setattr(made.u.rumble if name.endswith("_magnitude") else made.u.periodic, name, value)
        return made

    assert effect_levels(effect(uinput.FF_RUMBLE, strong_magnitude=0xC000, weak_magnitude=0x10)) == (0xC000, 0x10)
    assert effect_levels(effect(uinput.FF_PERIODIC, waveform=uinput.FF_SINE, magnitude=0x4000)) == (0x8000, 0x8000)
    assert effect_levels(effect(uinput.FF_PERIODIC, magnitude=-0x4000)) == (0x8000, 0x8000)
    assert effect_levels(effect(uinput.FF_PERIODIC, magnitude=0x7FFF)) == (0xFFFF, 0xFFFF)
    assert effect_levels(effect(uinput.FF_PERIODIC, magnitude=-0x8000)) == (0xFFFF, 0xFFFF)
    assert effect_levels(effect(0x52)) is None  # a constant force: not something two motors do


def test_the_pad_is_for_linux_and_says_what_keeps_the_device_shut(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        with pytest.raises(SetupError, match="--gamepad uinput is Linux only"):
            uinput.check_available()
        with pytest.raises(SetupError, match="--gamepad uinput is Linux only"):
            uinput.UinputPad("c0")
    if sys.platform == "win32":
        return  # the rest opens files the way Linux does
    monkeypatch.setattr(uinput.sys, "platform", "linux")
    monkeypatch.setattr(uinput, "UINPUT_PATHS", (str(tmp_path / "none"), str(tmp_path / "nor-this")))
    with pytest.raises(SetupError, match=r"found none \(/dev/uinput\): load it with `sudo modprobe uinput`"):
        uinput.check_available()
    shut = tmp_path / "uinput"
    shut.write_bytes(b"")
    monkeypatch.setattr(uinput, "UINPUT_PATHS", (str(tmp_path / "none"), str(shut)))
    uinput.check_available()  # a file that opens is all the check asks for
    if os.geteuid() != 0:  # root opens everything
        shut.chmod(0)
        with pytest.raises(SetupError, match=rf"may not open {shut}: run the receiver as root, or give your user"):
            uinput.check_available()


def test_the_receiver_says_at_start_what_the_pad_lacks(tmp_path, monkeypatch):
    def refuse() -> None:
        raise SetupError("--gamepad uinput may not open /dev/uinput: run the receiver as root")

    monkeypatch.setattr(virtual_gamepad, "check_available", refuse)

    async def run() -> None:
        rx = make_receiver(tmp_path, gamepad="uinput")
        try:
            with pytest.raises(SetupError, match="may not open /dev/uinput"):
                await rx.start()
        finally:
            await rx.stop()

    asyncio.run(run())


class _FakeUinputPad:
    """A pad with an event node, in place of the kernel's."""

    created: ClassVar[list[_FakeUinputPad]] = []
    slot = None

    def __init__(self, label: str) -> None:
        self.label = label
        self.device = f"/dev/input/event{40 + len(self.created)}"
        self.applied: list[dict[str, int]] = []
        self.closed = False
        self.created.append(self)

    def apply(self, xinput: dict[str, int]) -> None:
        self.applied.append(xinput)

    def reset(self) -> None:
        raise AssertionError("a pad that lives for one boot is closed, not reset")

    def on_rumble(self, callback) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_a_linux_pad_names_its_device_and_goes_with_its_controller(tmp_path, monkeypatch):
    """``--gamepad uinput`` end to end with the kernel's part replaced: the pad is created on the
    first report, the status and the log name its event node, and a controller that is switched
    off takes its pad with it, as a wireless pad does."""
    monkeypatch.setattr(virtual_gamepad, "check_available", lambda: None)
    monkeypatch.setattr(virtual_gamepad, "UinputPad", _FakeUinputPad)
    _FakeUinputPad.created.clear()

    async def run() -> None:
        rx = make_receiver(tmp_path, gamepad="uinput", start_game_delay=0.05)
        await rx.start()
        assert isinstance(rx.gamepads, VirtualGamepads) and rx.gamepads.kind == "uinput" and rx.cfg.start_game == "auto"
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12, lt=255)
            await wait_for(lambda: _FakeUinputPad.created and _FakeUinputPad.created[0].applied)
            pad = _FakeUinputPad.created[0]
            assert pad.applied[-1]["wButtons"] == 1 << 12 and pad.applied[-1]["bLeftTrigger"] == 255
            assert rx.status()["gamepads"] == {
                "c0": {"slot": None, "frames": 1, "rumbles": 0, "kind": "uinput", "device": "/dev/input/event40"}
            }
            created = [r for r in rx.log.records if r["event"] == "gamepad_created"]
            assert [(r["kind"], r["device"]) for r in created] == [("uinput", "/dev/input/event40")]
            fc.power_off()
            await wait_for(lambda: single(rx).session["state"] == "dtls-closed")
            assert pad.closed and rx.status()["gamepads"] == {}
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
