"""The game session: StartGame, the proxied InitializationRequest, the second ICE/DTLS session, input on
its 0x66 channel, the RPC channel and rumble with the haptics watchdog."""

from __future__ import annotations

import asyncio
import json

from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


def test_start_game_sent_once_ready_and_its_reply_held(tmp_path):
    """--start-game: DisplayToController #8 StartGame {key, 1} goes out once per boot,
    start_game_delay after READY (the first keepalive / time request), never before;
    the controller holds the reply until GAME_STARTED, which ends it OK.
    POST /_start_game sends one by hand."""

    async def run():
        rx = make_receiver(
            tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1, start_game="auto", start_game_delay=0.1
        )
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await asyncio.sleep(0.3)
            assert fc.start_game_requests == []
            assert single(rx).session["game"]["state"] == "idle"
            assert (await fc.keepalive())["code"] == 0  # R/C/READY seen by the receiver
            await wait_for(lambda: fc.start_game_requests)
            req = fc.start_game_requests[0]
            g = single(rx).session["game"]
            assert req["number"] == 1
            assert req["key"] == g["key"]
            assert 0 < len(req["key"]) <= 23
            assert g["state"] == "start-game-sent"
            assert g["start_game_ticket"] == req["ticket"]
            rec = next(r for r in rx.log.records if r["event"] == "rpc_out" and r.get("purpose") == "start_game")
            assert bytes.fromhex(rec["record"])[2:4] == b"\x75\x00"  # display RPC id
            # READY traffic and input go on while the reply is held; still one StartGame
            # (input moves the display state READY -> input, which re-runs the StartGame check)
            assert (await fc.keepalive())["code"] == 0
            await fc.get_current_time()
            assert fc.send_input_rpc(buttons=0x1)
            assert (await rx.wait_event(5))["pressed"] == [0]
            assert single(rx).session["display"]["state"] == "input"
            await asyncio.sleep(0.3)
            assert len(fc.start_game_requests) == 1
            assert g["state"] == "start-game-sent"
            assert fc.finish_start_game(ok=True)
            await wait_for(lambda: single(rx).session["game"]["state"] == "game-started")
            assert g["result"]["code"] == 0
            assert g["result"]["after_s"] >= 0
            assert any(r["event"] == "game_started" for r in rx.log.records)
            # by hand: refused by the fake (no longer in BROWSE), reported as a failure
            port = rx.http.http_ports[0]
            st, _, body = await fc.request("POST", "whatever.local", "/_start_game?key=manual-1", port=port, tls=False)
            assert st == 200
            assert b'"sent": true' in body
            await wait_for(lambda: single(rx).session["game"]["state"] == "game-start-failed")
            assert fc.start_game_requests[-1]["key"] == "manual-1"
            assert g["result"]["code"] == 9
            st, _, _ = await fc.request("POST", "whatever.local", "/_start_game?key=" + "k" * 24, port=port, tls=False)
            assert st == 400
            st, _, _ = await fc.request("GET", "whatever.local", "/_start_game", port=port, tls=False)
            assert st == 405
            # a new boot: fresh game session, one more automatic StartGame with a new key
            fc.close()
            fc = await connect_firmware_flow(rx)
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert single(rx).session["game"]["state"] == "idle"
            assert single(rx).session["game"]["start_game_sent"] == 0
            assert (await fc.keepalive())["code"] == 0
            await wait_for(lambda: fc.start_game_requests)
            await asyncio.sleep(0.3)
            assert len(fc.start_game_requests) == 1
            assert fc.start_game_requests[0]["key"] != req["key"]
            assert single(rx).session["game"]["start_game_sent"] == 1
            assert single(rx).session["game"]["state"] == "start-game-sent"
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_game_session_initialization_request_answered_with_a_second_endpoint(tmp_path):
    """After StartGame the controller's StreamingClient sends its InitializationRequest
    through the display link (ControllerToDisplay #1 ProxyRequest, inner RpcMessage);
    the answer (DisplayToController #1, inner response on the inner ticket) points at
    the game endpoint: its own UDP port, certificate and a 6-char ICE ufrag."""

    async def run():
        rx = make_receiver(
            tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1, start_game="auto", start_game_delay=0.05
        )
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert (await fc.keepalive())["code"] == 0
            await wait_for(lambda: fc.start_game_requests)
            key = fc.start_game_requests[0]["key"]
            init = await fc.send_initialization_request(key)
            ans, cand, ge = init["answer"], init["candidate"], single(rx).game.endpoint
            assert init["rpc"]["code"] == 0
            assert init["inner"]["ticket"] == 0
            assert init["inner"]["code"] == 0
            assert ge.local_port != single(rx).endpoint.local_port
            assert (cand.local_endpoint_ip, cand.local_endpoint_port) == ("127.0.0.1", ge.local_port)
            assert ans.ice_ufrag == ge.ufrag
            assert len(ans.ice_ufrag) == 6
            assert ans.ice_password == ge.pwd
            assert ans.fingerprint == single(rx).game.cert.fingerprint != single(rx).cert.fingerprint
            assert ans.raw_dtls  # echoed from the game offer
            assert ge.remote.ufrag == fc.game_endpoint.ufrag
            assert ge.remote.fingerprint == fc.game_endpoint.cert.fingerprint
            assert not ge.controlling
            assert ge.dtls_role == "server"
            g = single(rx).session["game"]
            assert g["state"] == "init-answered"
            assert g["init_answered"] == 1
            assert g["proxy"]["key"] == key
            assert g["init"] == {"version": 1, "f3": 7, "f4": "fake-f4"}
            assert g["udp"] == ["127.0.0.1", ge.local_port]
            rec = [r for r in rx.log.records if r["event"] == "rpc_out" and r.get("purpose") == "proxy_response"][-1]
            assert bytes.fromhex(rec["record"])[2:4] == b"\x75\x00"
            assert next(r for r in rx.log.records if r["event"] == "game_init_answered")["too_long"] is None
            # a new game session: fresh ICE credentials on the same socket
            init2 = await fc.send_initialization_request(key)
            assert init2["answer"].ice_ufrag != ans.ice_ufrag
            assert init2["candidate"].local_endpoint_port == ge.local_port
            assert ge.remote.ufrag == fc.game_endpoint.ufrag
            assert g["init_answered"] == 2
            # malformed / unknown proxied payloads
            p = rx.protos
            bad = p.ControllerToDisplay()
            bad.proxy_request.f1 = 1
            bad.proxy_request.body = b"\xff\xff"
            assert (await fc.rpc_call(bad))["code"] == 3  # INVALID_ARGUMENT
            bad.proxy_request.body = p.rpc_request(9, p.StreamerRequest())
            rpc = await fc.rpc_call(bad)
            inner = p.parse_rpc(rpc["payload"].proxy_response.data, p.StreamerResponse)
            assert rpc["code"] == 0
            assert inner["ticket"] == 9
            assert inner["code"] == 12
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_game_session_connects_starts_the_game_and_carries_input(tmp_path):
    """The whole game phase against the fake's game mode: StartGame -> proxied
    InitializationRequest -> ICE + DTLS (controller = client) to the game endpoint ->
    GAME_STARTED answers StartGame; input then arrives on the game session's 0x66
    channel (RTP-in-WireHeader2 or the bare message) and is acked the same way; the
    display link keeps working; a request on the game RPC channel gets an empty OK."""

    async def run(framing: str, sub: str):
        (tmp_path / sub).mkdir()
        rx = make_receiver(
            tmp_path / sub, display_rpc_delay=0.05, display_rpc_interval=0.1, start_game="auto", start_game_delay=0.05
        )
        await rx.start()
        fc = await connect_firmware_flow(rx, game_mode=True, game_framing=framing)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert (await fc.keepalive())["code"] == 0
            await wait_for(lambda: single(rx).session["game"]["state"] in ("game-started", "game-start-failed"), 8)
            g = single(rx).session["game"]
            assert g["state"] == "game-started"
            assert fc.game_error is None
            assert fc.app_state == "in_game"
            assert single(rx).game.endpoint.dtls.role == "server"
            assert fc.game_endpoint.dtls.role == "client"
            assert g["connected"]["fingerprint_verified"] is True
            assert g["connected"]["since_init_s"] >= 0
            names = [r["event"] for r in rx.log.records]
            assert names.index("game_init_answered") < names.index("game_connected") < names.index("game_started")
            assert single(rx).endpoint.stats["dtls_in"] > 0
            assert single(rx).game.endpoint.stats["dtls_in"] > 0
            # input on the game session
            seq = fc.send_game_input(buttons=0x1000, lx=500)
            ev = await rx.wait_event(5)
            assert ev["via"] == "game"
            assert ev["type"] == "gamepad"
            assert ev["pressed"] == [12]
            assert ev["axes"]["lx"] == 500
            assert await fc.wait_game_ack(5) == seq
            seq = fc.send_game_input(buttons=0)
            ev = await rx.wait_event(5)
            assert ev["released"] == [12]
            assert await fc.wait_game_ack(5) == seq
            assert g["input_framing"] == framing
            assert g["wire_in"] == {"0x0066": 2}
            assert g["acks_sent"] == 2
            assert fc.game_framing_errors == 0
            assert fc.game_rx_framing == [framing, framing]
            # the display link is untouched
            assert (await fc.keepalive())["code"] == 0
            assert fc.send_input_rpc(buttons=0x8000)
            ev = await rx.wait_event(5)
            assert ev["type"] == "rpc_input"
            assert ev["pressed"] == [15]
            # the game RPC channel: 0xFFFA answered on 0xFFFA, 0x74 on 0x75
            assert fc.send_game_rpc(0xFFFA, ticket=5)
            assert fc.send_game_rpc(0x74, ticket=6)
            await wait_for(lambda: len(fc.game_rpc_in) == 2)
            assert [(i, m["response"]["ticket"]) for i, m in fc.game_rpc_in] == [(0xFFFA, 5), (0x75, 6)]
            for _, m in fc.game_rpc_in:  # an empty OK: code 0 (to_dict shows the enum name), no payload
                assert m["response"].get("code", "OK") == "OK"
                assert "payload" not in m["response"]
            assert g["rpc_in"] == 2
            assert g["rpc_out"] == 2
            assert g["rpc_kinds"] == {"request": 2}
            rx.cfg.game_rpc_reply = "unimplemented"
            assert fc.send_game_rpc(0xFFFA, ticket=7)
            await wait_for(lambda: len(fc.game_rpc_in) == 3)
            assert fc.game_rpc_in[-1][1]["response"]["code"] == "UNIMPLEMENTED"
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run("rtp", "rtp"))
    asyncio.run(run("raw", "raw"))


