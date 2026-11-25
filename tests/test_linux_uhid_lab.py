"""The Linux Stadia pad and the DualSense pad against the real kernel: the device uhid creates
from the report descriptor, read back through its hidraw node the way SDL 3 reads it and through
its event node the way everything else does, rumble written to the hidraw node the way SDL 3
writes it, and a game's force feedback through the kernel's driver for the pad
(hid-google-stadiaff, hid-playstation). Runs on Linux when STADIACAST_LINUX_LAB is set, with the
right to open ``/dev/uhid`` and the pads' nodes (as root, or with the udev rules of the README);
a Stadia Controller and a DualSense appear on the system while it runs."""

from __future__ import annotations

import glob
import os
import sys
import time
from collections.abc import Iterator

import pytest

from stadiacast import dualsense_gamepad as ds
from stadiacast.dualsense_gamepad import DualSensePad, mac_bytes, mac_string
from stadiacast.gamepad import Button, GamepadState
from stadiacast.uhid_gamepad import PAD_NAME, UhidPad, input_report
from tests.test_linux_lab import Game, wait_until


def _uhid_opens() -> bool:
    try:
        os.close(os.open("/dev/uhid", os.O_RDWR))
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("STADIACAST_LINUX_LAB") or not _uhid_opens(),
    reason="needs Linux, the right to open /dev/uhid and STADIACAST_LINUX_LAB=1",
)

EV_KEY, EV_ABS, EV_FF = 0x01, 0x03, 0x15
BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR = 0x130, 0x131, 0x133, 0x134, 0x136, 0x137
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
BTN_TRIGGER_HAPPY1, BTN_TRIGGER_HAPPY2, BTN_TRIGGER_HAPPY3, BTN_TRIGGER_HAPPY4 = 0x2C0, 0x2C1, 0x2C2, 0x2C3
ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y = 0, 1, 2, 5, 9, 10, 0x10, 0x11
#: What the kernel makes of a wired Stadia Controller, less the three media keys of its headset
#: socket: the keys in the order programs number them, and the axes.
KEYS = [BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR]
KEYS += [BTN_TRIGGER_HAPPY1, BTN_TRIGGER_HAPPY2, BTN_TRIGGER_HAPPY3, BTN_TRIGGER_HAPPY4]
AXES = {ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y}


@pytest.fixture(scope="module", autouse=True)
def stadia_driver_loaded() -> None:
    """The first Stadia pad after a boot is taken twice: by the generic HID driver, and again by
    the kernel's Stadia driver once udev has loaded it for these ids, with new nodes each time.
    Let that pass with a pad of its own, so that the tests look at pads that stay as they are."""
    first = UhidPad("lab-first")
    try:
        deadline = time.monotonic() + 2  # a kernel without the driver keeps the generic one
        while time.monotonic() < deadline and not (first.device and _driver(first.device) == "stadia"):
            time.sleep(0.05)
    finally:
        first.close()


@pytest.fixture
def pad() -> Iterator[UhidPad]:
    made = UhidPad("lab")
    try:
        yield made
    finally:
        made.close()


def _event_node(hidraw: str) -> str:
    """The event node of the HID device behind a hidraw node."""
    name = os.path.basename(hidraw)
    found: list[str] = []
    wait_until(lambda: bool(found.extend(glob.glob(f"/sys/class/hidraw/{name}/device/input/input*/event*")) or found))
    return f"/dev/input/{os.path.basename(min(found))}"


def _open_hidraw(path: str) -> int:
    deadline = time.monotonic() + 3
    while True:  # udev sets the node's permissions a moment after the kernel made it
        try:
            return os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _driver(hidraw: str) -> str:
    """The HID driver bound to the pad: ``stadia`` (hid-google-stadiaff) where the kernel has it."""
    return os.path.basename(os.path.realpath(f"/sys/class/hidraw/{os.path.basename(hidraw)}/device/driver"))


def _uevent(hidraw: str) -> list[str]:
    """The lines of the HID device's ``uevent`` file, where SDL's HIDAPI reads ``HID_UNIQ``."""
    with open(f"/sys/class/hidraw/{os.path.basename(hidraw)}/device/uevent", encoding="utf-8") as f:
        return f.read().splitlines()


