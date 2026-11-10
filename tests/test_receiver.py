"""The receiver's limits on what it keeps and on how long a request may hold it."""

from __future__ import annotations

import asyncio
import os
import shutil
import time

from stadiacast import receiver as receiver_module
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, make_receiver, no_log, wait_for


def test_input_events_nobody_reads_do_not_pile_up(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver_module, "EVENTS_KEPT", 5)

    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        try:
            for n in range(12):
                rx.publish_event({"n": n})
            assert rx.events.qsize() == 5
            assert [(await rx.wait_event(1))["n"] for _ in range(5)] == [7, 8, 9, 10, 11]  # the newest
        finally:
            await rx.stop()

    asyncio.run(run())


def test_an_omaha_request_is_answered_in_bounded_time_and_cannot_write_markup(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)

        async def update_check(body: bytes) -> bytes:
            st, _, answer = await fc.request("POST", "update.googleapis.com", "/service/update2", body, "text/xml")
            assert st == 200
            return answer

        try:
            started = time.monotonic()
            await update_check(b"<app " * 400_000)  # 2 MB that never closes a tag
            assert time.monotonic() - started < 2
            answer = await update_check(b'<request><app appid="a&b<c"><updatecheck/></app></request>')
            assert b'<app appid="a&amp;b&lt;c" status="ok">' in answer
            many = b"".join(b'<app appid="{%d}"/>' % n for n in range(40))
            assert (await update_check(b"<request>" + many + b"</request>")).count(b"<app ") == 16
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_a_large_body_is_kept_whole_on_disk_and_decoded_only_at_its_start(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        body = b"\x08\x05" * 1_000_000  # two megabytes of valid protobuf: a million fields
        try:
            st, _, _ = await fc.request("POST", "x.local", "/unknown", body, "application/x-protobuf")
            assert st == 404
        finally:
            fc.close()
            await rx.stop()
        record = [r for r in rx.log.records if r["event"] == "http_request"][-1]
        assert record["body_len"] == len(body) and os.path.getsize(record["body_file"]) == len(body)
        assert len(record["decoded"]) == receiver_module.LOGGED_BODY_BYTES // 2
        assert os.path.getsize(os.path.join(rx.cfg.log_dir, "server.jsonl")) < 1_000_000

    asyncio.run(run())


def test_counters_are_logged_while_a_controller_sends_and_not_for_what_only_the_receiver_sends(tmp_path):
    async def run():
        rx = make_receiver(tmp_path, display_rpc_delay=0.05, rtcp_interval=0.02)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        stats = asyncio.get_running_loop().create_task(rx._stats_loop(interval=0.03))

        def logged() -> int:
            return sum(1 for r in rx.log.records if r["event"] == "rtc_stats")

        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await wait_for(lambda: logged() >= 1)
            fc.close()  # gone without a word: the receiver goes on with its reports and checks
            await asyncio.sleep(0.15)
            settled, reports = logged(), rx.single().endpoint.stats["rtcp_out"]
            await asyncio.sleep(0.3)
            assert rx.single().endpoint.stats["rtcp_out"] > reports + 5  # it sends
            assert logged() == settled  # and writes no record for it
        finally:
            stats.cancel()
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_a_body_that_cannot_be_saved_does_not_cost_the_request_its_answer(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        try:
            shutil.rmtree(os.path.join(rx.cfg.log_dir, "bodies"))  # what a full disk does to the write
            st, _, body = await fc.request(
                "POST", "update.googleapis.com", "/service/update2", b"<request/>", "text/xml"
            )
            assert st == 200 and b"<response" in body
        finally:
            fc.close()
            await rx.stop()
        assert rx.log.counts.get("body_not_saved") == 1
        assert [r["body_file"] for r in rx.log.records if r["event"] == "http_request"] == [None]

    asyncio.run(run())
