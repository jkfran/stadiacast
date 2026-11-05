"""Headset audio: the source spellings, the pacer's numbering, and what a fake controller receives."""

from __future__ import annotations

import asyncio
import types

import pytest

from stadiacast.audio import SAMPLES_PER_FRAME, AudioSender, Source, ffmpeg_path, source_from_spec
from stadiacast.rtc.rtp import parse_rtp
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


class _StubEndpoint:
    """Just enough of ``RtcEndpoint`` for ``AudioSender``: no SRTP, records the RTP it is handed."""

    def __init__(self) -> None:
        self.dtls = types.SimpleNamespace(handshake_complete=True)
        self.srtp_out = None
        self.sent: list[bytes] = []

    def send_rtp_packet(self, packet: bytes, *, protect: bool = True) -> bool:
        assert not protect  # the stub has no SRTP context, so the caller must not ask for one
        self.sent.append(packet)
        return True


def test_source_spec_spellings() -> None:
    tone = source_from_spec("tone")
    assert tone.args[:3] == ["-re", "-f", "lavfi"]
    assert source_from_spec("file:/tmp/a.ogg").args[-2:] == ["-i", "/tmp/a.ogg"]
    for bad in ("", "bogus", "file:"):
        with pytest.raises(ValueError):
            source_from_spec(bad)


def test_file_source_puts_stream_loop_before_the_input() -> None:
    src = Source.from_file("music.ogg", duration=2.0)
    assert src.args.index("-stream_loop") < src.args.index("-i")  # after -i it does nothing
    assert src.args[-2:] == ["-t", "2.0"]
    assert src.label == "file music.ogg"


def test_sender_numbers_frames_on_its_own_grid() -> None:
    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)  # type: ignore[arg-type]
        await sender.start(None)  # no encoder: the frames come from the test
        try:
            for i in range(3):
                sender.enqueue(bytes([0xF8, i]), marker=(i == 0))
            await wait_for(lambda: len(ep.sent) >= 3, timeout=2)
        finally:
            await sender.stop()

        packets = [parse_rtp(pkt) for pkt in ep.sent]
        assert [p.payload_type for p in packets] == [111] * 3
        assert [p.marker for p in packets] == [True, False, False]
        assert {p.ssrc for p in packets} == {sender.ssrc}
        assert packets[1].seq == (packets[0].seq + 1) & 0xFFFF
        assert packets[2].seq == (packets[1].seq + 1) & 0xFFFF
        # Timestamps follow the slot index, so a hole stays a hole instead of shifting the stream.
        assert packets[1].timestamp - packets[0].timestamp == SAMPLES_PER_FRAME
        assert packets[2].timestamp - packets[1].timestamp == SAMPLES_PER_FRAME
        assert [p.payload for p in packets] == [bytes([0xF8, 0]), bytes([0xF8, 1]), bytes([0xF8, 2])]

    asyncio.run(run())


def test_audio_reaches_the_controller(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            assert cs.audio_state() == {"playing": False}
            # The display link has to be past OnConnected, or the pad drops RPCs on the floor.
            await wait_for(lambda: fc.dc_state == "ready", timeout=6)
            await cs.audio_start(None)  # real SRTP: the fake protects/decrypts like the pad does
            for i in range(5):
                cs.audio_feed(bytes([0xF8, i]), marker=(i == 0))
            await wait_for(lambda: len(fc.audio_frames) >= 5, timeout=3)
            frames = fc.audio_frames[:5]
            assert [f.payload for f in frames] == [bytes([0xF8, i]) for i in range(5)]
            assert [f.seq for f in frames] == [(frames[0].seq + i) & 0xFFFF for i in range(5)]
            # The firmware only plays the stream once SetAudioMode(mode=2) opened the path.
            modes = [
                r["payload"].set_audio_mode
                for r in fc.rpc_in
                if r["kind"] == "request" and r["payload"] is not None and r["payload"].HasField("set_audio_mode")
            ]
            assert [(m.mode, m.f3) for m in modes] == [(2, True)]
            # The audio stream must not ride on the endpoint's own SSRC: input ACKs use that one.
            assert frames[0].ssrc != cs.endpoint.local_ssrc
            assert cs.audio_state()["playing"] is True
        finally:
            await cs.audio_stop()
            fc.close()
            await rx.stop()

    asyncio.run(run())


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg (with libopus) on PATH")
def test_ffmpeg_tone_comes_out_of_the_encoder(tmp_path) -> None:
    """The whole chain with a real encoder: ffmpeg -> local RTP -> pacer -> SRTP/UDP."""

    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await cs.audio_start(Source.test_tone(duration=0.5))
            await wait_for(lambda: len(fc.audio_frames) >= 5, timeout=10)
            frames = fc.audio_frames[:5]
            assert {f.payload_type for f in frames} == {111}
            assert all(f.payload for f in frames)  # real Opus, not empty keep-alives
            assert [f.seq for f in frames] == [(frames[0].seq + i) & 0xFFFF for i in range(5)]
        finally:
            await cs.audio_stop()
            fc.close()
            await rx.stop()

    asyncio.run(run())