def test_the_kernel_takes_the_pad_for_a_stadia_controller(pad: UhidPad) -> None:
    assert pad.device is not None and os.path.exists(pad.device)
    assert f"HID_UNIQ={pad.uniq}" in _uevent(pad.device)
    named = UhidPad("lab-serial", "99181FFAZ00123")
    try:
        assert named.device is not None and "HID_UNIQ=99181FFAZ00123" in _uevent(named.device)
    finally:
        named.close()
    game = Game(_event_node(pad.device))
    try:
        assert game.identity() == (0x03, 0x18D1, 0x9400, 0x0100)
        assert game.name() == PAD_NAME
        assert sorted(game.codes(EV_KEY)) == KEYS  # one more or one less would renumber the buttons
        assert game.codes(EV_ABS) == AXES
        for stick in (ABS_X, ABS_Y, ABS_Z, ABS_RZ):
            assert game.axis(stick)[1:3] == (1, 255)
        for trigger in (ABS_GAS, ABS_BRAKE):
            assert game.axis(trigger)[1:3] == (0, 255)
        assert game.axis(ABS_HAT0X)[1:3] == game.axis(ABS_HAT0Y)[1:3] == (-1, 1)
        driver = _driver(pad.device)
        assert driver in ("stadia", "hid-generic")
        assert (EV_FF in game.codes(0)) == (driver == "stadia")  # force feedback comes with the kernel's driver
    finally:
        game.close()


def test_a_pad_that_was_given_no_input_is_at_rest(pad: UhidPad) -> None:
    """A pad exists from the moment its controller connects. Before the first input its sticks
    are in the middle on the event device, which knows no value until a report comes, and a
    program that opens the hidraw node is told the same, as SDL 3 needs it."""
    assert pad.device is not None
    game = Game(_event_node(pad.device))
    try:
        wait_until(lambda: game.axis(ABS_X)[0] == 128)
        assert [game.axis(axis)[0] for axis in (ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE)] == [128] * 4 + [0, 0]
        assert (game.axis(ABS_HAT0X)[0], game.axis(ABS_HAT0Y)[0]) == (0, 0) and not game.held() & set(KEYS)
    finally:
        game.close()
    raw = _open_hidraw(pad.device)
    try:
        seen: list[bytes] = []

        def told() -> bool:
            try:
                seen.append(os.read(raw, 64))
            except BlockingIOError:
                return False
            return True

        wait_until(told)
        assert seen[0] == input_report(GamepadState())
    finally:
        os.close(raw)


def test_every_control_is_the_key_and_the_axis_of_the_wired_controller(pad: UhidPad) -> None:
    """Each button alone, then the D-pad, the sticks and the triggers: the event device says what
    a wired Stadia Controller's says for the same control."""
    assert pad.device is not None
    game = Game(_event_node(pad.device))
    try:
        pad.apply_state(GamepadState())
        wait_until(lambda: game.axis(ABS_X)[0] == 128)
        assert [game.axis(axis)[0] for axis in (ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE)] == [128] * 4 + [0, 0]
        assert not game.held() & set(KEYS)
        buttons = {
            Button.A: BTN_A,
            Button.B: BTN_B,
            Button.X: BTN_X,
            Button.Y: BTN_Y,
            Button.L1: BTN_TL,
            Button.R1: BTN_TR,
            Button.OPTIONS: BTN_SELECT,
            Button.MENU: BTN_START,
            Button.STADIA: BTN_MODE,
            Button.L3: BTN_THUMBL,
            Button.R3: BTN_THUMBR,
            Button.ASSISTANT: BTN_TRIGGER_HAPPY1,
            Button.CAPTURE: BTN_TRIGGER_HAPPY2,
        }
        for button, key in buttons.items():
            pad.apply_state(GamepadState(buttons=int(button)))
            wait_until(lambda key=key: game.held() & set(KEYS) == {key})
            pad.apply_state(GamepadState())
            wait_until(lambda: not game.held() & set(KEYS))
        hats = {
            Button.DPAD_UP: (0, -1),
            Button.DPAD_DOWN: (0, 1),
            Button.DPAD_LEFT: (-1, 0),
            Button.DPAD_RIGHT: (1, 0),
            Button.DPAD_UP | Button.DPAD_RIGHT: (1, -1),
        }
        for held, hat in hats.items():
            pad.apply_state(GamepadState(buttons=int(held)))
            wait_until(lambda hat=hat: (game.axis(ABS_HAT0X)[0], game.axis(ABS_HAT0Y)[0]) == hat)
        moves = [
            (GamepadState(lx=32767), ABS_X, 255),
            (GamepadState(lx=-32768), ABS_X, 1),
            (GamepadState(ly=32767), ABS_Y, 1),  # up is the small end
            (GamepadState(ly=-32768), ABS_Y, 255),
            (GamepadState(rx=32767), ABS_Z, 255),
            (GamepadState(ry=32767), ABS_RZ, 1),
            (GamepadState(l2=255), ABS_BRAKE, 255),
            (GamepadState(r2=255), ABS_GAS, 255),
        ]
        for state, axis, value in moves:
            pad.apply_state(state)
            wait_until(lambda axis=axis, value=value: game.axis(axis)[0] == value)
        pad.apply_state(GamepadState(l2=255, r2=255))
        wait_until(lambda: game.held() & set(KEYS) == {BTN_TRIGGER_HAPPY3, BTN_TRIGGER_HAPPY4})
        pad.apply_state(GamepadState(l2=255))
        wait_until(lambda: game.held() & set(KEYS) == {BTN_TRIGGER_HAPPY4})  # 4 is the left one
    finally:
        game.close()


