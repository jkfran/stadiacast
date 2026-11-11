"""Audio to the controller's headset jack: Opus in SRTP on the media 5-tuple.

The jack is fed by a normal RTP stream sent on the ICE pair the handshake selected, protected
with the keys the DTLS handshake exported (``EXTRACTOR-dtls_srtp`` -> ``endpoint.srtp_out``)::

    RTP(Opus, PT 111) -> srtp_out.protect_rtp() -> UDP to remote.selected

``docs/protocol.md`` ("Audio to the headset jack") says what the controller needs for a frame to
be audible: ``SetAudioMode(mode=2)`` first, the media on the UDP pair and not in the ``0x66``
sub-channel, a stream with its own SSRC and sequence numbers, timestamps that follow the 20 ms
slots and one frame per slot.  The last three are this module's: :class:`AudioSender` numbers
the stream itself and ``_pace`` sends on the grid, on a 4 ms poll since Windows' default timer
resolution of 15.6 ms turns a bare ``sleep(0.02)`` into 20 or 31 ms.

The frames come from ffmpeg, and a :class:`Source` is its input. A test tone or a file ffmpeg
reads by itself. What the computer plays (``system``) or one capture device (``device:NAME``) is
captured in this process (:mod:`stadiacast.capture`: PortAudio, or the WASAPI loopback on
Windows), and the sender writes the PCM blocks to ffmpeg's stdin. On Windows it first scales them
by the system volume, which the loopback does not apply. Reading a block, scaling it and measuring
its level happen in one worker thread, so no per-sample arithmetic runs on the event loop that
paces every controller's packets. A caller that already has an encoder passes ``None`` to
:meth:`AudioSender.start` and hands frames to :meth:`AudioSender.enqueue` itself. Encoding with
ffmpeg is the convenient default because its RTP muxer gives frame boundaries for free and the
local UDP hop costs under a millisecond.
"""

from __future__ import annotations

import array
import asyncio
import collections
import contextlib
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from stadiacast import capture
from stadiacast.errors import SetupError
from stadiacast.rtc.rtp import build_rtp, parse_rtp

#: Samples per Opus frame at 48 kHz: one 20 ms frame. RTP timestamps step by this.
SAMPLES_PER_FRAME = 960

#: Frame duration in seconds: the controller's jitter buffer is scheduled on this grid.
FRAME_INTERVAL = 0.020

#: Pacer poll interval. Four milliseconds keeps the send times on the grid despite the
#: 15.6 ms timer granularity Windows gives to ``sleep`` by default.
POLL_INTERVAL = 0.004

#: Frames the queue may hold before the oldest are dropped. Six frames = 120 ms of slack.
QUEUE_TARGET = 6

#: Notes a stream keeps: the latest ones, since a failing ffmpeg adds one per restart.
NOTES_KEPT = 6

#: Seconds before ffmpeg is started again after it exited, by how many times in a row it ran for
#: less than :data:`SETTLED_AFTER` seconds.  A device that was unplugged, or an ffmpeg that cannot
#: encode at all, is tried again every ten seconds instead of twice a second.
RESTART_DELAYS = (0.5, 1.0, 2.0, 4.0, 10.0)
SETTLED_AFTER = 5.0

#: Bytes kept of what ffmpeg wrote before it exited: the end of it goes into the log.
STDERR_KEPT = 2048


def ffmpeg_path() -> str | None:
    """The ``ffmpeg`` on PATH, or None. ``libopus`` is required, not just any build."""
    return shutil.which("ffmpeg")


def _set_timer_resolution(raised: bool) -> bool:
    """Ask Windows for 1 ms timer granularity while a stream runs, so the 4 ms poll is honoured.

    Returns whether the call was made: ``timeBeginPeriod`` and ``timeEndPeriod`` must pair up.
    """
    if sys.platform != "win32":
        return False
    with contextlib.suppress(AttributeError, OSError):
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        (winmm.timeBeginPeriod if raised else winmm.timeEndPeriod)(1)
        return True
    return False


class RtpSink(Protocol):
    """What the sender needs from an endpoint: :meth:`RtcEndpoint.send_rtp_packet`."""

    def send_rtp_packet(self, packet: bytes, *, protect: bool = True) -> bool: ...


# --------------------------------------------------------------------------- sources


