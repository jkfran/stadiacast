"""Audio to the controller's headset jack: Opus in SRTP on the media 5-tuple.

The jack is fed by a normal RTP stream sent on the ICE pair the handshake selected, protected
with the keys the DTLS handshake exported (``EXTRACTOR-dtls_srtp`` -> ``endpoint.srtp_out``)::

    RTP(Opus, PT 111) -> srtp_out.protect_rtp() -> UDP to remote.selected

Two things have to be true before a single frame is audible, and neither is in the SDP:

* **The firmware has to be told to open the path.** ``SetAudioMode(mode=2, f3=True)``
  (``DisplayToController`` #16) selects the WebRTC Opus route; without it the pad accepts the
  packets and plays nothing. ``mode=3`` is rejected as ``Unsupported audio mode: UDP_PCM``.
* **The media must not go through the 0x66 sub-channel.** That tunnel carries input reports
  and RPCs, and stuffing audio into it is silently dropped on real hardware (measured).

Three things the obvious implementation gets wrong, and this module does not:

1. **Its own SSRC and sequence numbers.** The display link also sends input ACKs, and those
   use ``endpoint.local_ssrc`` / ``endpoint.tx_seq``. Sharing them makes the controller see
   one stream whose sequence jumps backwards, which it treats as loss.
2. **Timestamps from the slot index, not from the encoder.** ffmpeg starts at a random
   timestamp and its output pace is not a 20 ms grid. ``slot * 960`` keeps the timeline the
   controller expects: a slot with no frame leaves a hole, and the controller conceals it
   instead of shifting everything after it (which is what crackles).
3. **Pacing in the sender.** ffmpeg hands frames over in bursts, while the controller's
   jitter buffer wants one every 20 ms. ``_pace`` does that on a 4 ms poll: Windows' default
   timer resolution is 15.6 ms, so a bare ``sleep(0.02)`` degenerates into 20/31 ms.

Nothing here captures audio from a sound card: this module only turns a stream of Opus frames
into what the controller accepts. ``Source`` builds the ffmpeg input that produces those frames
when a caller wants one (a tone to prove the path, a file to listen to); a caller that already
has an encoder passes ``None`` to :meth:`AudioSender.start` and hands frames to
:meth:`AudioSender.enqueue` itself. Encoding with ffmpeg is the convenient default because its
RTP muxer gives frame boundaries for free and the local UDP hop costs under a millisecond.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
QUEUE_MAX = 120


def ffmpeg_path() -> str | None:
    """The ``ffmpeg`` on PATH, or None. ``libopus`` is required, not just any build."""
    return shutil.which("ffmpeg")


def _raise_timer_resolution() -> None:
    """Ask Windows for 1 ms timer granularity, so the pacer's 4 ms poll is honoured."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(AttributeError, OSError):
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)


# --------------------------------------------------------------------------- sources


@dataclass
class Source:
    """One ffmpeg input, plus what the sender needs to know about it."""

    args: list[str]
    label: str

    @staticmethod
    def test_tone(hz: float = 440.0, duration: float | None = None) -> Source:
        """A sine tone: enough to prove the path end to end without touching the sound card."""
        spec = f"sine=frequency={hz:.0f}:sample_rate=48000"
        if duration:
            spec += f":duration={duration:.1f}"
        # lavfi renders instantly; -re makes it drip out at real time instead of filling the queue.
        return Source(["-re", "-f", "lavfi", "-i", spec], f"test tone {hz:.0f} Hz")

    @staticmethod
    def from_file(path: str, *, loop: bool = True, duration: float | None = None) -> Source:
        """An audio file. ``-stream_loop -1`` and ``-re`` must precede ``-i`` to have any effect."""
        args = ["-re"]
        if loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", path]
        if duration:
            args += ["-t", f"{duration:.1f}"]
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        return Source(args, f"file {name}")


# --------------------------------------------------------------------------- stats


@dataclass
class AudioStats:
    frames: int = 0
    payload_bytes: int = 0
    wire_bytes: int = 0
    started_at: float = 0.0
    last_frame_at: float = 0.0
    first_rtp_ts: int | None = None
    last_rtp_ts: int | None = None
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
    notes: list[str] = field(default_factory=list)

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
            "notes": self.notes[-6:],
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
        self.sink.enqueue(pkt.payload, pkt.marker, pkt.timestamp)