def test_input_reaches_the_hidraw_node_as_the_report_and_the_event_node_as_keys(pad: UhidPad) -> None:
    assert pad.device is not None
    raw = _open_hidraw(pad.device)
    game = Game(_event_node(pad.device))

    def waiting() -> list[bytes]:
        """Every report that waits on the hidraw node."""
        found: list[bytes] = []
        while True:
            try:
                found.append(os.read(raw, 64))
            except BlockingIOError:
                return found

    try:
        game.events()
        state = GamepadState(buttons=int(Button.ASSISTANT | Button.CAPTURE | Button.A | Button.X), lx=32767, l2=255)
        pad.apply_state(state)
        reports: list[bytes] = []
        wait_until(lambda: bool(reports.extend(waiting())) or input_report(state) in reports)
        # What SDL's Stadia driver reads: the pad at rest, which it was told when it opened the
        # node, and then the state.
        assert set(reports) <= {input_report(GamepadState()), input_report(state)}
        events: list[tuple[int, int, int]] = []
        wait_until(lambda: bool(events.extend(game.events()) or (EV_KEY, BTN_TRIGGER_HAPPY1, 1) in events))
        pressed = {code for kind, code, value in events if kind == EV_KEY and value == 1}
        assert pressed == {BTN_TRIGGER_HAPPY1, BTN_TRIGGER_HAPPY2, BTN_A, BTN_X, BTN_TRIGGER_HAPPY4}  # 4: L2 held
        assert (EV_ABS, ABS_X, 255) in events and (EV_ABS, ABS_BRAKE, 255) in events

        # The pad repeats its state while nothing changes: whoever reads the hidraw node gets the
        # report again, and the event device has nothing new to say.
        game.events()
        waiting()
        again: list[bytes] = []
        wait_until(lambda: len(again.extend(waiting()) or again) >= 3)
        assert set(again) == {input_report(state)}
        assert game.events() == []
    finally:
        game.close()
        os.close(raw)


def test_a_hidraw_write_of_report_5_is_rumble(pad: UhidPad) -> None:
    """What SDL does to rumble a Stadia Controller."""
    assert pad.device is not None
    levels: list[tuple[int, int]] = []
    pad.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    raw = _open_hidraw(pad.device)
    try:
        assert os.write(raw, b"\x05\x00\x80\x00\x40") == 5
        wait_until(lambda: (0x80, 0x40) in levels)
        os.write(raw, b"\x05\x00\x00\x00\x00")
        wait_until(lambda: levels[-1] == (0, 0))
    finally:
        os.close(raw)


