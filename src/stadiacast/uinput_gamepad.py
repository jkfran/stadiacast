"""Linux virtual pads through the kernel's uinput device.

``--gamepad uinput`` gives each controller an input device that the kernel, and with it SDL,
Steam and Proton, take for a wired Xbox 360 controller: its buttons, axes and ranges are the
ones the ``xpad`` driver gives that pad, and so is its identity (USB ``045e:028e``). The module
talks to ``/dev/uinput`` itself, with ``ioctl``, ``struct`` and ``ctypes``, so the output needs
no package and no compiler: a kernel with uinput and the right to open the device.

A game's rumble arrives on the same file as force feedback requests: an effect is uploaded,
played, stopped and erased, and :class:`Rumble` turns what plays into the two motor levels over
time, the way the kernel does it for a pad of this kind. The mapping and that bookkeeping are
plain Python and run in the tests of every platform; :class:`UinputPad` needs Linux.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import struct
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from stadiacast.errors import SetupError

# ---- the kernel's numbers (linux/input-event-codes.h, linux/input.h, linux/uinput.h) -----
EV_SYN, EV_KEY, EV_ABS, EV_FF, EV_UINPUT = 0x00, 0x01, 0x03, 0x15, 0x0101
SYN_REPORT = 0
BTN_A, BTN_B, BTN_X, BTN_Y = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR = 0x136, 0x137
BTN_SELECT, BTN_START, BTN_MODE = 0x13A, 0x13B, 0x13C
BTN_THUMBL, BTN_THUMBR = 0x13D, 0x13E
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
FF_RUMBLE, FF_PERIODIC, FF_GAIN = 0x50, 0x51, 0x60
FF_SQUARE, FF_TRIANGLE, FF_SINE = 0x58, 0x59, 0x5A  # the waveforms of a periodic effect
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2  # the codes of an EV_UINPUT event
BUS_USB = 0x03

#: What the pad says it is: the wired Xbox 360 controller, as the ``xpad`` driver names it.
PAD_NAME = "Microsoft X-Box 360 pad"
PAD_VENDOR, PAD_PRODUCT, PAD_VERSION = 0x045E, 0x028E, 0x0114
#: Force feedback effects a game can keep on the pad at once (``xpad`` has as many).
FF_EFFECTS = 16
#: What the pad offers a game, which is what the kernel offers for an ``xpad`` pad: rumble, the
#: periodic effects it plays as rumble, and a gain.
FF_BITS = (FF_RUMBLE, FF_PERIODIC, FF_SQUARE, FF_TRIANGLE, FF_SINE, FF_GAIN)
#: Where the kernel's uinput device has been found.
UINPUT_PATHS = ("/dev/uinput", "/dev/input/uinput", "/dev/misc/uinput")

#: XInput ``wButtons`` bit -> key code. ``xpad`` reports the X button as ``BTN_X`` (which the
#: headers also call BTN_NORTH) and Y as ``BTN_Y``; the order of the codes is what gives SDL its
#: button numbers. Bits 0..3, the D-pad, are the hat axes.
KEYS: dict[int, int] = {
    4: BTN_START,
    5: BTN_SELECT,
    6: BTN_THUMBL,
    7: BTN_THUMBR,
    8: BTN_TL,
    9: BTN_TR,
    10: BTN_MODE,
    12: BTN_A,
    13: BTN_B,
    14: BTN_X,
    15: BTN_Y,
}
#: Axis -> (minimum, maximum, fuzz, flat), as ``xpad`` sets them.
AXES: dict[int, tuple[int, int, int, int]] = {
    ABS_X: (-32768, 32767, 16, 128),
    ABS_Y: (-32768, 32767, 16, 128),
    ABS_Z: (0, 255, 0, 0),
    ABS_RX: (-32768, 32767, 16, 128),
    ABS_RY: (-32768, 32767, 16, 128),
    ABS_RZ: (0, 255, 0, 0),
    ABS_HAT0X: (-1, 1, 0, 0),
    ABS_HAT0Y: (-1, 1, 0, 0),
}

#: ``struct input_event``: a timestamp of two longs, then type, code and value.
EVENT = struct.Struct("llHHi")
#: Nothing pressed, everything centred.
NEUTRAL: dict[str, int] = {
    "wButtons": 0,
    "bLeftTrigger": 0,
    "bRightTrigger": 0,
    "sThumbLX": 0,
    "sThumbLY": 0,
    "sThumbRX": 0,
    "sThumbRY": 0,
}


def machine_name() -> str:
    """The kernel's name for this processor (``uname -m``); empty on a system without ``uname``,
    where no request is ever made."""
    return os.uname().machine if hasattr(os, "uname") else ""


def ioc(direction: str, kind: str, number: int, size: int, machine: str | None = None) -> int:
    """An ``ioctl`` request number: ``direction`` is ``""``, ``"r"``, ``"w"`` or ``"rw"`` as seen
    from the program, ``kind`` the driver's letter. Most architectures share one layout
    (asm-generic/ioctl.h); Alpha, MIPS, PowerPC and SPARC have three direction bits and thirteen
    size bits, and PA-RISC has the common layout with read and write the other way round."""
    machine = (machine_name() if machine is None else machine).lower()
    if machine.startswith(("alpha", "mips", "ppc", "powerpc", "sparc")):
        size_bits, none, read, write = 13, 1, 2, 4
    elif machine.startswith("parisc"):
        size_bits, none, read, write = 14, 0, 1, 2
    else:
        size_bits, none, read, write = 14, 0, 2, 1
    bits = (read if "r" in direction else 0) | (write if "w" in direction else 0) or none
    return (bits << (16 + size_bits)) | (size << 16) | (ord(kind) << 8) | number


class _Setup(ctypes.Structure):  # struct uinput_setup
    _fields_ = [
        ("bustype", ctypes.c_uint16),
        ("vendor", ctypes.c_uint16),
        ("product", ctypes.c_uint16),
        ("version", ctypes.c_uint16),
        ("name", ctypes.c_char * 80),
        ("ff_effects_max", ctypes.c_uint32),
    ]


class _AbsSetup(ctypes.Structure):  # struct uinput_abs_setup
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("value", ctypes.c_int32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("fuzz", ctypes.c_int32),
        ("flat", ctypes.c_int32),
        ("resolution", ctypes.c_int32),
    ]


class _Envelope(ctypes.Structure):  # struct ff_envelope
    _fields_ = [
        ("attack_length", ctypes.c_uint16),
        ("attack_level", ctypes.c_uint16),
        ("fade_length", ctypes.c_uint16),
        ("fade_level", ctypes.c_uint16),
    ]


class _Periodic(ctypes.Structure):  # struct ff_periodic_effect: the union's widest member
    _fields_ = [
        ("waveform", ctypes.c_uint16),
        ("period", ctypes.c_uint16),
        ("magnitude", ctypes.c_int16),
        ("offset", ctypes.c_int16),
        ("phase", ctypes.c_uint16),
        ("envelope", _Envelope),
        ("custom_len", ctypes.c_uint32),
        ("custom_data", ctypes.c_void_p),
    ]


class _RumbleEffect(ctypes.Structure):  # struct ff_rumble_effect
    _fields_ = [("strong_magnitude", ctypes.c_uint16), ("weak_magnitude", ctypes.c_uint16)]


class _EffectUnion(ctypes.Union):
    _fields_ = [("periodic", _Periodic), ("rumble", _RumbleEffect)]


class _Effect(ctypes.Structure):  # struct ff_effect
    _fields_ = [
        ("type", ctypes.c_uint16),
        ("id", ctypes.c_int16),
        ("direction", ctypes.c_uint16),
        ("trigger_button", ctypes.c_uint16),
        ("trigger_interval", ctypes.c_uint16),
        ("replay_length", ctypes.c_uint16),
        ("replay_delay", ctypes.c_uint16),
        ("u", _EffectUnion),
    ]


class _Upload(ctypes.Structure):  # struct uinput_ff_upload
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32), ("effect", _Effect), ("old", _Effect)]


class _Erase(ctypes.Structure):  # struct uinput_ff_erase
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32), ("effect_id", ctypes.c_uint32)]


_INT = ctypes.sizeof(ctypes.c_int)
UI_DEV_CREATE = ioc("", "U", 1, 0)
UI_DEV_SETUP = ioc("w", "U", 3, ctypes.sizeof(_Setup))
UI_ABS_SETUP = ioc("w", "U", 4, ctypes.sizeof(_AbsSetup))
UI_SET_EVBIT = ioc("w", "U", 100, _INT)
UI_SET_KEYBIT = ioc("w", "U", 101, _INT)
UI_SET_ABSBIT = ioc("w", "U", 103, _INT)
UI_SET_FFBIT = ioc("w", "U", 107, _INT)
UI_SET_PHYS = ioc("w", "U", 108, ctypes.sizeof(ctypes.c_void_p))
UI_BEGIN_FF_UPLOAD = ioc("rw", "U", 200, ctypes.sizeof(_Upload))
UI_END_FF_UPLOAD = ioc("w", "U", 201, ctypes.sizeof(_Upload))
UI_BEGIN_FF_ERASE = ioc("rw", "U", 202, ctypes.sizeof(_Erase))
UI_END_FF_ERASE = ioc("w", "U", 203, ctypes.sizeof(_Erase))
_SYSNAME_BYTES = 64
UI_GET_SYSNAME = ioc("r", "U", 44, _SYSNAME_BYTES)


def pad_values(xinput: Mapping[str, int]) -> dict[tuple[int, int], int]:
    """An XInput-layout state as the pad's keys and axes: ``(type, code) -> value``.

    The sticks' Y axes point down on Linux, and ``xpad`` reports them as the complement of what
    the pad sends, so that both ends of the range stay inside it.
    """
    mask = xinput["wButtons"]
    values = {(EV_KEY, code): 1 if mask & (1 << bit) else 0 for bit, code in KEYS.items()}

    def stick(name: str) -> int:
        return max(-32768, min(32767, xinput[name]))

    values.update(
        {
            (EV_ABS, ABS_X): stick("sThumbLX"),
            (EV_ABS, ABS_Y): ~stick("sThumbLY"),
            (EV_ABS, ABS_RX): stick("sThumbRX"),
            (EV_ABS, ABS_RY): ~stick("sThumbRY"),
            (EV_ABS, ABS_Z): max(0, min(255, xinput["bLeftTrigger"])),
            (EV_ABS, ABS_RZ): max(0, min(255, xinput["bRightTrigger"])),
            (EV_ABS, ABS_HAT0X): (1 if mask & 8 else 0) - (1 if mask & 4 else 0),
            (EV_ABS, ABS_HAT0Y): (1 if mask & 2 else 0) - (1 if mask & 1 else 0),
        }
    )
    return values


def report(values: Mapping[tuple[int, int], int], before: Mapping[tuple[int, int], int]) -> bytes:
    """The input events that turn the state ``before`` into ``values``, closed by a
    ``SYN_REPORT``; empty when nothing changed."""
    changed = [(key, value) for key, value in values.items() if before.get(key) != value]
    if not changed:
        return b""
    events = [EVENT.pack(0, 0, kind, code, value) for (kind, code), value in changed]
    return b"".join(events) + EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)


def effect_levels(effect: Any) -> tuple[int, int] | None:
    """The strong and the weak motor level of a ``struct ff_effect``, 0..65535 each, or ``None``
    for a kind of effect the pad does not play.

    A periodic effect shakes both motors at twice its magnitude, which is how the kernel plays
    one on a pad that can only rumble; its waveform and its envelope are not followed.
    """
    if effect.type == FF_RUMBLE:
        return effect.u.rumble.strong_magnitude, effect.u.rumble.weak_magnitude
    if effect.type == FF_PERIODIC:
        level = min(0xFFFF, abs(effect.u.periodic.magnitude) * 0xFFFF // 0x7FFF)
        return level, level
    return None


class Rumble:
    """What the force feedback effects on a pad add up to at a given moment.

    The kernel hands a uinput device every request as the game made it: an effect is uploaded
    into a slot, played a number of times and stopped, and the gain scales all of them. This
    class does with them what the kernel does for the pads whose driver can only set two motors
    (its ff-memless): an effect lasts ``length`` ms after ``delay`` ms, or until it is stopped
    when its length is 0; played ``count`` times it runs that often, one run after the other;
    an effect that is changed while it plays starts its run again; and effects that play
    together add up.
    """

    def __init__(self) -> None:
        self.gain = 0xFFFF
        self._effects: dict[int, tuple[int, int, int, int]] = {}  # slot -> strong, weak, length ms, delay ms
        self._playing: dict[int, tuple[float, int]] = {}  # slot -> started at, count

    def upload(self, slot: int, strong: int, weak: int, length_ms: int, delay_ms: int, now: float) -> None:
        """Set or replace the effect in ``slot``. One that is playing starts its run again from
        ``now`` with the new values, the runs it has finished staying finished."""
        if slot in self._playing:
            started, count = self._playing[slot]
            _strong, _weak, length, delay = self._effects[slot]
            finished = max(0, round((now - started) * 1000)) // (delay + length) if length else 0
            if finished >= count:
                del self._playing[slot]
            else:
                self._playing[slot] = (now, count - finished)
        self._effects[slot] = (strong & 0xFFFF, weak & 0xFFFF, max(0, length_ms), max(0, delay_ms))

    def erase(self, slot: int) -> None:
        self._effects.pop(slot, None)
        self._playing.pop(slot, None)

    def play(self, slot: int, count: int, now: float) -> None:
        """Start the effect in ``slot`` from its beginning, ``count`` times; 0 stops it."""
        if count > 0 and slot in self._effects:
            self._playing[slot] = (now, count)
        else:
            self._playing.pop(slot, None)

    def _phase(self, slot: int, now: float) -> tuple[bool, float | None] | None:
        """Whether the playing effect in ``slot`` shakes at ``now`` and when that changes next;
        ``None`` once it has run out. Counted in whole milliseconds, as the effect is given."""
        _strong, _weak, length, delay = self._effects[slot]
        started, count = self._playing[slot]
        elapsed = max(0, round((now - started) * 1000))
        if not length:  # lasts until it is stopped
            return (True, None) if elapsed >= delay else (False, started + delay / 1000)
        run = delay + length
        index = elapsed // run
        if index >= count:
            return None
        begun = index * run
        if elapsed - begun < delay:
            return False, started + (begun + delay) / 1000
        # with no delay between the runs the effect shakes through to the end of the last one
        return True, started + (begun + run if delay else count * run) / 1000

    def levels(self, now: float) -> tuple[int, int]:
        """The strong and the weak motor at ``now``, 0..65535 each. Effects that have run out are
        forgotten on the way."""
        strong = weak = 0
        for slot in list(self._playing):
            phase = self._phase(slot, now)
            if phase is None:
                del self._playing[slot]
            elif phase[0]:
                strong += self._effects[slot][0] * self.gain // 0xFFFF
                weak += self._effects[slot][1] * self.gain // 0xFFFF
        return min(strong, 0xFFFF), min(weak, 0xFFFF)

    def next_change(self, now: float) -> float | None:
        """When the levels change next without a further request, if they do."""
        times = [phase[1] for slot in self._playing if (phase := self._phase(slot, now)) and phase[1] is not None]
        return min(times, default=None)


def open_uinput() -> int:
    """Open the kernel's uinput device, or say in a :class:`SetupError` what keeps it shut."""
    if not sys.platform.startswith("linux"):
        raise SetupError("--gamepad uinput is Linux only: it creates the pad through the kernel's uinput device")
    denied: str | None = None
    for path in UINPUT_PATHS:
        try:
            return os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            continue
        except PermissionError:
            denied = path
        except OSError as e:
            raise SetupError(f"--gamepad uinput cannot open {path}: {e}") from e
    if denied:
        raise SetupError(
            f"--gamepad uinput may not open {denied}: run the receiver as root, or give your user the"
            " device with the udev rule in the README (Virtual gamepad on Linux)"
        )
    raise SetupError(
        "--gamepad uinput needs the kernel's uinput device and found none (/dev/uinput): load it with"
        " `sudo modprobe uinput`"
    )


