"""The ``InputProtocolMessage`` stream: the controller's input on the game session (and on SRTP).

An ``InputProtocolMessage`` carries either an ``ack_sequence_number`` or an ``InputMessageSeries``
(plain or compressed) of timestamped messages: gamepad state (``GamepadMessage_V2``), keyboard,
mouse, touch and the feedback messages the display sends back.  :class:`InputDecoder` turns a
payload into event dicts with per-gamepad press / release and axis diffs.  The display-link
input report (``ControllerToDisplay`` #3) is a different message, decoded by
:mod:`stadiacast.gamepad`.
"""

from __future__ import annotations

import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stadiacast.protos import PKG_STREAMING, Protos


def _ignore_event(_ev: dict[str, Any]) -> None:
    """Default ``on_event``."""


def decompress_series(data: bytes) -> bytes:
    """``InputProtocolMessage.compressed_series``: the firmware mentions both gzip and deflate, so
    try zlib / gzip framing first, then raw deflate."""
    for wbits in (zlib.MAX_WBITS | 32, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    raise ValueError("compressed_series is neither zlib/gzip nor raw deflate")


@dataclass
class GamepadTracker:
    """Last known state of one gamepad id, for press / release and axis diffs."""

    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    lt: int = 0
    rt: int = 0
    seen: bool = False


class InputDecoder:
    """Decode ``InputProtocolMessage`` payloads into event dicts.

    Button bit positions are reported raw (``pressed`` / ``released`` lists of bit indices):
    the ``GamepadMessage_V2`` bit layout has not been mapped live, unlike the display-link
    report decoded by :mod:`stadiacast.gamepad`.
    """

    SLIM_FIELDS = (
        "gamepad_message_v2",
        "gamepad_message",
        "keyboard_message_v2",
        "keyboard_message",
        "mouse_message_v2",
        "mouse_message",
        "touch_message_v2",
        "text_input_message_v2",
        "input_future_response_v2",
        "input_feedback_message_v2",
    )

    def __init__(self, protos: Protos, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.protos = protos
        self.on_event = on_event or _ignore_event
        self.gamepads: dict[int, GamepadTracker] = {}
        self.stats: Counter[str] = Counter()

    def decode(self, payload: bytes) -> dict[str, Any]:
        """Decode one payload; raises ``DecodeError`` / ``ValueError`` on garbage."""
        msg = self.protos.InputProtocolMessage()
        msg.ParseFromString(payload)
        out: dict[str, Any] = {"events": []}
        if msg.HasField("ack_sequence_number"):
            out["ack_sequence_number"] = msg.ack_sequence_number
        series = None
        if msg.HasField("input_message_series"):
            series = msg.input_message_series
        elif len(msg.compressed_series):
            raw = decompress_series(msg.compressed_series)
            series = self.protos.InputMessageSeries()
            series.ParseFromString(raw)
            out["compressed"] = True
            if msg.HasField("uncompressed_size") and msg.uncompressed_size != len(raw):
                out["uncompressed_size_mismatch"] = [msg.uncompressed_size, len(raw)]
        if series is not None:
            out.update(
                session_id=series.session_id,
                sequence_number=series.sequence_number,
                base_timestamp_us=series.base_timestamp_us,
                n_messages=len(series.messages),
            )
            if series.HasField("ack_time_us"):
                out["ack_time_us"] = series.ack_time_us
            for slim in series.messages:
                ev = self._slim(slim, series)
                if ev is not None:
                    out["events"].append(ev)
                    self.stats[ev["type"]] += 1
                    self.on_event(ev)
        self.stats["messages"] += 1
        return out

    def _slim(self, slim: Any, series: Any) -> dict[str, Any] | None:
        t_us = series.base_timestamp_us + slim.relative_time_us
        base = {"t_us": t_us, "relative_time_us": slim.relative_time_us}
        if slim.HasField("rtp_timestamp"):
            base["rtp_timestamp"] = slim.rtp_timestamp
        if slim.HasField("relative_frame_token"):
            base["relative_frame_token"] = slim.relative_frame_token
        if slim.HasField("gamepad_message_v2"):
            return self._gamepad_v2(slim.gamepad_message_v2, base)
        if slim.HasField("gamepad_message"):
            return self._gamepad_v1(slim.gamepad_message, base)
        for name in self.SLIM_FIELDS[2:]:
            if slim.HasField(name):
                ev = dict(base)
                ev["type"] = name
                ev["data"] = self.to_dict(getattr(slim, name))
                return ev
        ev = dict(base)
        ev["type"] = "empty"
        return ev

    to_dict = staticmethod(Protos.to_dict)

    def _diff(self, st: GamepadTracker, buttons: int, axes: dict[str, int], ev: dict[str, Any]) -> None:
        changed = (buttons ^ st.buttons) if st.seen else buttons
        ev["pressed"] = [b for b in range(32) if (changed >> b) & 1 and (buttons >> b) & 1]
        ev["released"] = [b for b in range(32) if (changed >> b) & 1 and not (buttons >> b) & 1]
        ev["axes"] = axes
        prev = {"lx": st.lx, "ly": st.ly, "rx": st.rx, "ry": st.ry, "lt": st.lt, "rt": st.rt}
        ev["axes_changed"] = [k for k, v in axes.items() if (not st.seen) or prev.get(k) != v]
        st.buttons, st.seen = buttons, True
        for k, v in axes.items():
            setattr(st, k, v)

    def _gamepad_v2(self, g: Any, base: dict[str, Any]) -> dict[str, Any]:
        ev = dict(base)
        ev.update(type="gamepad", version=2, id=g.id, buttons=f"0x{g.buttons:08x}")
        if g.HasField("device_presence"):
            ev["device_presence"] = g.device_presence
        if g.HasField("device_type"):
            ev["device_type"] = self.protos.enum_name(f"{PKG_STREAMING}.GamepadMessage_V2.Type", g.device_type)
        if g.HasField("registration_info"):
            ev["registration_info"] = self.to_dict(g.registration_info)
        st = self.gamepads.setdefault(g.id, GamepadTracker())
        axes = {
            "lx": g.axis_left_x,
            "ly": g.axis_left_y,
            "rx": g.axis_right_x,
            "ry": g.axis_right_y,
            "lt": g.trigger_left,
            "rt": g.trigger_right,
        }
        self._diff(st, g.buttons, axes, ev)
        return ev

    def _gamepad_v1(self, g: Any, base: dict[str, Any]) -> dict[str, Any]:
        ev = dict(base)
        ev.update(type="gamepad", version=1)
        if g.HasField("type"):
            ev["message_type"] = self.protos.enum_name(f"{PKG_STREAMING}.GamepadMessageType", g.type)
        gid = None
        for sub in ("register", "button", "joystick", "trigger", "vibration"):
            if g.HasField(sub):
                m = getattr(g, sub)
                ev[sub] = self.to_dict(m)
                gid = m.id if m.HasField("id") else gid
        ev["id"] = gid
        st = self.gamepads.setdefault(gid if gid is not None else -1, GamepadTracker())
        buttons = g.button.buttons if g.HasField("button") else st.buttons
        axes = {"lx": st.lx, "ly": st.ly, "rx": st.rx, "ry": st.ry, "lt": st.lt, "rt": st.rt}
        if g.HasField("joystick"):
            j = g.joystick
            axes.update(lx=j.left_x, ly=j.left_y, rx=j.right_x, ry=j.right_y)
        if g.HasField("trigger"):
            axes.update(lt=g.trigger.left_trigger, rt=g.trigger.right_trigger)
        ev["buttons"] = f"0x{buttons:08x}"
        self._diff(st, buttons, axes, ev)
        return ev