def test_a_rumble_left_running_stops_when_its_program_closes_the_pad(pad: UhidPad) -> None:
    """A program that ends while its rumble runs: nothing stops the motors but the kernel's word
    that the pad's nodes are closed. No other program may hold a node of the pad open here."""
    assert pad.device is not None
    levels: list[tuple[int, int]] = []
    pad.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    raw = _open_hidraw(pad.device)
    try:
        os.write(raw, b"\x05\x00\x80\x00\x40")
        wait_until(lambda: (0x80, 0x40) in levels)
    finally:
        os.close(raw)
    wait_until(lambda: levels[-1] == (0, 0))


def test_a_rumble_left_running_stops_though_another_program_has_the_pads_event_node(pad: UhidPad) -> None:
    """The kernel's word that the pad is closed only comes when nobody has any of its nodes. A
    program that reads the event node, as a launcher does all day, would keep the motors on for
    a game that was killed: the pad hears its hidraw node let go of, and sees that nobody has it."""
    assert pad.device is not None
    levels: list[tuple[int, int]] = []
    pad.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    launcher = Game(_event_node(pad.device))
    try:
        raw = _open_hidraw(pad.device)
        try:
            os.write(raw, b"\x05\x00\x80\x00\x40")
            wait_until(lambda: (0x80, 0x40) in levels)
        finally:
            os.close(raw)
        wait_until(lambda: levels[-1] == (0, 0))
    finally:
        launcher.close()


def test_a_driver_that_takes_the_pad_anew_finds_the_state_it_had(pad: UhidPad) -> None:
    """What the first pad after a boot goes through, done by hand: the device is taken from its
    driver and given to one again, which makes a new event device. That one knows no report (its
    sticks read 0, held up and to the left) until the pad writes its state again."""
    if os.geteuid() != 0:
        pytest.skip("taking a device from its driver by hand takes root")
    assert pad.device is not None
    held = GamepadState(buttons=int(Button.A), lx=32767, ly=32767, r2=255)
    pad.apply_state(held)
    hid = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(pad.device)}/device")
    with open(os.path.join(hid, "driver", "unbind"), "w", encoding="ascii") as f:
        f.write(os.path.basename(hid))
    with open("/sys/bus/hid/drivers_probe", "w", encoding="ascii") as f:
        f.write(os.path.basename(hid))

    def state() -> tuple[int, int, int, bool] | None:
        nodes = glob.glob(os.path.join(hid, "input", "input*", "event*"))
        if not nodes:
            return None
        try:
            game = Game(f"/dev/input/{os.path.basename(nodes[0])}")
        except OSError:
            return None
        try:
            return game.axis(ABS_X)[0], game.axis(ABS_Y)[0], game.axis(ABS_GAS)[0], BTN_A in game.held()
        finally:
            game.close()

    wait_until(lambda: state() == (255, 1, 255, True))
    assert pad.device is not None and os.path.exists(pad.device)
    assert os.path.realpath(f"/sys/class/hidraw/{os.path.basename(pad.device)}/device") == hid


def test_a_games_force_feedback_reaches_the_motors_through_the_kernels_driver(pad: UhidPad) -> None:
    """A game that rumbles through the event node: hid-google-stadiaff turns the effect into a
    SET_REPORT of report 5, which the pad answers."""
    assert pad.device is not None
    if _driver(pad.device) != "stadia":
        pytest.skip("hid-google-stadiaff did not take the pad (not built, or not loaded)")
    levels: list[tuple[int, int]] = []
    pad.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    game = Game(_event_node(pad.device))
    try:
        slot = game.upload(0x8000, 0x4000, 0)
        game.play(slot)
        wait_until(lambda: (0x80, 0x40) in levels)
        game.play(slot, 0)
        wait_until(lambda: levels[-1] == (0, 0))
    finally:
        game.close()


# ---- the DualSense pad -----------------------------------------------------------------------
BTN_LEFT, BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST, BTN_TL2, BTN_TR2 = 0x110, 0x130, 0x131, 0x133, 0x134, 0x138, 0x139
ABS_RX, ABS_RY = 3, 4
#: What the kernel's PlayStation driver makes of a DualSense: its gamepad's keys and axes.
DS_KEYS = [BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST, BTN_TL, BTN_TR, BTN_TL2, BTN_TR2]
DS_KEYS += [BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR]
DS_AXES = {ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y}
SERIAL = "99181FFAZ00123"


