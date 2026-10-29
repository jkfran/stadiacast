"""Summarize the controller input recorded in a receiver run.

Reads ``<run>/server.jsonl`` (the ``input`` records of type ``rpc_input``) and prints, per
controller, every button press in order with the button name and how long it was held, plus
the range each stick axis and trigger covered.

    stadiacast-show-input captures/<stamp> [--since HH:MM:SS]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from stadiacast.gamepad import button_name


def _ts(rec: dict[str, Any]) -> float:
    return datetime.datetime.fromisoformat(rec["ts"]).timestamp()


def _hms(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def load_input_events(path: str, since: str = "00:00:00") -> list[dict[str, Any]]:
    """The ``rpc_input`` records of a ``server.jsonl`` at or after the local time ``since``."""
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if (
                rec.get("event") == "input"
                and rec.get("type") == "rpc_input"
                and datetime.datetime.fromtimestamp(_ts(rec)).strftime("%H:%M:%S") >= since
            ):
                events.append(rec)
    return events


def _print_presses(events: list[dict[str, Any]]) -> None:
    """Every press paired with its release, in order."""
    down: dict[int, float] = {}
    presses: list[tuple[float, int, float | None]] = []
    for rec in events:
        t = _ts(rec)
        for b in rec.get("pressed", []):
            down[b] = t
        for b in rec.get("released", []):
            if b in down:
                start = down.pop(b)
                presses.append((start, b, t - start))
    presses += [(start, b, None) for b, start in down.items()]
    presses.sort()
    prev = None
    for i, (start, b, held) in enumerate(presses, 1):
        gap = f"+{start - prev:6.2f}s" if prev is not None else " " * 8
        held_s = f"held {held:.2f}s" if held is not None else "still held"
        print(f"  #{i:<3d} {_hms(start)} {gap}  {button_name(b):<10s} (bit {b:2d})  {held_s}")
        prev = start


def _print_ranges(events: list[dict[str, Any]]) -> None:
    """The range each stick axis and trigger covered."""
    for group, names in (("axes", ("lx", "ly", "rx", "ry")), ("triggers", ("l", "r"))):
        for name in names:
            vals = [rec.get(group, {}).get(name, 0) for rec in events]
            nonzero = [v for v in vals if v]
            if nonzero:
                print(f"  {group}.{name}: {len(nonzero)} non-zero reports, range {min(nonzero)} .. {max(nonzero)}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="stadiacast-show-input", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run", help="run directory (captures/<stamp>) or a server.jsonl file")
    ap.add_argument("--since", default="00:00:00", help="only events at or after this local time (HH:MM:SS)")
    a = ap.parse_args(argv)
    path = a.run if a.run.endswith(".jsonl") else os.path.join(a.run, "server.jsonl")
    events = load_input_events(path, a.since)
    print(f"{len(events)} input reports in {path}")
    by_controller: dict[str, list[dict[str, Any]]] = {}
    for rec in events:
        by_controller.setdefault(rec.get("controller", "?"), []).append(rec)
    for label, recs in by_controller.items():
        if len(by_controller) > 1 or label != "?":
            print(f"\n== controller {label}: {len(recs)} reports ==")
        _print_presses(recs)
        _print_ranges(recs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