@dataclass
class Source:
    """One ffmpeg input, plus what the sender needs to know about it."""

    args: list[str]
    label: str
    #: ``args`` end in ``-i pipe:0``: the sender writes the PCM of the capture below to ffmpeg's
    #: stdin (the ``system`` and ``device:NAME`` sources).
    pcm: bool = False
    #: Makes that capture, not yet started. One per ffmpeg run, since a capture does not restart.
    open_capture: Callable[[], capture.BlockCapture] | None = None
    #: The input ends by itself: when ffmpeg exits cleanly the stream is over, not to be restarted.
    finite: bool = False

    @staticmethod
    def system() -> Source:
        """What this computer plays: the platform's loopback (see :mod:`stadiacast.capture`)."""
        return Source.device(None)

    @staticmethod
    def device(name: str | None) -> Source:
        """A capture device by name (``device:NAME``), or the platform's loopback when ``None``.

        Always a PCM pipe into ffmpeg: WASAPI loopback on Windows (:mod:`stadiacast.loopback`),
        the sound server's monitor for what a Linux computer plays, PortAudio for the rest
        (:mod:`stadiacast.capture`).
        """
        dev: capture.CaptureDevice
        if sys.platform == "win32":
            from stadiacast import loopback

            dev = loopback.describe(name)
        elif name is None and sys.platform.startswith("linux"):
            dev = capture.linux_monitor()
        else:
            dev = capture.open_portaudio(name)
        args = ["-f", "s16le", "-ar", str(dev.rate), "-ac", str(dev.channels), "-i", "pipe:0"]
        return Source(args, f"capture {dev.name}", pcm=True, open_capture=dev.capture)

    @staticmethod
    def test_tone(hz: float = 440.0, duration: float | None = None) -> Source:
        """A sine tone: enough to prove the path end to end without touching the sound card."""
        spec = f"sine=frequency={hz:.0f}:sample_rate=48000"
        if duration:
            spec += f":duration={duration:.1f}"
        # lavfi renders instantly; -re makes it drip out at real time instead of filling the queue.
        return Source(["-re", "-f", "lavfi", "-i", spec], f"test tone {hz:.0f} Hz", finite=bool(duration))

    @staticmethod
    def from_file(path: str) -> Source:
        """An audio file, played in a loop. ``-stream_loop -1`` and ``-re`` must precede ``-i`` to
        have any effect."""
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        return Source(["-re", "-stream_loop", "-1", "-i", path], f"file {name}")


# --------------------------------------------------------------------------- stats


