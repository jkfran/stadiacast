"""stadiacast.show_input: the per-controller summary of a run's input records."""

from __future__ import annotations

import datetime
import json

from stadiacast.show_input import load_input_events, main


def write_run(tmp_path, base: datetime.datetime) -> str:
    def ts(offset_s: float) -> str:
        return (base + datetime.timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")

    def rec(offset_s, controller="c0", pressed=(), released=(), lx=0, r=0):
        return {
            "ts": ts(offset_s), "event": "input", "type": "rpc_input", "controller": controller,
            "pressed": list(pressed), "released": list(released),
            "axes": {"lx": lx, "ly": 0, "rx": 0, "ry": 0}, "triggers": {"l": 0, "r": r},
        }  # fmt: skip

    records = [
        {"ts": ts(0), "event": "ready"},
        rec(1.0, pressed=[12]),
        rec(1.25, released=[12], lx=-5000),
        rec(2.0, pressed=[16], lx=12000, r=255),
        rec(1.5, controller="c1", pressed=[0]),
        rec(1.6, controller="c1", released=[0]),
        {
            "ts": ts(3),
            "event": "input",
            "type": "gamepad",
            "controller": "c0",
            "pressed": [1],
        },  # game-session input: not summarized
    ]
    (tmp_path / "server.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_summary_per_controller(tmp_path, capsys):
    base = datetime.datetime(2026, 1, 1, 10, 0, 0).astimezone()  # local time, so --since compares like the tool
    run = write_run(tmp_path, base)
    assert main([run]) == 0
    out = capsys.readouterr().out
    assert out.startswith("5 input reports in ")
    assert "== controller c0: 3 reports ==" in out
    assert "== controller c1: 2 reports ==" in out
    c0 = out.split("== controller c0")[1].split("== controller c1")[0]
    assert "a          (bit 12)  held 0.25s" in c0
    assert "stadia     (bit 16)  still held" in c0
    assert "axes.lx: 2 non-zero reports, range -5000 .. 12000" in c0
    assert "triggers.r: 1 non-zero reports, range 255 .. 255" in c0
    c1 = out.split("== controller c1")[1]
    assert "dpad_up    (bit  0)  held 0.10s" in c1


def test_since_filter_and_a_direct_jsonl_path(tmp_path, capsys):
    base = datetime.datetime(2026, 1, 1, 10, 0, 0).astimezone()
    run = write_run(tmp_path, base)
    assert len(load_input_events(run + "/server.jsonl")) == 5
    assert len(load_input_events(run + "/server.jsonl", since="10:00:01.5"[:8])) == 5  # same second: kept
    assert len(load_input_events(run + "/server.jsonl", since="10:00:02")) == 1
    assert main([run + "/server.jsonl", "--since", "10:00:02"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 input reports in ")
    assert "stadia" in out
    assert "c1" not in out
