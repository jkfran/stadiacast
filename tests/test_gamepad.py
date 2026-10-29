"""stadiacast.gamepad: the ControllerToDisplay #3 input layout, and the --preset flag set
proven against the real controller."""

from __future__ import annotations

from stadiacast.gamepad import BUTTON_NAMES, XINPUT_GUIDE, Button, GamepadState, button_name, radial_deadzone
from stadiacast.protos import Protos

# what the live button-mapping session recorded, in the order the buttons were pressed
LIVE_MAPPING = [
    ("a", 12),
    ("b", 13),
    ("x", 14),
    ("y", 15),
    ("l1", 8),
    ("r1", 9),
    ("l3", 6),
    ("r3", 7),
    ("dpad_up", 0),
    ("dpad_down", 1),
    ("dpad_left", 2),
    ("dpad_right", 3),
    ("options", 5),
    ("menu", 4),
    ("assistant", 10),
    ("capture", 11),
    ("stadia", 16),
]


def test_button_bits_match_the_live_mapping_session():
    assert [(button_name(bit), bit) for _, bit in LIVE_MAPPING] == LIVE_MAPPING
    assert len(BUTTON_NAMES) == 17
    assert len(Button) == 17
    assert Button.Y == 0x8000
    assert Button.STADIA == 0x10000
    assert button_name(20) == "bit20"


def test_from_message3_and_edges():
    p = Protos()
    msg = p.ControllerToDisplay()
    m = msg.message3
    m.f1, m.f2, m.f3, m.f4, m.f5, m.f6, m.f7 = int(Button.A | Button.L1), -32768, 32767, 5, -6, 255, 3
    s1 = GamepadState.from_message3(p.ControllerToDisplay.FromString(msg.SerializeToString()).message3)
    assert (s1.lx, s1.ly, s1.rx, s1.ry, s1.l2, s1.r2) == (-32768, 32767, 5, -6, 255, 3)
    assert s1.held() == ["l1", "a"]
    assert s1.changed_bits(None) == ([8, 12], [])
    s2 = GamepadState(buttons=int(Button.A | Button.B))
    assert s2.changed_bits(s1) == ([13], [8])
    ev = s2.as_event(s1)
    assert ev["type"] == "rpc_input"
    assert ev["pressed_names"] == ["b"]
    assert ev["released_names"] == ["l1"]
    assert ev["axes"] == {"lx": 0, "ly": 0, "rx": 0, "ry": 0}
    assert ev["triggers"] == {"l": 0, "r": 0}


def test_xinput_conversion():
    s = GamepadState(
        buttons=int(
            Button.DPAD_UP | Button.MENU | Button.R1 | Button.Y | Button.ASSISTANT | Button.CAPTURE | Button.STADIA
        ),
        lx=-100,
        ly=32767,
        l2=7,
        r2=255,
    )
    x = s.to_xinput()
    assert x["wButtons"] == 0x0001 | 0x0010 | 0x0200 | 0x8000 | XINPUT_GUIDE  # assistant/capture dropped
    assert (x["sThumbLX"], x["sThumbLY"], x["bLeftTrigger"], x["bRightTrigger"]) == (-100, 32767, 7, 255)
    assert GamepadState(buttons=int(Button.ASSISTANT)).to_xinput()["wButtons"] == 0


def test_radial_deadzone():
    assert radial_deadzone(3000, -2000, 7849) == (0, 0)  # inside: centred
    assert radial_deadzone(32767, 0, 7849) == (32767, 0)  # full deflection stays full
    assert radial_deadzone(0, -32768, 7849)[1] <= -32767  # symmetric scale
    x, y = radial_deadzone(20000, 0, 7849)
    assert 0 < x < 20000
    assert y == 0
    assert radial_deadzone(1234, -5, 0) == (1234, -5)  # disabled
