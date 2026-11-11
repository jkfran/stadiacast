"""Where system audio comes from: the ``system`` and ``device:NAME`` sources.

The capture runs in this process and the sender pipes the PCM into ffmpeg's stdin, on every
platform, because ffmpeg's own device inputs are not reliable for this: its AVFoundation input
drops about a tenth of the samples on a Mac, audible as a choppy stream.

- macOS captures through PortAudio (the ``sounddevice`` package, part of the ``audio`` extra). It
  has no loopback of its own: what the Mac plays is reached through a virtual output such as
  BlackHole, chosen as the sound output or inside a Multi-Output Device; the first input whose
  name looks like one is the default.
- Linux records the monitor of the sound server's default output with ``parec``, the recorder
  that comes with ``pactl``: PulseAudio and PipeWire both answer it, where PortAudio only reaches
  the monitor on systems with ALSA's PulseAudio plugin and records the microphone on the others.
  A receiver that runs as root under ``sudo`` starts the two tools as the user who called it,
  whose sound server it is. ``device:NAME`` is a PortAudio input, as on macOS.
- Windows captures the default output's WASAPI loopback (:mod:`stadiacast.loopback`).

Both kinds share one shape: a :class:`CaptureDevice` says what PCM format the device delivers
(what ffmpeg's ``pipe:0`` input is told) and makes a fresh :class:`BlockCapture`, which hands
over 20 ms blocks through a bounded queue that the sender reads from a worker thread.
"""

from __future__ import annotations

import abc
import contextlib
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from stadiacast.errors import SetupError

#: Fragments that mark a virtual input carrying the computer's own output.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "virtual", "monitor")

#: Frames per block: 20 ms at 48 kHz, one Opus frame.
BLOCK = 960

#: Blocks a capture holds for a reader that fell behind: about half a second.
QUEUE_BLOCKS = 24

#: What ``parec`` is asked for: the headset stream's own format, so that nothing is resampled.
PULSE_RATE, PULSE_CHANNELS = 48000, 2