@pytest.fixture(scope="module")
def playstation_driver_loaded() -> None:
    """As for the Stadia pad: the first DualSense after a boot is taken by the generic driver and
    then by the kernel's PlayStation driver, once udev has loaded it. Let that pass."""
    first = DualSensePad("lab-ds-first", "99181FFAZ00000")
    try:
        deadline = time.monotonic() + 2  # a kernel without the driver keeps the generic one
        while time.monotonic() < deadline and not (first.device and _driver(first.device) == "playstation"):
            time.sleep(0.05)
    finally:
        first.close()


@pytest.fixture
def dualsense(playstation_driver_loaded: None) -> Iterator[DualSensePad]:  # noqa: ARG001 (the driver, loaded first)
    made = DualSensePad("lab-ds", SERIAL)
    try:
        wait_until(lambda: made.device is not None and os.path.exists(made.device))
        yield made
    finally:
        made.close()


def _input_devices(hidraw: str) -> dict[str, str]:
    """The event nodes of the HID device behind a hidraw node, by the name of their input device.
    The kernel's driver makes them one after the other while it takes the pad, a moment after the
    hidraw node is there: one that is only half made is not listed yet."""
    found: dict[str, str] = {}
    for path in glob.glob(f"/sys/class/hidraw/{os.path.basename(hidraw)}/device/input/input*"):
        try:
            with open(os.path.join(path, "name"), encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            continue
        for event in glob.glob(os.path.join(path, "event*")):
            node = f"/dev/input/{os.path.basename(event)}"
            if os.path.exists(node):
                found[name] = node
    return found


def _gamepad(pad: DualSensePad) -> Game:
    """The pad's gamepad as a game holds it, once the kernel has made it."""
    assert pad.device is not None
    wait_until(lambda: ds.PAD_NAME in _input_devices(pad.device or ""))
    return Game(_input_devices(pad.device)[ds.PAD_NAME])


def _battery(pad: DualSensePad) -> tuple[str, str] | None:
    """What the system says of the pad's battery, its state and its level, once it has one."""
    assert pad.device is not None
    read: list[str] = []
    try:
        (supply,) = glob.glob(f"/sys/class/hidraw/{os.path.basename(pad.device)}/device/power_supply/*")
        for field in ("status", "capacity"):
            with open(os.path.join(supply, field), encoding="utf-8") as f:
                read.append(f.read().strip())
    except (OSError, ValueError, TypeError):
        # Not there yet, or there and still being registered: the kernel then answers "try again",
        # which Python's text layer makes a TypeError of.
        return None
    return read[0], read[1]


def _effects(*, flags: int = 0, more: int = 0, strong: int = 0, weak: int = 0) -> bytes:
    """An output report as SDL writes it to the hidraw node: 48 bytes."""
    data = bytearray(48)
    data[0], data[1], data[3], data[4], data[39] = 0x02, flags, weak, strong, more
    return bytes(data)


def test_the_kernel_takes_the_pad_for_a_dualsense(dualsense: DualSensePad) -> None:
    """The ids, the address the pad is known by, and where the kernel has its PlayStation driver
    what that makes of the pad: the gamepad with a DualSense's keys and axes under the version
    programs know that driver's layout by, a battery, and the two devices beside it."""
    assert dualsense.device is not None
    address = mac_string(mac_bytes(SERIAL))
    assert dualsense.uniq == address and f"HID_UNIQ={address}" in _uevent(dualsense.device)
    driver = _driver(dualsense.device)
    assert driver in ("playstation", "hid-generic")
    game = _gamepad(dualsense)
    try:
        assert game.name() == ds.PAD_NAME
        if driver == "hid-generic":
            assert game.identity() == (0x03, 0x054C, 0x0CE6, 0x0111)
            return
        assert game.identity() == (0x03, 0x054C, 0x0CE6, 0x8111)
        assert sorted(game.codes(EV_KEY)) == DS_KEYS and game.codes(EV_ABS) == DS_AXES
    finally:
        game.close()
    # and since Linux 6.18 one more, for a headset in the pad's jack
    devices = {ds.PAD_NAME, f"{ds.PAD_NAME} Motion Sensors", f"{ds.PAD_NAME} Touchpad"}
    wait_until(lambda: set(_input_devices(dualsense.device or "")) >= devices)
    wait_until(lambda: _battery(dualsense) == ("Full", "100"))  # not known yet
    dualsense.set_battery(76)
    wait_until(lambda: _battery(dualsense) == ("Discharging", "75"))  # the middle of its tenth


def test_every_control_is_what_the_kernels_driver_makes_of_a_dualsenses(dualsense: DualSensePad) -> None:
    """Each control alone, read from the gamepad the kernel's driver makes: what SDL 2, browsers
    and the rest read. Capture, the microphone button, is no key there, and Assistant is the
    click of the touchpad, a device of its own."""
    assert dualsense.device is not None
    if _driver(dualsense.device) != "playstation":
        pytest.skip("hid-playstation did not take the pad (not built, or not loaded)")
    game = _gamepad(dualsense)
    try:
        wait_until(lambda: game.axis(ABS_X)[0] == 128)
        assert [game.axis(axis)[0] for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ)] == [128] * 4 + [0, 0]
        assert not game.held() & set(DS_KEYS)
        keys = {
            Button.A: BTN_SOUTH,
            Button.B: BTN_EAST,
            Button.X: BTN_WEST,
            Button.Y: BTN_NORTH,
            Button.L1: BTN_TL,
            Button.R1: BTN_TR,
            Button.OPTIONS: BTN_SELECT,
            Button.MENU: BTN_START,
            Button.STADIA: BTN_MODE,
            Button.L3: BTN_THUMBL,
            Button.R3: BTN_THUMBR,
        }
        for button, key in keys.items():
            dualsense.apply_state(GamepadState(buttons=int(button)))
            wait_until(lambda key=key: game.held() & set(DS_KEYS) == {key})
        for button in (Button.CAPTURE, Button.ASSISTANT):
            dualsense.apply_state(GamepadState(buttons=int(button)))
            wait_until(lambda: not game.held() & set(DS_KEYS))
        hats = {Button.DPAD_UP: (0, -1), Button.DPAD_DOWN: (0, 1), Button.DPAD_LEFT: (-1, 0), Button.DPAD_RIGHT: (1, 0)}
        for button, hat in hats.items():
            dualsense.apply_state(GamepadState(buttons=int(button)))
            wait_until(lambda hat=hat: (game.axis(ABS_HAT0X)[0], game.axis(ABS_HAT0Y)[0]) == hat)
        dualsense.apply_state(GamepadState(lx=32767, ly=32767, rx=-32768, ry=-32768, l2=255, r2=40))
        wait_until(lambda: game.axis(ABS_Z)[0] == 255)
        sticks = [game.axis(axis)[0] for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ)]
        assert sticks == [255, 0, 0, 255, 255, 40] and game.held() & set(DS_KEYS) == {BTN_TL2, BTN_TR2}
        dualsense.apply_state(GamepadState())
    finally:
        game.close()
    wait_until(lambda: f"{ds.PAD_NAME} Touchpad" in _input_devices(dualsense.device or ""))
    try:
        touchpad = Game(_input_devices(dualsense.device)[f"{ds.PAD_NAME} Touchpad"])
    except PermissionError:
        return  # a touchpad is the desktop's to read: not this user's, without a rule for it
    try:
        dualsense.apply_state(GamepadState(buttons=int(Button.ASSISTANT)))
        wait_until(lambda: BTN_LEFT in touchpad.held())
        dualsense.apply_state(GamepadState())
        wait_until(lambda: BTN_LEFT not in touchpad.held())
    finally:
        touchpad.close()