class AudioSender:
    """Forwards one local Opus stream to the headset jack of one controller."""

    def __init__(
        self,
        endpoint: Any,
        *,
        log: Callable[..., Any] | None = None,
        payload_type: int = 111,
        ssrc: int | None = None,
        bitrate_kbps: int = 64,
        srtp: bool = True,
    ) -> None:
        self.endpoint = endpoint
        self.log = log or (lambda *_a, **_k: None)
        self.payload_type = payload_type
        #: SRTP is what the firmware's own audio path uses. ``srtp=False`` sends bare RTP,
        #: which the fake controller in the tests can read without a crypto context.
        self.srtp = srtp
        #: Audio gets its own SSRC: ``endpoint.local_ssrc`` belongs to the input ACKs.
        self.ssrc = ssrc if ssrc is not None else (0x51A00000 | (int(time.time()) & 0xFFFF))
        self.bitrate_kbps = bitrate_kbps
        #: libopus ``-application``: ``lowdelay`` for games, ``audio`` for music.
        self.application = "lowdelay"
        self.queue_target = QUEUE_TARGET
        self.seq = int(time.time()) & 0xFFFF
        self.stats = AudioStats()
        self.source: Source | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._port: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._pacer: asyncio.Task[None] | None = None
        self._q: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=QUEUE_MAX)
        self._running = False
        self._last_send_at = 0.0

    # ---- lifecycle -----------------------------------------------------------
    async def start(self, source: Source | None = None) -> dict[str, Any]:
        """Start ffmpeg and the pacer. Calling it again replaces the source.

        ``source=None`` starts the pacer alone, with frames coming from :meth:`enqueue`:
        that is what the tests use, and what any caller with its own encoder would do.
        """
        await self.stop()
        self.stats = AudioStats(started_at=time.time())
        self._q.clear()
        self._last_send_at = 0.0
        self.source = source
        _raise_timer_resolution()
        if source is not None:
            exe = ffmpeg_path()
            if exe is None:
                raise RuntimeError("ffmpeg not found on PATH (a build with libopus is required)")
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(lambda: _LocalRtp(self), local_addr=("127.0.0.1", 0))
            self._transport = transport
            self._port = transport.get_extra_info("sockname")[1]
            self._task = asyncio.get_running_loop().create_task(self._supervise(exe, source))
        self._running = True
        self._pacer = asyncio.get_running_loop().create_task(self._pace())
        self.log("audio_started", source=self.source.label if self.source else None, pt=self.payload_type)
        return {
            "playing": True,
            "source": self.source.label if self.source else None,
            "payload_type": self.payload_type,
            "ssrc": f"{self.ssrc:08x}",
        }

    async def stop(self) -> dict[str, Any]:
        self._running = False
        await self._cancel_tasks()
        await self._kill_ffmpeg()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._port = None
        if self.stats.frames:
            self.log("audio_stopped", **self.stats.snapshot())
        return {"playing": False, "stats": self.stats.snapshot()}

    async def _cancel_tasks(self) -> None:
        for name in ("_task", "_pacer"):
            task = getattr(self, name)
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            setattr(self, name, None)

    @property
    def playing(self) -> bool:
        return self._running

    def kill_now(self) -> None:
        """Tear down without awaiting: the event loop is going away."""
        self._running = False
        for name in ("_task", "_pacer"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ---- ffmpeg --------------------------------------------------------------
    def _ffmpeg_cmd(self, exe: str, source: Source) -> list[str]:
        return [
            exe,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            # No -re here: the pacer owns the timing, each source paces itself.
            *source.args,
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
        """Keep ffmpeg alive: a device change or an unplug kills it mid-stream."""
        while self._running:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._ffmpeg_cmd(exe, source),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,  # ffmpeg prints the SDP there
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                self.stats.notes.append(f"spawn failed: {e}")
                self.log("audio_ffmpeg_spawn_failed", error=repr(e))
                await asyncio.sleep(2.0)
                continue
            err = b""
            if self._proc.stderr is not None:
                with contextlib.suppress(Exception):
                    err = await self._proc.stderr.read()
            code = await self._proc.wait()
            self._proc = None
            if not self._running:
                return
            tail = err.decode("utf-8", "replace").strip().splitlines()[-4:]
            self.stats.ffmpeg_restarts += 1
            self.stats.notes.append(f"ffmpeg exit {code}: {' | '.join(tail)[:180]}")
            self.log("audio_ffmpeg_exited", code=code, tail=tail)
            await asyncio.sleep(0.5)

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

    # ---- one frame at a time -------------------------------------------------
    def enqueue(self, payload: bytes, marker: bool, timestamp: int | None = None) -> None:
        """One Opus frame in. Nothing is sent here: the pacer takes it on the grid."""
        if not payload:
            self.stats.dropped += 1
            return
        if self.stats.first_rtp_ts is None and timestamp is not None:
            self.stats.first_rtp_ts = timestamp
        self._q.append((payload, marker))
        self.stats.max_queue = max(self.stats.max_queue, len(self._q))
        # Keep latency down: anything past the target drops the oldest frame (the start-up burst).
        while len(self._q) > self.queue_target:
            self._q.popleft()
            self.stats.trimmed += 1

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
        self.stats.last_rtp_ts = ts
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
        self.stats.wire_bytes += len(pkt)
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
            "queue_target": self.queue_target,
            "queue": len(self._q),
            "stats": self.stats.snapshot(),
        }


def source_from_spec(spec: str) -> Source:
    """Build a :class:`Source` from the ``--audio-source`` spelling.

    ``tone`` (a 440 Hz sine) or ``file:PATH``. Anything else is an error the caller can report
    without a traceback.
    """
    if spec == "tone":
        return Source.test_tone()
    if spec.startswith("file:"):
        path = spec[len("file:") :]
        if not path:
            raise ValueError("--audio-source file: needs a path")
        return Source.from_file(path)
    raise ValueError(f"unknown audio source {spec!r}; use tone or file:PATH")


__all__ = ["SAMPLES_PER_FRAME", "AudioSender", "AudioStats", "Source", "ffmpeg_path", "source_from_spec"]
