"""A virtual gamepad per controller (Windows ViGEmBus, macOS OpenJoystickDriver or Linux uinput).

``--gamepad vigem`` creates one ViGEm pad per controller through ``vgamepad`` (the ``gamepad``
extra, over the ViGEmBus driver), feeds every decoded input report to it as XInput fields, and
forwards the game's rumble back to the controller through the game session, which the flag
switches on once the link is READY (rumble has no other path). The reports come from the
display link and, once a game runs, from the game session; both read into the same
:class:`~stadiacast.gamepad.GamepadState`. Two controllers are XInput players 1 and 2, each fed
only by its own reports. A pad that cannot be created shows its error in ``/_status`` and is
tried again on the controller's next report after it reboots.

``--gamepad ojd`` is the same on macOS, where the pad is a feed process of the OpenJoystickDriver
app (:mod:`stadiacast.ojd_gamepad`) and is closed when its controller goes away.

``--gamepad uinput`` is the same on Linux, where the pad is an input device the receiver creates
through the kernel's uinput (:mod:`stadiacast.uinput_gamepad`). It is removed when its controller
goes away, as a wireless pad is when it is switched off.

:class:`PadBackend` is the small surface the receiver drives, so the tests run the same code
against a fake pad on every platform.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from stadiacast.errors import SetupError
from stadiacast.gamepad import XINPUT_LEFT_DEADZONE, XINPUT_RIGHT_DEADZONE, GamepadState
from stadiacast.ojd_gamepad import OJDPad, find_ojd
from stadiacast.uinput_gamepad import UinputPad, check_available

if TYPE_CHECKING:
    from stadiacast.config import Config
    from stadiacast.receiver import Receiver

#: XInput ``wButtons`` bit -> ``vgamepad.XUSB_BUTTON`` name, 1:1 with the mask. Bits 12..15 carry
#: A/B/X/Y; bit 11 is unused by XInput (Stadia's Capture has no equivalent), bit 10 is the guide.
BUTTON_BITS: dict[int, str] = {
    0: "XUSB_GAMEPAD_DPAD_UP",
    1: "XUSB_GAMEPAD_DPAD_DOWN",
    2: "XUSB_GAMEPAD_DPAD_LEFT",
    3: "XUSB_GAMEPAD_DPAD_RIGHT",
    4: "XUSB_GAMEPAD_START",
    5: "XUSB_GAMEPAD_BACK",
    6: "XUSB_GAMEPAD_LEFT_THUMB",
    7: "XUSB_GAMEPAD_RIGHT_THUMB",
    8: "XUSB_GAMEPAD_LEFT_SHOULDER",
    9: "XUSB_GAMEPAD_RIGHT_SHOULDER",
    10: "XUSB_GAMEPAD_GUIDE",
    12: "XUSB_GAMEPAD_A",
    13: "XUSB_GAMEPAD_B",
    14: "XUSB_GAMEPAD_X",
    15: "XUSB_GAMEPAD_Y",
}

#: XInput motor levels are 0..255; the controller's take 0..65535.
MOTOR_SCALE = 257

#: Button and trigger changes kept in order for a pad that is still being created, so a tap made
#: in that time is played back. Past this many the newest state stands for all of them.
PENDING_TRANSITIONS = 32


def _wording(error: Exception) -> str:
    """An error for the log and ``/_status``: our own wording as plain text, a driver's as its repr."""
    return str(error) if isinstance(error, SetupError) else repr(error)


class PadBackend(Protocol):
    """What the receiver drives: one virtual pad."""

    slot: int | None

    def apply(self, xinput: dict[str, int]) -> None: ...

    def reset(self) -> None: ...

    def on_rumble(self, callback: Callable[[int, int], None]) -> None: ...

    def close(self) -> None: ...