class BlockCapture(abc.ABC):
    """A running capture as the sender sees it: interleaved int16 blocks from a bounded queue.

    A subclass opens its device in :meth:`start`, hands every block to :meth:`_push` from
    whatever thread delivers it, and calls :meth:`_mark_end` once no more will come. A full queue
    drops the newest block and counts it in ``overflows``: the stream stays real time instead of
    lagging behind a reader that stalled. One capture per ffmpeg run, since none restarts.
    """

    def __init__(self, name: str, rate: int, channels: int) -> None:
        self.name = name
        self.rate = rate
        self.channels = channels
        #: Why the capture ended, when it failed.
        self.error: str | None = None
        #: Blocks lost to a full queue or reported lost by the device.
        self.overflows = 0
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._ended = False

    @abc.abstractmethod
    def start(self) -> None:
        """Open the device and start delivering blocks."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Close the device; :meth:`read` then returns what is still queued, then ``None``."""

    def read(self, timeout: float = 1.0) -> bytes | None:
        """The oldest block; ``b""`` when none came within ``timeout``, ``None`` once the capture ended."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None if self._ended else b""

    def details(self) -> dict[str, Any]:
        """What else the log should say about a started capture (the loopback names its keepalive)."""
        return {}

    def _push(self, block: bytes) -> None:
        try:
            self._q.put_nowait(block)
        except queue.Full:
            self.overflows += 1  # the reader stalled: drop rather than lag

    def _mark_end(self) -> None:
        """No more blocks: :meth:`read` returns ``None`` once the queue is empty, and the marker
        wakes a reader that is waiting."""
        self._ended = True
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(None)


@dataclass(frozen=True)
class CaptureDevice(abc.ABC):
    """A capture device and the PCM format it delivers: the rate and the one or two channels used."""

    name: str
    rate: int
    channels: int

    @abc.abstractmethod
    def capture(self) -> BlockCapture:
        """A fresh, not yet started capture of this device."""


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ImportError as e:
        raise SetupError("system audio capture needs the audio extra: pip install 'stadiacast[audio]'") from e
    except OSError as e:  # the binding is there but not the library it wraps
        raise SetupError(f"PortAudio is missing ({e}); on Linux install libportaudio2") from e
    except Exception as e:
        # The binding starts PortAudio as it is imported, and a PortAudio that speaks to PulseAudio
        # fails there when no sound server answers: on a computer without one, or for root, whose
        # session has none.
        raise SetupError(
            f"PortAudio cannot start ({e}): it needs the sound server of the user who is logged in, so run"
            " the receiver as that user"
        ) from e
    return sounddevice


@dataclass(frozen=True)
class PortAudioDevice(CaptureDevice):
    """A PortAudio input, opened by its name."""

    def capture(self) -> PortAudioCapture:
        return PortAudioCapture(self)


class PortAudioCapture(BlockCapture):
    """A PortAudio input stream: its callback, on PortAudio's own thread, pushes each block."""

    def __init__(self, device: PortAudioDevice) -> None:
        super().__init__(device.name, device.rate, device.channels)
        self._stream: Any = None

    def _callback(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status and status.input_overflow:
            self.overflows += 1
        self._push(bytes(indata))

    def start(self) -> None:
        if self._ended:  # stopped before it opened
            return
        sd = _sounddevice()
        self._stream = sd.RawInputStream(
            samplerate=self.rate,
            blocksize=BLOCK,
            channels=self.channels,
            dtype="int16",
            device=self.name,
            # "low" asks CoreAudio for 20 ms I/O buffers: the default 85 ms ones hand over four
            # blocks at once, which the sender's pacer would have to trim.
            latency="low",
            callback=self._callback,
        )
        self._stream.start()
        if self._ended:  # stopped while it was opening, on another thread
            self.stop()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:  # a failing close has nothing left to do
                self.error = repr(e)
        self._mark_end()


def portaudio_devices() -> list[PortAudioDevice]:
    """The PortAudio input devices, in index order, each with its default rate and up to two channels."""
    sd = _sounddevice()
    try:
        found = list(sd.query_devices())
    except Exception as e:  # sounddevice.PortAudioError, which cannot be named before the import
        raise SetupError(f"PortAudio cannot list the sound devices ({e})") from e
    return [
        PortAudioDevice(str(d["name"]), int(d["default_samplerate"]), min(2, int(d["max_input_channels"])))
        for d in found
        if int(d["max_input_channels"]) > 0
    ]


def pick_loopback(names: list[str]) -> str | None:
    """The first device that looks like a loopback of the computer's output, if any."""
    for name in names:
        if any(hint in name.lower() for hint in LOOPBACK_HINTS):
            return name
    return None


def sound_server_user() -> dict[str, Any]:
    """How to start a PulseAudio tool so that it reaches the sound server of the person at the
    computer, as keyword arguments for ``subprocess``: nothing for an ordinary user, whose server
    it is. Root has none, so a receiver started with ``sudo`` runs the tool as the user who
    called it, with that user's home and runtime directory."""
    if sys.platform == "win32" or os.geteuid() != 0:
        return {}
    uid = os.environ.get("SUDO_UID", "")
    if not uid.isdigit() or int(uid) == 0:
        return {}
    import pwd

    try:
        entry = pwd.getpwuid(int(uid))
    except (KeyError, OverflowError):  # nobody has that number
        return {}
    try:
        groups = os.getgrouplist(entry.pw_name, entry.pw_gid)
    except (OSError, OverflowError):
        groups = [entry.pw_gid]
    env = dict(os.environ)
    env.update(HOME=entry.pw_dir, USER=entry.pw_name, LOGNAME=entry.pw_name, XDG_RUNTIME_DIR=f"/run/user/{uid}")
    return {"user": entry.pw_uid, "group": entry.pw_gid, "extra_groups": groups, "env": env}


def _pulse_tool(name: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        raise SetupError(
            f"{name} not found: what a Linux computer plays is read from PulseAudio or PipeWire with pactl and"
            " parec (the pulseaudio-utils package); or name a capture device with device:NAME (see --audio-devices)"
        )
    return exe


def linux_default_monitor() -> str:
    """The monitor of the sound server's default output: what the computer plays."""
    exe = _pulse_tool("pactl")
    user = sound_server_user()
    run = subprocess.run([exe, "get-default-sink"], capture_output=True, text=True, timeout=10, check=False, **user)
    sink = run.stdout.strip() if run.returncode == 0 else ""
    if not sink:  # a pactl older than 15 has no such command; its info names the sink
        plain = {**user, "env": {**user.get("env", os.environ), "LC_ALL": "C"}}
        info = subprocess.run([exe, "info"], capture_output=True, text=True, timeout=10, check=False, **plain)
        named = [
            line.partition(":")[2].strip() for line in info.stdout.splitlines() if line.startswith("Default Sink:")
        ]
        sink = named[0] if info.returncode == 0 and named else ""
        if not sink:
            said = (info.stderr or run.stderr).strip().splitlines()
            raise SetupError(
                f"pactl found no default output ({said[-1] if said else 'no answer'}): the sound server belongs to"
                " the user who is logged in, so run the receiver as that user or with sudo from that user's"
                " session; or name a capture device with device:NAME"
            )
    return f"{sink}.monitor"


@dataclass(frozen=True)
class PulseSource(CaptureDevice):
    """A source of the sound server (PulseAudio, or PipeWire speaking its protocol), recorded by ``parec``."""

    executable: str = "parec"

    def capture(self) -> PulseCapture:
        return PulseCapture(self)


class PulseCapture(BlockCapture):
    """``parec`` writing raw PCM into a pipe, which a thread cuts into blocks."""

    def __init__(self, device: PulseSource) -> None:
        super().__init__(device.name, device.rate, device.channels)
        self._executable = device.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        if self._stopped:  # stopped before it opened
            return
        proc = subprocess.Popen(
            [
                self._executable,
                f"--device={self.name}",
                "--format=s16le",
                f"--rate={self.rate}",
                f"--channels={self.channels}",
                "--raw",
                "--latency-msec=20",  # one block at a time: the default hands over seconds at once
                "--client-name=Stadiacast",
                "--stream-name=headset",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **sound_server_user(),
        )
        self._proc = proc
        self._thread = threading.Thread(target=self._read, args=(proc,), name="parec", daemon=True)
        self._thread.start()
        if self._stopped:  # stopped while it was starting, on another thread
            self.stop()

    def _read(self, proc: subprocess.Popen[bytes]) -> None:
        size = BLOCK * self.channels * 2
        assert proc.stdout is not None and proc.stderr is not None
        try:
            while len(block := proc.stdout.read(size)) == size:
                self._push(block)
        except (OSError, ValueError):  # the pipe was closed under the read
            pass
        if not self._stopped:  # it ended by itself: the sound server went away, or never answered
            said = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
            self.error = f"parec ended: {said[-1] if said else 'no message'}"
        with contextlib.suppress(OSError):
            proc.wait(timeout=2)
        self._mark_end()

    def stop(self) -> None:
        self._stopped = True
        proc, self._proc = self._proc, None
        if proc is None:
            if self._thread is None:
                self._mark_end()
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()


def linux_monitor() -> PulseSource:
    """What a Linux computer plays, as a capture device: the default output's monitor."""
    return PulseSource(linux_default_monitor(), PULSE_RATE, PULSE_CHANNELS, executable=_pulse_tool("parec"))


def open_portaudio(name: str | None) -> PortAudioDevice:
    """A capture device by a fragment of its name (exact match first), or the Mac's loopback when ``None``."""
    devices = portaudio_devices()
    names = [d.name for d in devices]
    if name is None:
        name = pick_loopback(names)
        if name is None:
            raise SetupError(
                f"no loopback device among the audio inputs ({', '.join(names) or 'none'}): install BlackHole "
                "(brew install blackhole-2ch) and choose it as the sound output, or name an input with device:NAME"
            )
    key = name.lower()
    match = next((d for d in devices if key == d.name.lower()), None) or next(
        (d for d in devices if key in d.name.lower()), None
    )
    if match is None:
        raise SetupError(f"no capture device matches {name!r}; available: {names}")
    return match


def list_devices() -> list[str]:
    """The capture devices ``--audio-source device:NAME`` accepts on this platform."""
    if sys.platform == "win32":
        from stadiacast import loopback

        return [d.name for d in loopback.devices()]
    return [d.name for d in portaudio_devices()]


__all__ = [
    "BLOCK",
    "LOOPBACK_HINTS",
    "QUEUE_BLOCKS",
    "BlockCapture",
    "CaptureDevice",
    "PortAudioCapture",
    "PortAudioDevice",
    "PulseCapture",
    "PulseSource",
    "linux_default_monitor",
    "linux_monitor",
    "list_devices",
    "open_portaudio",
    "pick_loopback",
    "portaudio_devices",
    "sound_server_user",
]