def check_available() -> None:
    """Fail at start-up, in plain words, where a pad could not be created later."""
    os.close(open_uinput())


class UinputPad:
    """One virtual Xbox 360 pad. Its force feedback requests are answered on a thread of its own,
    since a game waits inside the kernel until each of them is: an answer then never waits for
    the event loop to come round to it."""

    slot: int | None = None  # Linux has no XInput player slot
    # Declared here because a type checker on another system does not read past the first lines
    # of __init__.
    label: str
    device: str | None  # the event node, e.g. /dev/input/event7
    _ioctl: Any
    _poll: Any
    _values: dict[tuple[int, int], int]
    _rumble: Rumble
    _sent: tuple[int, int]
    _callback: Callable[[int, int], None] | None
    _lock: threading.Lock  # the callback and what was last sent to it
    _closed: bool
    _fd: int
    _wake_r: int
    _wake_w: int
    _thread: threading.Thread

    def __init__(self, label: str) -> None:
        if not sys.platform.startswith("linux"):
            raise SetupError("--gamepad uinput is Linux only: it creates the pad through the kernel's uinput device")
        import fcntl
        import select

        self.label = label
        self.device = None
        self._ioctl = fcntl.ioctl
        self._values = {}
        self._rumble = Rumble()
        self._sent = (0, 0)
        self._callback = None
        self._lock = threading.Lock()
        self._closed = False
        self._fd = open_uinput()
        self._wake_r, self._wake_w = os.pipe()
        try:
            self._create()
        except OSError as e:
            self._close_files()
            raise SetupError(f"the kernel refused the virtual pad: {e}") from e
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN)
        self._poll.register(self._wake_r, select.POLLIN)
        self._thread = threading.Thread(target=self._feedback_loop, name=f"uinput-{label}", daemon=True)
        self._thread.start()

    # ---- the device ----------------------------------------------------------------------
    def _create(self) -> None:
        fd, ioctl = self._fd, self._ioctl
        for kind in (EV_KEY, EV_ABS, EV_FF):
            ioctl(fd, UI_SET_EVBIT, kind)
        for code in KEYS.values():
            ioctl(fd, UI_SET_KEYBIT, code)
        for bit in FF_BITS:
            ioctl(fd, UI_SET_FFBIT, bit)
        setup = _Setup(BUS_USB, PAD_VENDOR, PAD_PRODUCT, PAD_VERSION, PAD_NAME.encode(), FF_EFFECTS)
        ioctl(fd, UI_DEV_SETUP, setup)
        for code, (minimum, maximum, fuzz, flat) in AXES.items():
            ioctl(fd, UI_SET_ABSBIT, code)
            ioctl(fd, UI_ABS_SETUP, _AbsSetup(code, 0, minimum, maximum, fuzz, flat, 0))
        with contextlib.suppress(OSError):  # a name for the pad's place, where a kernel takes one
            ioctl(fd, UI_SET_PHYS, ctypes.create_string_buffer(f"stadiacast/{self.label}".encode()))
        ioctl(fd, UI_DEV_CREATE)
        self.device = self._event_node()

    def _event_node(self) -> str | None:
        name = ctypes.create_string_buffer(_SYSNAME_BYTES)
        try:
            self._ioctl(self._fd, UI_GET_SYSNAME, name)
            entries = os.listdir(f"/sys/devices/virtual/input/{name.value.decode()}")
        except (OSError, UnicodeDecodeError):
            return None
        return next((f"/dev/input/{entry}" for entry in sorted(entries) if entry.startswith("event")), None)

    def _close_files(self) -> None:
        for fd in (self._fd, self._wake_r, self._wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)

    # ---- input ---------------------------------------------------------------------------
    def apply(self, xinput: Mapping[str, int]) -> None:
        values = pad_values(xinput)
        data = report(values, self._values)
        if not data:
            return
        try:
            os.write(self._fd, data)
        except OSError as e:
            raise SetupError(f"the virtual pad no longer takes reports: {e}") from e
        self._values = values

    def reset(self) -> None:
        self.apply(NEUTRAL)

    # ---- rumble --------------------------------------------------------------------------
    def on_rumble(self, callback: Callable[[int, int], None]) -> None:
        """``callback`` gets the two motor levels as XInput has them, 0..255, on the pad's thread."""
        with self._lock:
            self._callback = callback
            self._sent = (0, 0)
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"r")  # a game may have started an effect before anyone listened

    def _feedback_loop(self) -> None:
        while True:
            due = self._rumble.next_change(time.monotonic())
            wait_ms = None if due is None else int(max(0.0, due - time.monotonic()) * 1000) + 1
            try:
                ready = self._poll.poll(wait_ms)
            except OSError:
                return
            for fd, _what in ready:
                if fd == self._wake_r:
                    if b"x" in os.read(fd, 16):  # close() asks the thread to end
                        return
                    continue
                try:
                    data = os.read(fd, EVENT.size * 32)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _sec, _usec, kind, code, value = EVENT.unpack_from(data, offset)
                    self._request(kind, code, value)
            self._tell()

    def _request(self, kind: int, code: int, value: int) -> None:
        """One event read from the uinput file: a request to answer, or an effect to play or stop."""
        if kind == EV_UINPUT and code == UI_FF_UPLOAD:
            upload = _Upload(request_id=value & 0xFFFFFFFF)
            with contextlib.suppress(OSError):  # a request the kernel has given up waiting for
                self._ioctl(self._fd, UI_BEGIN_FF_UPLOAD, upload)
                effect = upload.effect
                levels = effect_levels(effect)
                if levels is None:
                    upload.retval = -errno.EINVAL
                else:
                    length, delay = effect.replay_length, effect.replay_delay
                    self._rumble.upload(effect.id, *levels, length, delay, time.monotonic())
                    upload.retval = 0
                self._ioctl(self._fd, UI_END_FF_UPLOAD, upload)
        elif kind == EV_UINPUT and code == UI_FF_ERASE:
            erase = _Erase(request_id=value & 0xFFFFFFFF)
            with contextlib.suppress(OSError):
                self._ioctl(self._fd, UI_BEGIN_FF_ERASE, erase)
                self._rumble.erase(erase.effect_id)
                erase.retval = 0
                self._ioctl(self._fd, UI_END_FF_ERASE, erase)
        elif kind == EV_FF and code == FF_GAIN:
            self._rumble.gain = max(0, min(0xFFFF, value))
        elif kind == EV_FF and code < FF_GAIN:
            self._rumble.play(code, value, time.monotonic())

    def _tell(self) -> None:
        """Hand the motor levels to the callback when they are not the ones it got last."""
        strong, weak = self._rumble.levels(time.monotonic())
        levels = (strong >> 8, weak >> 8)
        with self._lock:
            callback = self._callback
            if callback is None or levels == self._sent:
                return
            self._sent = levels
        callback(*levels)

    # ---- lifecycle -----------------------------------------------------------------------
    def close(self) -> None:
        """Remove the pad from the system. Requests of a game that are still waiting fail there."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._callback = None
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")
        if self._thread is not threading.current_thread():
            self._thread.join(2)
        # Closing the file is what removes the device. Asking for it with UI_DEV_DESTROY first
        # takes the kernel's locks in an order that a game's request, coming the other way at
        # that moment, can cross on kernels without the fix for it; a close cannot.
        self._close_files()


__all__ = [
    "PAD_NAME",
    "PAD_PRODUCT",
    "PAD_VENDOR",
    "Rumble",
    "UinputPad",
    "check_available",
    "effect_levels",
    "open_uinput",
    "pad_values",
    "report",
]