def test_a_program_that_opens_the_dualsense_is_given_its_report_at_once(dualsense: DualSensePad) -> None:
    """What SDL's driver does with a DualSense it has just opened: it waits 16 ms for
    a 64-byte report, and reads neither the address nor the firmware of a pad that shows none.
    The pad reports all the time and at once when the node is opened, so the report is there
    well within that."""
    import select

    assert dualsense.device is not None
    dualsense.set_battery(76)
    dualsense.apply_state(GamepadState(buttons=int(Button.CAPTURE | Button.ASSISTANT | Button.A)))
    waits: list[float] = []
    for _ in range(20):
        raw = _open_hidraw(dualsense.device)
        try:
            began = time.monotonic()
            ready, _, _ = select.select([raw], [], [], 1.0)
            waits.append(time.monotonic() - began)
            assert ready
            report = os.read(raw, 128)
            assert len(report) == 64 and report[0] == 0x01
            assert report[8] & 0x20 and report[10] & 0x06 == 0x06 and report[53] == 7
        finally:
            os.close(raw)
        time.sleep(0.07)  # past the hurry that an open sets off: the next open finds the pad at its own pace
    # all but a few: a machine that is busy with something else may keep any thread waiting
    assert sorted(waits)[-6] < 0.016, f"a report took {max(waits) * 1000:.1f} ms to come: {sorted(waits)[-8:]}"


