"""Stadia Controller input on the display link (Gotham Wi-Fi firmware ota.282115).

Once the display RPC handshake reaches READY, the controller reports its input
state as an RpcNotification carrying ``ControllerToDisplay`` #3
(the display connection's input handler), framed with WireHeader2 id 0x74 and sent
only when something changes.  The layout:

    f1      button mask (``Button``; bits 0-15 = the XInput wButtons layout)
    f2, f3  left stick X, Y    (-32768..32767, Y positive = up)
    f4, f5  right stick X, Y   (-32768..32767, Y positive = up)
    f6, f7  L2, R2 triggers    (0..255; no digital bit)

This module is pure Python with no dependencies: decoding, button names, edge
detection, a radial stick deadzone and a conversion to the XInput
``XINPUT_GAMEPAD`` field set, so any virtual-gamepad backend can consume it.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Any


class Button(enum.IntFlag):
    """ControllerToDisplay #3 field 1, one bit per button (live-mapped)."""

    DPAD_UP = 1 << 0
    DPAD_DOWN = 1 << 1
    DPAD_LEFT = 1 << 2
    DPAD_RIGHT = 1 << 3
    MENU = 1 << 4  # the "≡" button (XInput START)
    OPTIONS = 1 << 5  # the "⋯" button (XInput BACK)
    L3 = 1 << 6
    R3 = 1 << 7
    L1 = 1 << 8
    R1 = 1 << 9
    ASSISTANT = 1 << 10  # Stadia-specific
    CAPTURE = 1 << 11  # Stadia-specific
    A = 1 << 12
    B = 1 << 13
    X = 1 << 14
    Y = 1 << 15
    STADIA = 1 << 16  # the Stadia logo button (a short tap; a 4 s hold powers the controller off)


BUTTON_NAMES: dict[int, str] = {b.value.bit_length() - 1: str(b.name).lower() for b in Button}  # bit -> name

STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MAX = 255

# XInput XINPUT_GAMEPAD.wButtons.  Bits 0-9 and 12-15 are identical to ours; XInput has no
# Assistant / Capture buttons, and its (undocumented, XInputGetStateEx) guide bit is 0x0400.
XINPUT_GUIDE = 0x0400
XINPUT_SHARED_BITS = 0xF3FF
# Microsoft's suggested thumbstick deadzones (XINPUT_GAMEPAD_*_THUMB_DEADZONE).
XINPUT_LEFT_DEADZONE = 7849
XINPUT_RIGHT_DEADZONE = 8689


def button_name(bit: int) -> str:
    return BUTTON_NAMES.get(bit, f"bit{bit}")


def radial_deadzone(x: int, y: int, deadzone: int) -> tuple[int, int]:
    """Zero the stick inside a circle of radius ``deadzone`` and rescale the rest so the
    edge of the deadzone maps to 0 and full deflection stays at full range."""
    if deadzone <= 0:
        return x, y
    mag = math.hypot(x, y)
    if mag <= deadzone:
        return 0, 0
    full = float(STICK_MAX)
    scaled = min(full, (mag - deadzone) / (full - deadzone) * full)
    k = scaled / mag

    def clamp(v: float) -> int:
        return max(STICK_MIN, min(STICK_MAX, round(v)))

    return clamp(x * k), clamp(y * k)


@dataclass(frozen=True)
class GamepadState:
    """One input report.  Field names follow the physical controls."""

    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    l2: int = 0
    r2: int = 0

    @classmethod
    def from_message3(cls, m: Any) -> GamepadState:
        """From the decoded ``ControllerToDisplay.message3`` protobuf (fields f1..f7)."""
        return cls(buttons=m.f1, lx=m.f2, ly=m.f3, rx=m.f4, ry=m.f5, l2=m.f6, r2=m.f7)

    def held(self) -> list[str]:
        """Names of the buttons held in this report, lowest bit first."""
        return [button_name(b) for b in range(32) if self.buttons & (1 << b)]

    def changed_bits(self, prev: GamepadState | None) -> tuple[list[int], list[int]]:
        """(pressed, released) bit numbers relative to the previous report."""
        before = prev.buttons if prev is not None else 0
        delta = self.buttons ^ before
        pressed = [b for b in range(32) if delta & self.buttons & (1 << b)]
        released = [b for b in range(32) if delta & before & (1 << b)]
        return pressed, released

    def to_xinput(self, left_deadzone: int = 0, right_deadzone: int = 0) -> dict[str, int]:
        """The XInput ``XINPUT_GAMEPAD`` field set.  Stadia maps to the guide bit;
        Assistant and Capture have no XInput equivalent and are dropped (read them
        from ``buttons``).  XInput also uses Y positive = up, so no sign flip."""
        w = self.buttons & XINPUT_SHARED_BITS
        if self.buttons & Button.STADIA:
            w |= XINPUT_GUIDE
        lx, ly = radial_deadzone(self.lx, self.ly, left_deadzone)
        rx, ry = radial_deadzone(self.rx, self.ry, right_deadzone)
        return {
            "wButtons": w,
            "bLeftTrigger": self.l2,
            "bRightTrigger": self.r2,
            "sThumbLX": lx,
            "sThumbLY": ly,
            "sThumbRX": rx,
            "sThumbRY": ry,
        }

    def as_event(self, prev: GamepadState | None) -> dict[str, Any]:
        """The receiver's ``rpc_input`` event for this report."""
        pressed, released = self.changed_bits(prev)
        return {
            "type": "rpc_input",
            "buttons": self.buttons,
            "pressed": pressed,
            "released": released,
            "pressed_names": [button_name(b) for b in pressed],
            "released_names": [button_name(b) for b in released],
            "axes": {"lx": self.lx, "ly": self.ly, "rx": self.rx, "ry": self.ry},
            "triggers": {"l": self.l2, "r": self.r2},
        }