def occupied_xinput_slots() -> set[int]:
    """Which of XInput's players 0..3 have a pad right now (Windows; empty elsewhere)."""
    if sys.platform != "win32":
        return set()
    import ctypes

    class _Gamepad(ctypes.Structure):
        _fields_ = [
            ("wButtons", ctypes.c_ushort),
            ("bLeftTrigger", ctypes.c_ubyte),
            ("bRightTrigger", ctypes.c_ubyte),
            ("sThumbLX", ctypes.c_short),
            ("sThumbLY", ctypes.c_short),
            ("sThumbRX", ctypes.c_short),
            ("sThumbRY", ctypes.c_short),
        ]

    class _State(ctypes.Structure):
        _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _Gamepad)]

    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            lib = ctypes.WinDLL(name)
        except OSError:
            continue
        lib.XInputGetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_State)]
        lib.XInputGetState.restype = ctypes.c_ulong
        state = _State()
        return {i for i in range(4) if lib.XInputGetState(i, ctypes.byref(state)) == 0}
    return set()


def wait_until_plugged_in(
    update: Callable[[], None], timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Send the first report once Windows has finished plugging the new pad in.

    ViGEm answers ``VIGEM_ERROR_TARGET_NOT_PLUGGED_IN`` to a report sent before the plug-in
    completes, which takes a moment after the target is added (seconds on a slow machine). A pad
    still unplugged at ``timeout`` means a Windows without the Xbox 360 controller driver the pad
    binds to (Windows Server): a ``SetupError`` that says so.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            update()
            return
        except RuntimeError as e:
            if "NOT_PLUGGED_IN" not in str(e):
                raise
            if time.monotonic() >= deadline:
                raise SetupError(
                    f"Windows did not plug the pad in within {timeout:g} s ({e}): a ViGEm pad binds to"
                    " Windows' own Xbox 360 controller driver, which desktop Windows 10 and 11 have"
                    " and Windows Server lacks"
                ) from e
            sleep(0.1)


class ViGEmPad:
    """One ViGEm Xbox 360 pad. Create it off the event loop: the wake-up dance sleeps."""

    def __init__(self, label: str, *, settle: bool = True) -> None:
        import vgamepad as vg  # Windows only; the import itself fails elsewhere

        self.label = label
        self._buttons = {bit: getattr(vg.XUSB_BUTTON, name) for bit, name in BUTTON_BITS.items()}
        before = occupied_xinput_slots()
        self.pad: Any = vg.VX360Gamepad()
        self.mask = 0
        self.slot: int | None = None
        wait_until_plugged_in(self.pad.update)
        if settle:
            self._settle(before)

    def _settle(self, before: set[int]) -> None:
        # XInput enumerates a fresh ViGEm target only after a short delay, and until a report
        # changes something the pad exposes uninitialised stick values (a constant phantom drift):
        # alternate a tiny deflection with a neutral report, then leave it neutral.
        time.sleep(1.0)
        for i in range(6):
            if i % 2:
                self.pad.left_joystick(200, 0)
            else:
                self.pad.reset()
            self.pad.update()
            time.sleep(0.25)
        self.pad.reset()
        self.pad.update()
        time.sleep(0.2)
        for _ in range(20):  # the XInput player this pad landed on: whatever appeared since `before`
            new = occupied_xinput_slots() - before
            if new:
                self.slot = min(new)
                return
            time.sleep(0.1)

    def apply(self, xinput: dict[str, int]) -> None:
        w = xinput["wButtons"]
        changed = w ^ self.mask
        if changed:
            for bit, button in self._buttons.items():
                if changed & (1 << bit):
                    if w & (1 << bit):
                        self.pad.press_button(button)
                    else:
                        self.pad.release_button(button)
            self.mask = w
        self.pad.left_joystick(xinput["sThumbLX"], xinput["sThumbLY"])
        self.pad.right_joystick(xinput["sThumbRX"], xinput["sThumbRY"])
        self.pad.left_trigger(xinput["bLeftTrigger"])
        self.pad.right_trigger(xinput["bRightTrigger"])
        self.pad.update()

    def reset(self) -> None:
        self.pad.reset()
        self.pad.update()
        self.mask = 0

    def on_rumble(self, callback: Callable[[int, int], None]) -> None:
        # ViGEm calls back on its own thread with (client, target, large_motor, small_motor, led, user).
        self.pad.register_notification(
            callback_function=lambda _c, _t, large, small, _led, _user: callback(int(large), int(small))
        )

    def close(self) -> None:
        try:
            self.pad.unregister_notification()
        except Exception:  # not registered, or ViGEm already gone
            pass
        self.reset()
        self.pad = None  # vgamepad removes the target when the object goes away


class VirtualGamepads:
    """The receiver's gamepad output: one pad per controller label, created on the first report."""

    def __init__(
        self,
        cfg: Config,
        log: Callable[..., Any],
        rx: Receiver,
        backend_factory: Callable[[str], PadBackend],
        *,
        kind: str = "vigem",
        per_boot: bool = False,
    ) -> None:
        self.cfg = cfg
        self.log = log
        self.rx = rx
        self.kind = kind
        #: A pad lives for one boot of its controller and is closed when the controller goes away
        #: (an OJD feed is one of four, a uinput pad is made in no time), where a ViGEm pad, which
        #: takes seconds to create, stays and is only let go of.
        self._per_boot = per_boot
        self._factory = backend_factory
        self._pads: dict[str, PadBackend] = {}
        self._creating: dict[str, asyncio.Task[None]] = {}
        self._closing: set[asyncio.Task[None]] = set()  # pads being closed in a worker thread
        self._pending: dict[str, deque[GamepadState]] = {}
        self._generation: dict[str, int] = {}
        self._closed = False
        self._frames: dict[str, int] = {}
        self._rumbles: dict[str, int] = {}
        self._failed: dict[str, str] = {}  # label -> why its pad could not be created, until release()
        self._dz = (XINPUT_LEFT_DEADZONE, XINPUT_RIGHT_DEADZONE) if cfg.gamepad_deadzone else (0, 0)

    @classmethod
    def create(cls, cfg: Config, log: Callable[..., Any], rx: Receiver) -> VirtualGamepads:
        """The backend ``--gamepad`` names, or a :class:`SetupError` saying what to install."""
        if cfg.gamepad == "ojd":
            if sys.platform != "darwin":
                raise SetupError("--gamepad ojd is macOS only: it uses the signed OpenJoystickDriver app")
            executable = find_ojd(cfg.ojd_path)
            return cls(
                cfg, log, rx, lambda label: OJDPad(label, executable, cfg.gamepad_profile), kind="ojd", per_boot=True
            )
        if cfg.gamepad == "uinput":
            check_available()  # the wrong system, no uinput, or no right to open it: said at start
            return cls(cfg, log, rx, UinputPad, kind="uinput", per_boot=True)
        if cfg.gamepad != "vigem":
            raise ValueError(f"unknown --gamepad {cfg.gamepad!r}; use off, vigem, ojd or uinput")
        if sys.platform != "win32":
            raise SetupError("--gamepad vigem is Windows only: it drives the ViGEmBus driver through vgamepad")
        try:
            import vgamepad  # noqa: F401
        except ImportError as e:
            raise SetupError("--gamepad vigem needs the gamepad extra: pip install 'stadiacast[gamepad]'") from e
        except RuntimeError as e:  # vgamepad connects to the bus on import; no driver, no import
            raise SetupError(
                f"--gamepad vigem needs the ViGEmBus driver installed (vgamepad said: {e}); "
                "get it from https://github.com/nefarius/ViGEmBus/releases"
            ) from e
        return cls(cfg, log, rx, ViGEmPad)

    # ---- input -----------------------------------------------------------------------
    def on_event(self, ev: dict[str, Any]) -> None:
        """One decoded input event from the receiver: apply it to that controller's pad.

        Both channels feed it: the display link, and the game session once a game runs (which
        ``--gamepad`` starts, so in a game this is where the input comes from). Only gamepad
        reports carry ``axes``; the game session's keyboard, mouse and feedback events do not.
        """
        label = str(ev.get("controller") or "")
        if self._closed or not label or label in self._failed or "axes" not in ev:
            return
        state = GamepadState.from_event(ev)
        pad = self._pads.get(label)
        if pad is None:
            pending = self._pending.setdefault(label, deque())
            if pending and (pending[-1].buttons, pending[-1].l2, pending[-1].r2) == (state.buttons, state.l2, state.r2):
                pending[-1] = state
            elif len(pending) < PENDING_TRANSITIONS:
                pending.append(state)  # retain brief presses/releases while a pad is being created
            else:
                # More than is worth playing back (a ViGEm pad takes seconds to appear): the newest
                # state stands for all of it, which lets go of whatever was pressed on the way.
                pending.clear()
                pending.append(state)
            if label not in self._creating:
                self._creating[label] = asyncio.get_running_loop().create_task(
                    self._create(label, self._generation.get(label, 0))
                )
            return
        self._apply(label, pad, state)

    def _apply(self, label: str, pad: PadBackend, state: GamepadState) -> None:
        try:
            pad.apply(state.to_xinput(*self._dz))
        except SetupError as e:  # the pad's own word that it is done: a feed that ended or cannot keep up
            self._backend_failed(label, pad, str(e))
            return
        self._frames[label] = self._frames.get(label, 0) + 1

    def _creation_failed(self, label: str, error: Exception) -> None:
        message = _wording(error)
        self._failed[label] = message
        self._pending.pop(label, None)
        self.log("gamepad_failed", controller=label, error=message)
        self.rx.live_changed()

    def _backend_failed(self, label: str, pad: PadBackend, error: str) -> None:
        if self._closed or self._pads.get(label) is not pad:
            return  # a callback from an already released feed
        self._rumble(label, 0, 0)
        self._pads.pop(label)
        self._failed[label] = error
        self.log("gamepad_failed", controller=label, error=error)
        self._close_pad(label, pad)
        self.rx.live_changed()

    async def _create(self, label: str, generation: int, after: asyncio.Task[None] | None = None) -> None:
        """Build the pad for ``label`` in a worker thread, then keep it or let it go.

        ``generation`` is the label's count of releases when the pad was asked for. A pad that
        lives for one boot and is ready only after its controller went away is nobody's: it is
        closed, and built again when the controller is back and its input waits."""
        if after is not None:  # the pad this one replaces is still being closed
            await asyncio.wait([after])
        if self._closed:
            self._creating.pop(label, None)
            return
        factory = asyncio.create_task(asyncio.to_thread(self._factory, label))
        pad: PadBackend | None = None
        error: Exception | None = None
        try:
            pad = await asyncio.shield(factory)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop a constructor. Reap the device it
            # eventually returns instead of leaking it after receiver shutdown.
            try:
                late = await factory
            except Exception:
                pass
            else:
                self._close_pad(label, late)
            raise
        except Exception as e:  # ViGEm refused, Windows never plugged the pad in, the driver did not start
            error = e
        finally:
            self._creating.pop(label, None)
        stale = self._per_boot and generation != self._generation.get(label, 0)
        if self._closed or stale:
            closing = self._close_pad(label, pad) if pad is not None else None
            if not self._closed and label in self._pending and label not in self._failed:
                again = self._create(label, self._generation.get(label, 0), closing)
                self._creating[label] = asyncio.create_task(again)
            return
        if pad is None:
            assert error is not None
            self._creation_failed(label, error)  # no pad until release()
            return
        self._pads[label] = pad
        self._frames.setdefault(label, 0)
        self._rumbles.setdefault(label, 0)
        loop = asyncio.get_running_loop()

        def forward(large: int, small: int) -> None:  # from ViGEm's thread onto the loop
            loop.call_soon_threadsafe(forward_if_current, large, small)

        def forward_if_current(large: int, small: int) -> None:
            if not self._closed and self._pads.get(label) is pad:
                self._rumble(label, large, small)

        pad.on_rumble(forward)
        # Subprocess-backed pads can fail between reports, including while idle.
        on_error = getattr(pad, "on_error", None)
        if on_error is not None:
            on_error(lambda error: loop.call_soon_threadsafe(self._backend_failed, label, pad, error))
        created: dict[str, Any] = {"kind": self.kind, "slot": pad.slot, "deadzone": self.cfg.gamepad_deadzone}
        if self.kind == "uinput":
            created["device"] = getattr(pad, "device", None)
        self.log("gamepad_created", controller=label, **created)
        self.rx.live_changed()
        for state in self._pending.pop(label, ()):
            self._apply(label, pad, state)
            if label in self._failed:
                break

    # ---- rumble ----------------------------------------------------------------------
    def _rumble(self, label: str, large: int, small: int) -> None:
        """The game's motor levels, from ViGEm's thread via the loop: to the controller's game session."""
        ctl = self.rx.controllers.get(label)
        if ctl is None:
            return
        left, right = min(65535, large * MOTOR_SCALE), min(65535, small * MOTOR_SCALE)
        self._rumbles[label] = self._rumbles.get(label, 0) + 1
        self.rx.live_changed()
        try:
            seq = ctl.game.set_rumble(left, right)
        except (RuntimeError, ValueError) as e:
            self.log("gamepad_rumble_failed", _quiet=True, controller=label, error=str(e))
            return
        self.log("gamepad_rumble", _quiet=True, controller=label, left=left, right=right, sent=seq is not None)

    # ---- lifecycle -------------------------------------------------------------------
    def release(self, label: str) -> None:
        """Let go of every button and stick: the controller rebooted or went away.

        A pad that could not be created is forgotten too, so the controller's next report tries
        again: rebooting the controller is how a user retries a pad ViGEm refused.
        """
        self._generation[label] = self._generation.get(label, 0) + 1
        pad = self._pads.get(label)
        if pad is not None:
            if self._per_boot:
                self._pads.pop(label)
                self._close_pad(label, pad)  # close the feed and release OJD's limited slots
            else:
                try:
                    pad.reset()
                except Exception as e:  # a pad its driver has dropped: the next report gets a new one
                    self._backend_failed(label, pad, _wording(e))
        self._pending.pop(label, None)
        self._failed.pop(label, None)

    def close(self) -> None:
        self._closed = True
        # Constructors running in threads cannot be cancelled. _create sees
        # _closed and disposes of their results; wait_closed joins those tasks
        # before asyncio shuts down its executor and cancels pending futures.
        for label, pad in list(self._pads.items()):
            self._close_pad(label, pad)
        self._pads.clear()
        self._pending.clear()

    async def wait_closed(self) -> None:
        """Wait for what worker threads still hold: pads being created, then pads being closed."""
        await asyncio.gather(*list(self._creating.values()), return_exceptions=True)
        await asyncio.gather(*list(self._closing), return_exceptions=True)

    def _close_pad(self, label: str, pad: PadBackend) -> asyncio.Task[None] | None:
        """Close a pad. One whose ``close`` waits (``close_blocks``: a feed process to see out) is
        closed in a worker thread, and the task that follows it is returned: the event loop it
        would hold up carries every controller's traffic."""
        if getattr(pad, "close_blocks", False):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no loop to hold up
                pass
            else:
                task = loop.create_task(self._close_in_thread(label, pad))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
                return task
        try:
            pad.close()
        except Exception as e:
            self.log("gamepad_close_failed", controller=label, error=repr(e))
        return None

    async def _close_in_thread(self, label: str, pad: PadBackend) -> None:
        try:
            await asyncio.to_thread(pad.close)
        except Exception as e:
            self.log("gamepad_close_failed", controller=label, error=repr(e))

    def status(self) -> dict[str, Any]:
        """Per label: the pad's XInput ``slot`` and its counters, or the ``error`` its creation ended
        in; a macOS pad names its ``kind`` and ``profile``, a Linux pad its ``kind`` and ``device``."""
        out: dict[str, Any] = {
            label: {"slot": pad.slot, "frames": self._frames.get(label, 0), "rumbles": self._rumbles.get(label, 0)}
            for label, pad in self._pads.items()
        }
        out.update({label: {"error": error} for label, error in self._failed.items()})
        if self.kind == "ojd":
            for value in out.values():
                value.update(kind="ojd", profile=self.cfg.gamepad_profile)
        if self.kind == "uinput":
            for label, value in out.items():
                value.update(kind="uinput")
                if label in self._pads:
                    value.update(device=getattr(self._pads[label], "device", None))
        return out


__all__ = ["BUTTON_BITS", "MOTOR_SCALE", "PadBackend", "ViGEmPad", "VirtualGamepads", "occupied_xinput_slots"]
