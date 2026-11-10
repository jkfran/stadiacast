"""What the controller reports about itself: the hardware fields, the battery gauge, the headset."""

from __future__ import annotations

import asyncio

from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


def test_hardware_battery_and_headset_reach_the_status(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()  # the first periodic request is what enters READY
            await wait_for(lambda: cs.session["display"]["state"] == "ready")
            hardware = [r for r in rx.log.records if r["event"] == "controller_hardware"]
            assert len(hardware) == 1
            seen = (hardware[0]["firmware"], hardware[0]["hardware"], hardware[0]["headset"])
            assert seen == ("282115", "gotham_pvt_3", False)
            assert cs.status()["headset"] is False  # the f4 snapshot: nothing in the jack
            assert cs.status()["battery_percent"] is None  # the gauge has not reported yet

            fc.send_battery(0.76)
            await wait_for(lambda: cs.status()["battery_percent"] == 76)
            fc.send_battery(0.76)  # a repeat is counted but kept off the console
            await wait_for(lambda: cs.session["display"]["battery_samples"] == 2)
            assert rx.log.counts["controller_battery"] == 2
            assert cs.session["display"]["battery"] == 0.76

            fc.send_audio_config(1)
            await wait_for(lambda: cs.status()["headset"] is True)
            assert cs.session["display"]["audio_mode"] == 1
            fc.send_audio_config(0)
            await wait_for(lambda: cs.status()["headset"] is False)
            audio = [r for r in rx.log.records if r["event"] == "controller_audio_config"]
            assert len(audio) == 2  # other records may follow: the periodic ones are not ours to time
            assert audio[-1]["audio"]["device_info"]["f6"] == 9989
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_the_serial_falls_back_to_the_controller_state(tmp_path, monkeypatch) -> None:
    """A controller that sends no controller-device-id header is still named by its serial: f17."""
    monkeypatch.setattr(FakeController, "rpc_headers", lambda _self: [])

    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()  # the first periodic request is what enters READY
            await wait_for(lambda: cs.session["display"]["state"] == "ready")
            assert cs.serial == "FAKESERIAL0001"
            hardware = next(r for r in rx.log.records if r["event"] == "controller_hardware")
            assert hardware["serial"] == "FAKESERIAL0001"
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