@dataclass
class AudioStats:
    """What one run of the sender did: ``/_audio`` shows the snapshot, ``audio_stopped`` logs it."""

    frames: int = 0
    payload_bytes: int = 0
    started_at: float = 0.0
    last_frame_at: float = 0.0
    dropped: int = 0
    send_failed: int = 0
    ffmpeg_restarts: int = 0
    #: A slot went by with nothing queued, so the controller got a hole to conceal.
    underruns: int = 0
    #: Deepest the queue ever got: how far ahead of real time the encoder runs.
    max_queue: int = 0
    #: Frames dropped to keep latency down (ffmpeg bursts at start-up).
    trimmed: int = 0
    gap_ms_last: float = 0.0
    gap_ms_max: float = 0.0
    #: Peak of the last PCM block fed to ffmpeg, 0 to 1: shows whether the gain is doing anything.
    level: float = 0.0
    #: The latest things that went wrong, oldest first.
    notes: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=NOTES_KEPT))

    def snapshot(self) -> dict[str, Any]:
        up = round(time.time() - self.started_at, 1) if self.started_at else None
        return {
            "frames": self.frames,
            "fps": round(self.frames / up, 1) if up else None,
            "kbps": round(self.payload_bytes * 8 / up / 1000, 1) if up and up > 0 else None,
            "uptime": up,
            "since_last_frame": round(time.time() - self.last_frame_at, 2) if self.last_frame_at else None,
            "dropped": self.dropped,
            "send_failed": self.send_failed,
            "ffmpeg_restarts": self.ffmpeg_restarts,
            "underruns": self.underruns,
            "max_queue": self.max_queue,
            "trimmed": self.trimmed,
            "gap_ms_last": self.gap_ms_last,
            "gap_ms_max": round(self.gap_ms_max, 1),
            "level": round(self.level, 3),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the sender


class _LocalRtp(asyncio.DatagramProtocol):
    """ffmpeg's RTP output: one datagram is exactly one Opus frame.

    Frames are only queued here. ffmpeg's pacing is neither even nor on the 20 ms grid, so
    the actual send is ``AudioSender._pace``'s job.
    """

    def __init__(self, sink: AudioSender) -> None:
        self.sink = sink

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        try:
            pkt = parse_rtp(data)
        except ValueError:
            self.sink.stats.dropped += 1
            return
        self.sink.enqueue(pkt.payload, pkt.marker)


class AudioSender:
    """Forwards one local Opus stream to the headset jack of one controller."""

    def __init__(
        self,
        endpoint: RtpSink | None,
        *,
        log: Callable[..., Any] | None = None,
        payload_type: int = 111,
        ssrc: int | None = None,
        bitrate_kbps: int = 64,
        srtp: bool = True,
        volume: float = 1.0,
        application: str = "lowdelay",
        follow_system_volume: bool = True,
    ) -> None:
        self.endpoint = endpoint
        self.log = log or (lambda *_a, **_k: None)
        self.payload_type = payload_type
        #: SRTP is what the firmware's own audio path uses. ``srtp=False`` sends bare RTP, for a
        #: test that looks at the packets themselves.
        self.srtp = srtp
        #: Audio gets its own SSRC: ``endpoint.local_ssrc`` belongs to the input ACKs.
        self.ssrc = ssrc if ssrc is not None else (0x51A00000 | (int(time.time()) & 0xFFFF))
        self.bitrate_kbps = bitrate_kbps
        #: Linear gain applied before encoding; above 1 a limiter guards against clipping.
        self.volume = volume
        #: Windows only: scale a loopback capture by the endpoint volume, so the slider and the
        #: media keys reach the headset (loopback hands over the mix before that attenuation).
        self.follow_system_volume = follow_system_volume
        #: libopus ``-application``: ``lowdelay`` for games, ``audio`` for music.
        self.application = application
        self.seq = int(time.time()) & 0xFFFF
        self.stats = AudioStats()
        self.source: Source | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._port: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._pacer: asyncio.Task[None] | None = None
        self._feed: asyncio.Task[None] | None = None
        self._capture: capture.BlockCapture | None = None
        self._lifecycle = asyncio.Lock()  # start() and stop() run one at a time
        self._killed = False  # kill_now() is final: a start it interrupted must not revive the stream
        self._sysvol = 1.0
        self._sysvol_at = 0.0
        self._sysvol_warned = False
        #: Past the target the oldest frame goes, which keeps latency down (ffmpeg bursts at start-up).
        self._q: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=QUEUE_TARGET)
        self._running = False
        self._last_send_at = 0.0
        self._timer_raised = False

    # ---- lifecycle -----------------------------------------------------------
    async def start(self, source: Source | None = None) -> dict[str, Any]:
        """Start ffmpeg and the pacer. Calling it again replaces the source.

        ``source=None`` starts the pacer alone, with frames coming from :meth:`enqueue`:
        that is what the tests use, and what any caller with its own encoder would do.
        After :meth:`kill_now` nothing starts: the answer says ``playing`` false.
        """
        async with self._lifecycle:
            await self._stop()
            # Looked up before anything starts, so a missing ffmpeg leaves nothing half set up.
            exe = ffmpeg_path() if source is not None else None
            if source is not None and exe is None:
                raise SetupError("ffmpeg not found on PATH (a build with libopus is required)")
            started = False
            if not self._killed:
                self.stats = AudioStats(started_at=time.time())
                self._q.clear()
                self._last_send_at = 0.0
                self.source = source
                started = await self._start_tasks(exe, source)
            return {
                "playing": started,
                "source": self.source.label if self.source else None,
                "payload_type": self.payload_type,
                "ssrc": f"{self.ssrc:08x}",
            }

    async def _start_tasks(self, exe: str | None, source: Source | None) -> bool:
        loop = asyncio.get_running_loop()
        if source is not None and exe is not None:
            transport, _ = await loop.create_datagram_endpoint(lambda: _LocalRtp(self), local_addr=("127.0.0.1", 0))
            if self._killed:  # while the socket was being opened
                transport.close()
                return False
            self._transport = transport
            self._port = transport.get_extra_info("sockname")[1]
            self._task = loop.create_task(self._supervise(exe, source))
        self._timer_raised = _set_timer_resolution(True)
        self._running = True
        self._pacer = loop.create_task(self._pace())
        self.log("audio_started", source=source.label if source else None, pt=self.payload_type)
        return True

    async def stop(self) -> dict[str, Any]:
        async with self._lifecycle:
            return await self._stop()

    async def _stop(self) -> dict[str, Any]:
        was_running, self._running = self._running, False
        self._release_timer()
        await self._cancel_tasks()
        self._stop_capture()  # a feed cancelled while its device was opening has not stopped it
        await self._kill_ffmpeg()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._port = None
        if was_running and self.stats.frames:
            self.log("audio_stopped", **self.stats.snapshot())
        return {"playing": False, "stats": self.stats.snapshot()}

    async def _cancel_tasks(self) -> None:
        for name in ("_feed", "_task", "_pacer"):
            task = getattr(self, name)
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            setattr(self, name, None)

    def _release_timer(self) -> None:
        if self._timer_raised:
            self._timer_raised = False
            _set_timer_resolution(False)

    @property
    def playing(self) -> bool:
        return self._running

    def kill_now(self) -> None:
        """Tear down without awaiting, for good: the controller's session is being reset or
        stopped, and whoever holds this sender drops it."""
        self._killed = True
        self._running = False
        self._release_timer()
        for name in ("_feed", "_task", "_pacer"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)
        self._stop_capture()
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ---- ffmpeg --------------------------------------------------------------
    def _af_args(self) -> list[str]:
        """The gain before encoding. The protocol has no volume field and the controller only
        knows its physical keys, so loudness is set here; a limiter keeps a raised gain from clipping."""
        v = float(self.volume)
        if abs(v - 1.0) < 1e-3:
            return []
        chain = f"volume={v:.4f}"
        if v > 1.0:
            chain += ",alimiter=limit=0.98"
        return ["-af", chain]

    def _ffmpeg_cmd(self, exe: str, source: Source) -> list[str]:
        # -nostdin and an input on stdin exclude each other: the PCM pipe goes without it.
        head = [exe, "-hide_banner", "-loglevel", "warning"] + ([] if source.pcm else ["-nostdin"])
        return [
            *head,
            # No -re here: the pacer owns the timing, each source paces itself.
            *source.args,
            *self._af_args(),
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            f"{self.bitrate_kbps}k",
            "-frame_duration",
            "20",
            # lowdelay has no look-ahead: right for game audio, which must not lag.
            "-application",
            self.application,
            "-compression_level",
            "10",
            "-f",
            "rtp",
            "-payload_type",
            str(self.payload_type),
            f"rtp://127.0.0.1:{self._port}?pkt_size=1200",
        ]

    async def _supervise(self, exe: str, source: Source) -> None:
        """Keep ffmpeg alive: a device change or an unplug kills it mid-stream.  Exits that follow
        each other closely are spaced out (:data:`RESTART_DELAYS`); a finite source that ends
        cleanly ends the stream."""
        quick_exits = 0
        while self._running:
            started = time.monotonic()
            try:
                proc = self._proc = await asyncio.create_subprocess_exec(
                    *self._ffmpeg_cmd(exe, source),
                    stdin=asyncio.subprocess.PIPE if source.pcm else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,  # ffmpeg prints the SDP there
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                self.stats.notes.append(f"spawn failed: {e}")
                self.log("audio_ffmpeg_spawn_failed", error=repr(e))
                await asyncio.sleep(RESTART_DELAYS[-1])
                continue
            if source.pcm and proc.stdin is not None:
                self._feed = asyncio.get_running_loop().create_task(self._feed_pcm(proc.stdin, source))
            # What ffmpeg says is read as it comes, so that its pipe never fills, and only the end
            # of it is kept: a warning repeated for hours would otherwise all stay in memory.
            said = b""
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    while chunk := await proc.stderr.read(4096):
                        said = (said + chunk)[-STDERR_KEPT:]
            code = await proc.wait()
            self._proc = None
            feed, self._feed = self._feed, None
            if feed is not None:
                feed.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await feed
            self._stop_capture()
            if not self._running:
                return
            if source.finite and code == 0:
                while self._q and self._running:  # the pacer takes one frame per slot
                    await asyncio.sleep(FRAME_INTERVAL)
                self._running = False
                self._release_timer()
                self.log("audio_source_ended", source=source.label, **self.stats.snapshot())
                return
            quick_exits = quick_exits + 1 if time.monotonic() - started < SETTLED_AFTER else 0
            tail = said.decode("utf-8", "replace").strip().splitlines()[-4:]
            self.stats.ffmpeg_restarts += 1
            self.stats.notes.append(f"ffmpeg exit {code}: {' | '.join(tail)[:180]}")
            self.log("audio_ffmpeg_exited", code=code, tail=tail)
            await asyncio.sleep(RESTART_DELAYS[max(0, min(quick_exits, len(RESTART_DELAYS)) - 1)])

    async def _kill_ffmpeg(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    # ---- the PCM pipe (system and device captures) -----------------------------
    async def _feed_pcm(self, stdin: asyncio.StreamWriter, source: Source) -> None:
        """Write the capture's PCM blocks to ffmpeg's stdin, scaled by the system volume.

        However this ends (the capture fails to start, the capture ends, ffmpeg goes away), stdin
        is closed: ffmpeg then exits, and the supervisor starts both again.
        """
        try:
            cap = source.open_capture() if source.open_capture else None
            if cap is None:
                raise RuntimeError("the PCM source has no capture to open")
            # Registered before it opens, in a worker thread since opening a sound device takes a
            # while: a stop during the open reaches it, and a capture stopped while it opens closes
            # its device once it has (BlockCapture.stop).
            self._capture = cap
            await asyncio.to_thread(cap.start)
        except Exception as e:
            self.stats.notes.append(f"capture failed: {e!r}")
            self.log("audio_capture_failed", error=repr(e))
            self._stop_capture()
            stdin.close()
            return
        self.log("audio_capture", device=cap.name, rate=cap.rate, channels=cap.channels, **cap.details())
        try:
            while self._running:
                # The gain is read here, on the loop thread: the COM interface sysvolume caches belongs
                # to the thread that created it, and to_thread runs on whichever pool thread is free.
                block, level = await asyncio.to_thread(self._read_scaled, cap, self._system_gain())
                if block is None:  # the capture ended (cap.error says why)
                    self.stats.notes.append(f"capture ended: {cap.error!r}")
                    self.log("audio_capture_ended", error=cap.error)
                    return
                if not block:
                    continue
                self.stats.level = level
                stdin.write(block)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return  # ffmpeg went away; the supervisor restarts it
        finally:
            self._stop_capture()
            with contextlib.suppress(Exception):
                stdin.close()

    def _stop_capture(self) -> None:
        cap, self._capture = self._capture, None
        if cap is not None:
            with contextlib.suppress(Exception):
                cap.stop()

    def _system_gain(self) -> float:
        """Windows' endpoint volume as a gain, read every 0.4 s; 1.0 elsewhere or when unreadable."""
        if not self.follow_system_volume or sys.platform != "win32":
            return 1.0
        now = time.monotonic()
        if now - self._sysvol_at >= 0.4:
            self._sysvol_at = now
            from stadiacast import sysvolume

            value = sysvolume.master_scalar()
            if value is not None and sysvolume.muted():
                value = 0.0  # mute is a switch of its own, the slider need not be at zero
            if value is None:
                if not self._sysvol_warned:
                    self._sysvol_warned = True
                    self.stats.notes.append("the Windows volume could not be read; streaming at full volume")
                value = 1.0
            self._sysvol = value
        return self._sysvol

    @staticmethod
    def _read_scaled(cap: capture.BlockCapture, gain: float) -> tuple[bytes | None, float]:
        """Worker thread: wait for one capture block, scale it by ``gain`` and measure its peak.

        Both walk every sample (1,920 per stereo block, fifty blocks a second), so they run here
        rather than on the event loop that paces every controller's RTP and DTLS.
        """
        block = cap.read(0.5)
        if not block:
            return block, 0.0
        scaled = AudioSender._scale_pcm(block, gain)
        return scaled, AudioSender._peak(scaled)

    @staticmethod
    def _scale_pcm(chunk: bytes, gain: float) -> bytes:
        """Multiply interleaved int16 PCM by a gain from 0 to 1 (the system volume).

        An attenuation never leaves the int16 range, so no sample needs clipping, and ``map`` over
        the array keeps the multiplication out of Python bytecode, about three times faster than a
        loop.
        """
        gain = min(1.0, max(0.0, gain))
        if not chunk or gain > 1.0 - 1e-3:
            return chunk
        samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
        return array.array("h", map(int, map(gain.__mul__, samples))).tobytes()

    @staticmethod
    def _peak(chunk: bytes) -> float:
        """The largest sample magnitude of interleaved int16 PCM, from 0 to 1."""
        samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
        return max(max(samples), -min(samples)) / 32768.0 if samples else 0.0

    # ---- one frame at a time -------------------------------------------------
    def enqueue(self, payload: bytes, marker: bool) -> None:
        """One Opus frame in. Nothing is sent here: the pacer takes it on the grid."""
        if not payload:
            self.stats.dropped += 1
            return
        if len(self._q) == QUEUE_TARGET:
            self.stats.trimmed += 1  # the append below pushes the oldest frame out
        self._q.append((payload, marker))
        self.stats.max_queue = max(self.stats.max_queue, len(self._q))

    async def _pace(self) -> None:
        """Send one frame per 20 ms slot, timestamps from the slot index (see the module docstring)."""
        loop = asyncio.get_running_loop()
        # Wait for the first frame so start-up delay does not land on the timeline.
        while self._running and not self._q:
            await asyncio.sleep(POLL_INTERVAL)
        t0 = loop.time()
        slot = 0
        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            due = int((loop.time() - t0) / FRAME_INTERVAL)
            if due - slot > 10:  # suspend or a long stall: resync instead of bursting
                self.stats.notes.append(f"pacer resync {due - slot} slots")
                self.log("audio_pacer_resync", skipped=due - slot)
                slot = due
            while slot < due and self._running:
                if self._q:
                    payload, marker = self._q.popleft()
                    self._emit(payload, marker, slot)
                else:
                    self.stats.underruns += 1
                slot += 1

    def _emit(self, payload: bytes, marker: bool, slot: int) -> None:
        ts = (slot * SAMPLES_PER_FRAME) & 0xFFFFFFFF
        pkt = build_rtp(self.payload_type, self.seq, ts, self.ssrc, payload, marker)
        if not self._send_rtp(pkt):
            self.stats.send_failed += 1
            return
        now = time.time()
        if self._last_send_at:
            gap = (now - self._last_send_at) * 1000.0
            self.stats.gap_ms_last = round(gap, 1)
            self.stats.gap_ms_max = max(self.stats.gap_ms_max, gap)
        self._last_send_at = now
        self.seq = (self.seq + 1) & 0xFFFF
        self.stats.frames += 1
        self.stats.payload_bytes += len(payload)
        self.stats.last_frame_at = now

    def _send_rtp(self, rtp_packet: bytes) -> bool:
        """SRTP, then the UDP socket, to the candidate the handshake selected."""
        ep = self.endpoint
        if ep is None:
            return False
        try:
            return bool(ep.send_rtp_packet(rtp_packet, protect=self.srtp))
        except Exception as e:  # one bad frame must not take the send loop with it
            self.stats.notes.append(f"send: {e!r}")
            return False

    def state(self) -> dict[str, Any]:
        return {
            "playing": self._running,
            "source": self.source.label if self.source else None,
            "payload_type": self.payload_type,
            "ssrc": f"{self.ssrc:08x}",
            "transport": "srtp/udp" if self.srtp else "rtp/udp",
            "application": self.application,
            "bitrate_kbps": self.bitrate_kbps,
            "volume": self.volume,
            "follow_system_volume": self.follow_system_volume,
            "queue_target": QUEUE_TARGET,
            "queue": len(self._q),
            "stats": self.stats.snapshot(),
        }


def source_from_spec(spec: str) -> Source:
    """Build a :class:`Source` from the ``--audio-source`` spelling.

    ``tone`` (a 440 Hz sine), ``file:PATH``, ``system`` (what this computer plays) or
    ``device:NAME`` (one capture device, see ``--audio-devices``). A bad spelling is a
    ``ValueError``; a capture that cannot be set up on this machine is a
    :class:`~stadiacast.errors.SetupError`. Resolving a capture device enumerates the sound
    cards (and runs ``pactl`` on Linux), so an event loop calls this from a worker thread.
    """
    if spec == "tone":
        return Source.test_tone()
    if spec == "system":
        return Source.system()
    if spec.startswith("device:"):
        name = spec[len("device:") :]
        if not name:
            raise ValueError("--audio-source device: needs a name (see --audio-devices)")
        return Source.device(name)
    if spec.startswith("file:"):
        path = spec[len("file:") :]
        if not path:
            raise ValueError("--audio-source file: needs a path")
        # ffmpeg takes a URL where it takes a path: only a file of this computer is a source
        if not os.path.isfile(path):
            raise ValueError(f"--audio-source file: there is no file {path!r}")
        return Source.from_file(path)
    raise ValueError(f"unknown audio source {spec!r}; use tone, file:PATH, system or device:NAME")


__all__ = ["SAMPLES_PER_FRAME", "AudioSender", "AudioStats", "RtpSink", "Source", "ffmpeg_path", "source_from_spec"]