def test_rumble_on_the_game_session_and_the_watchdog_refresh(tmp_path):
    """POST /_rumble -> InputFeedbackMessage_V2.gamepad_vibration on the game session's
    0x66 channel (the framing and gamepad id of the controller's own input).  A level
    longer than the haptics watchdog stays on because it is re-sent; `ms` ends it with
    an explicit 0/0; without `ms` it is held until the next call."""

    async def run():
        rx = make_receiver(
            tmp_path,
            display_rpc_delay=0.05,
            display_rpc_interval=0.1,
            start_game="auto",
            start_game_delay=0.05,
            rumble_refresh=0.2,
        )
        await rx.start()
        fc = await connect_firmware_flow(rx, game_mode=True, game_framing="raw", haptics_watchdog=0.5)
        port = rx.http.http_ports[0]

        async def rumble(body, method="POST"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            st, _, out = await fc.request(
                method, "whatever.local", "/_rumble", raw, "application/json", port=port, tls=False
            )
            return st, (json.loads(out) if out else None)

        try:
            st, out = await rumble({"left": 1000, "right": 1000})
            assert st == 409
            assert out["sent"] is False
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert (await fc.keepalive())["code"] == 0
            await wait_for(lambda: single(rx).session["game"]["state"] == "game-started", 8)
            fc.send_game_input(gid=2, buttons=0)  # the controller's own series: raw, gamepad 2
            await rx.wait_event(5)
            # a short pulse, stopped by our 0/0 (not by the watchdog)
            st, out = await rumble({"left": 65535, "right": 32768, "ms": 300})
            assert st == 200
            assert out["sent"]
            assert out["framing"] == "raw"
            await wait_for(lambda: fc.motors == (65535, 32768))
            vib = fc.game_feedback[-1].gamepad_vibration
            assert (vib.id, vib.left_motor_speed, vib.right_motor_speed) == (2, 65535, 32768)
            await wait_for(lambda: fc.motors == (0, 0), 2)
            assert fc.watchdog_expired == 0
            # sustained, 3 watchdog periods: kept on by the refresh, then stopped
            writes = fc.motor_writes
            st, _ = await rumble({"left": 20000, "right": 20000, "ms": 1500})
            await asyncio.sleep(1.2)
            assert st == 200
            assert fc.motors == (20000, 20000)
            assert fc.watchdog_expired == 0
            await wait_for(lambda: fc.motors == (0, 0), 2)
            assert fc.watchdog_expired == 0
            assert fc.motor_writes - writes >= 7
            assert any(r["event"] == "rumble_done" for r in rx.log.records)
            # held until changed (query form), then stopped
            st, _, _ = await fc.request("POST", "whatever.local", "/_rumble?left=1000&right=0", port=port, tls=False)
            await asyncio.sleep(0.8)
            assert st == 200
            assert fc.motors == (1000, 0)
            assert fc.watchdog_expired == 0
            st, out = await rumble({"left": 0, "right": 0})
            await wait_for(lambda: fc.motors == (0, 0))
            writes = fc.motor_writes
            await asyncio.sleep(0.5)
            assert fc.motor_writes == writes
            assert out["rumble"]["left"] == 0
            # without the refresh the watchdog would stop it (the fake models the 5000 ms timer)
            rx.cfg.rumble_refresh = 10.0
            await rumble({"left": 5000, "right": 5000})
            await wait_for(lambda: fc.watchdog_expired == 1 and fc.motors == (0, 0), 2)
            await rumble({"left": 0, "right": 0})
            # per-call framing: RTP to a session reading bare messages is dropped, raw moves the motors
            assert fc.game_framing_errors == 0
            assert set(fc.game_rx_framing) == {"raw"}
            writes = fc.motor_writes
            st, out = await rumble({"left": 3000, "right": 3000, "ms": 200, "framing": "rtp"})
            await asyncio.sleep(0.4)
            assert st == 200
            assert out["framing"] == "rtp"
            assert fc.motor_writes == writes
            assert fc.game_framing_errors >= 2
            st, out = await rumble({"left": 3000, "right": 3000, "ms": 200, "framing": "raw"})
            await wait_for(lambda: fc.motors == (3000, 3000))
            await wait_for(lambda: fc.motors == (0, 0))
            assert out["framing"] == "raw"
            assert fc.watchdog_expired == 1
            # every series is new: strictly increasing sequence numbers
            seqs = fc.game_feedback_seqs
            assert seqs == sorted(set(seqs))
            assert seqs[0] == 1
            # bad input: rejected before anything is sent
            writes, sent = fc.motor_writes, single(rx).session["game"]["rumble"]["sent"]
            assert (await rumble({"left": 70000, "right": 0}))[0] == 400
            assert (await rumble({"left": 1, "right": 1, "ms": 0}))[0] == 400
            for ms in (b"NaN", b"Infinity", b"1e300", b"3600001"):
                assert (await rumble(b'{"left": 9, "right": 9, "ms": ' + ms + b"}"))[0] == 400
            st, _, _ = await fc.request(
                "POST", "whatever.local", "/_rumble?left=9&right=9&ms=inf", port=port, tls=False
            )
            assert st == 400
            assert (await rumble({"left": 9, "right": 9, "framing": "smoke"}))[0] == 400
            assert (await rumble(b"{nope"))[0] == 400
            await asyncio.sleep(0.1)
            assert fc.motor_writes == writes
            assert single(rx).session["game"]["rumble"]["sent"] == sent
            assert fc.motors == (0, 0)
            st, out = await rumble(b"", method="GET")
            assert st == 200
            assert out["game_state"] == "game-started"
            assert (await rumble(b"", method="PUT"))[0] == 405
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
