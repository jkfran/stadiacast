"""Structured event log: JSON Lines on disk plus a one-line console echo per event.

Every component of the receiver reports through one :class:`EventLog` instance.
A record is ``{"ts": ..., "event": ..., **fields}``; the console line is a compact
rendering of the same record with the bulky fields left out unless ``verbose``.
"""

from __future__ import annotations

import datetime
import json
import threading
from collections import deque
from typing import Any, ClassVar, Protocol


def now_iso() -> str:
    """Local wall-clock time with offset, millisecond precision (the ``ts`` of every record)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def format_input_event(ev: dict[str, Any]) -> str:
    """One console line for a decoded input event (display-link report or game-session gamepad message)."""
    kind = ev.get("type")
    if kind == "rpc_input":
        axes, triggers = ev.get("axes", {}), ev.get("triggers", {})
        via = f" via={ev['via']}" if ev.get("via") else ""
        return (
            f"[input] press={ev.get('pressed_names')} rel={ev.get('released_names')} "
            f"buttons={ev.get('buttons', 0):#07x} lx={axes.get('lx')} ly={axes.get('ly')} "
            f"rx={axes.get('rx')} ry={axes.get('ry')} l2={triggers.get('l')} r2={triggers.get('r')}{via}"
        )
    t = ev.get("t_us", 0) / 1e6
    if kind == "gamepad":
        a = ev.get("axes", {})
        return (
            f"[input] pad{ev.get('id')} t={t:.3f}s buttons={ev.get('buttons')} "
            f"press={ev.get('pressed')} rel={ev.get('released')} "
            f"lx={a.get('lx')} ly={a.get('ly')} rx={a.get('rx')} ry={a.get('ry')} lt={a.get('lt')} rt={a.get('rt')}"
            + (f" presence={ev['device_presence']}" if "device_presence" in ev else "")
            + (f" type={ev['device_type']}" if "device_type" in ev else "")
            + (" REGISTRATION" if "registration_info" in ev else "")
        )
    return f"[input] {kind} t={t:.3f}s {ev.get('data', '')}"


class EventLogger(Protocol):
    """What components need from a log: call it with an event name and fields."""

    def __call__(self, event: str, /, _quiet: bool = False, **fields: Any) -> dict[str, Any]: ...


class BoundLog:
    """A view of an :class:`EventLog` that adds fixed fields to every record, e.g. ``controller="c1"``."""

    def __init__(self, log: EventLog, **context: Any) -> None:
        self._log = log
        self.context = context

    def __call__(self, event: str, /, _quiet: bool = False, **fields: Any) -> dict[str, Any]:
        return self._log(event, _quiet=_quiet, **{**self.context, **fields})

    def bind(self, **context: Any) -> BoundLog:
        return BoundLog(self._log, **{**self.context, **context})


class EventLog:
    """Append-only JSONL log with an optional console echo.

    Call the instance to record an event: ``log("dtls_connected", role="server")``.
    ``_quiet=True`` keeps a record out of the console (it is still written to disk).
    The last ``keep`` records stay in memory for ``/_status`` and the tests.
    """

    #: Fields that are only shown on the console with ``verbose``.
    BULKY: ClassVar[frozenset[str]] = frozenset(
        {
            "headers", "body_hex", "body_text", "decoded", "response_hex", "offer", "answer", "ev",
            "request_headers", "response_headers", "clearcut", "omaha_request", "body_file", "controller_state",
        }
    )  # fmt: skip

    def __init__(self, path: str | None, verbose: bool = False, echo: bool = True, keep: int = 200) -> None:
        self.path = path
        self.verbose = verbose
        self.echo = echo
        self.records: deque[dict[str, Any]] = deque(maxlen=keep)
        self.counts: dict[str, int] = {}
        self._fh = open(path, "a", buffering=1, encoding="utf-8") if path else None  # noqa: SIM115
        self._lock = threading.Lock()

    def __call__(self, event: str, /, _quiet: bool = False, **fields: Any) -> dict[str, Any]:
        """Record one event and return the record.

        Fields named ``ts`` or ``event`` are stored as ``_ts`` / ``_event`` so they cannot
        clobber the record's own keys.
        """
        for reserved in ("ts", "event"):
            if reserved in fields:
                fields[f"_{reserved}"] = fields.pop(reserved)
        rec: dict[str, Any] = {"ts": now_iso(), "event": event, **fields}
        with self._lock:
            self.counts[event] = self.counts.get(event, 0) + 1
            if self._fh:
                self._fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
            self.records.append(rec)
        if self.echo and not _quiet:
            try:
                line = self.format(rec)
            except Exception as e:  # formatting must never take the server down
                line = f"[{rec['ts'][11:23]}] {event} (format error {e!r}) {json.dumps(fields, default=str)[:300]}"
            print(line, flush=True)
        return rec

    def bind(self, **context: Any) -> BoundLog:
        """A logger that stamps ``context`` (e.g. the controller label) on every record."""
        return BoundLog(self, **context)

    def format(self, rec: dict[str, Any]) -> str:
        """The console rendering of a record: ``[time] [controller] event k=v ...``."""
        ts = f"[{rec['ts'][11:23]}]" + (f" [{rec['controller']}]" if rec.get("controller") else "")
        ev = rec["event"]
        if ev == "input":
            return f"{ts} " + format_input_event(rec)
        if ev == "http_request":
            query = f"?{rec['query']}" if rec.get("query") else ""
            sni = f" sni={rec.get('sni')}" if rec.get("sni") and rec.get("sni") != rec.get("host") else ""
            note = f" NOTE {rec['note']}" if rec.get("note") else ""
            return (
                f"{ts} http {rec.get('method')} {rec.get('host')}{rec.get('path')}{query} -> {rec.get('status')} "
                f"({rec.get('body_len', 0)}B in / {rec.get('response_len', 0)}B out) [{rec.get('handler')}]{sni}{note}"
            )
        skip = ("ts", "event", "controller")
        parts = [f"{k}={v}" for k, v in rec.items() if k not in skip and (self.verbose or k not in self.BULKY)]
        return f"{ts} {ev} " + " ".join(parts)[:400]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