def test_a_dualsenses_rumble_reaches_the_motors_and_stops_with_the_program_that_left_it_on(
    dualsense: DualSensePad,
) -> None:
    """Rumble as SDL writes it to the hidraw node, the way the pad's firmware version asks for.
    A program that ends while its rumble runs is not there to stop it, and the kernel's driver,
    which holds the device open for itself, never says that the last program closed it."""
    assert dualsense.device is not None
    levels: list[tuple[int, int]] = []
    dualsense.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    raw = _open_hidraw(dualsense.device)
    try:
        assert os.write(raw, _effects(flags=0x02, more=0x04, strong=0x80, weak=0x40)) == 48
        wait_until(lambda: (0x80, 0x40) in levels)
        os.write(raw, _effects())  # SDL's stop
        wait_until(lambda: levels[-1] == (0, 0))
        os.write(raw, _effects(flags=0x02, more=0x04, strong=0xFF, weak=0x10))
        wait_until(lambda: levels[-1] == (0xFF, 0x10))
    finally:
        os.close(raw)  # as a program that is killed lets go of it
    wait_until(lambda: levels[-1] == (0, 0))


def test_a_games_force_feedback_reaches_the_motors_through_the_playstation_driver(dualsense: DualSensePad) -> None:
    """A game that rumbles through the gamepad's event node: hid-playstation turns the effect into
    the pad's output report. Force feedback is an option of that driver's build, and a kernel made
    for servers may leave it out: its gamepad then takes no effect, from any DualSense."""
    assert dualsense.device is not None
    if _driver(dualsense.device) != "playstation":
        pytest.skip("hid-playstation did not take the pad (not built, or not loaded)")
    levels: list[tuple[int, int]] = []
    dualsense.on_rumble(lambda strong, weak: levels.append((strong, weak)))
    game = _gamepad(dualsense)
    try:
        if EV_FF not in game.codes(0):
            pytest.skip("this kernel's hid-playstation was built without force feedback (CONFIG_PLAYSTATION_FF)")
        slot = game.upload(0x8000, 0x4000, 0)
        game.play(slot)
        wait_until(lambda: (0x80, 0x40) in levels)
        game.play(slot, 0)
        wait_until(lambda: levels[-1] == (0, 0))
    finally:
        game.close()


def test_a_second_pad_with_the_same_address_is_refused_by_the_kernel_and_says_so(
    dualsense: DualSensePad, monkeypatch
) -> None:
    """The kernel's driver has one DualSense per address. A second pad of the same controller,
    another receiver's for instance, is started and then let go of, and no driver takes it."""
    assert dualsense.device is not None
    if _driver(dualsense.device) != "playstation":
        pytest.skip("hid-playstation did not take the pad (not built, or not loaded)")
    wait_until(lambda: _battery(dualsense) is not None)  # the driver has taken the first for good
    monkeypatch.setattr(DualSensePad, "DRIVER_WAIT", 0.3)
    second = DualSensePad("lab-ds-again", SERIAL)
    told: list[str] = []
    try:
        second.on_error(told.append)
        wait_until(lambda: bool(told))
        assert "no other took it" in told[0]
    finally:
        second.close()
    assert _driver(dualsense.device) == "playstation"  # the first is as it was
