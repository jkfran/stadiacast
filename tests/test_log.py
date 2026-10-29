"""stadiacast.log: the JSONL record shape, the console rendering and the in-memory window."""

from __future__ import annotations

import json

from stadiacast.log import EventLog, format_input_event


def test_records_are_json_lines_with_reserved_keys_protected(tmp_path, capsys):
    path = tmp_path / "server.jsonl"
    log = EventLog(str(path), echo=True, keep=3)
    rec = log("ready", port=443, ts="not-the-timestamp", event="not-the-event")
    assert rec["event"] == "ready" and rec["_ts"] == "not-the-timestamp" and rec["_event"] == "not-the-event"
    log("quiet_one", _quiet=True, body_hex="ff" * 10)
    for i in range(5):
        log("tick", n=i)
    log.close()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in lines] == ["ready", "quiet_one"] + ["tick"] * 5
    assert [r["n"] for r in log.records] == [2, 3, 4]  # only the last `keep` stay in memory
    assert log.counts == {"ready": 1, "quiet_one": 1, "tick": 5}
    out = capsys.readouterr().out
    assert "ready port=443" in out and "quiet_one" not in out  # _quiet keeps it off the console


def test_console_rendering_of_http_and_input_records():
    log = EventLog(None, echo=False)
    rec = log(
        "http_request", method="POST", host="h", path="/p", query="a=1", status=200, body_len=3, response_len=0,
        handler="rpc:X", sni="other", note="placeholder", request_headers=[("Host", "h")],
    )  # fmt: skip
    line = log.format(rec)
    assert "http POST h/p?a=1 -> 200 (3B in / 0B out) [rpc:X] sni=other NOTE placeholder" in line
    assert "request_headers" not in line  # the http line is a fixed summary
    generic = log("dtls_connected", role="server", offer={"big": "blob"})
    assert log.format(generic).endswith("dtls_connected role=server")  # bulky fields stay off the console...
    log.verbose = True
    assert "offer={'big': 'blob'}" in log.format(generic)  # ...unless verbose

    rpc = {
        "type": "rpc_input", "buttons": 0x10, "pressed_names": ["menu"], "released_names": [],
        "axes": {"lx": -1234, "ly": 0, "rx": 0, "ry": 0}, "triggers": {"l": 0, "r": 200},
    }  # fmt: skip
    assert format_input_event(rpc) == (
        "[input] press=['menu'] rel=[] buttons=0x00010 lx=-1234 ly=0 rx=0 ry=0 l2=0 r2=200"
    )
    pad = {"type": "gamepad", "id": 0, "t_us": 1_500_000, "buttons": "0x00000010", "pressed": [4], "released": [],
           "axes": {"lx": 1, "ly": 2, "rx": 3, "ry": 4, "lt": 5, "rt": 6}, "registration_info": {}}  # fmt: skip
    assert format_input_event(pad) == (
        "[input] pad0 t=1.500s buttons=0x00000010 press=[4] rel=[] lx=1 ly=2 rx=3 ry=4 lt=5 rt=6 REGISTRATION"
    )
    assert format_input_event({"type": "keyboard_message_v2", "t_us": 0, "data": {"k": 1}}).startswith(
        "[input] keyboard_message_v2 t=0.000s"
    )
