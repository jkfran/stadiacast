"""Shared helpers for the end-to-end tests: a loopback receiver and fake controllers wired to it."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stadiacast.config import Config
from stadiacast.controller import ControllerSession
from stadiacast.log import EventLog
from stadiacast.receiver import Receiver
from tests.fake_controller import FakeController


def no_log(*_args: Any, **_kwargs: Any) -> None:
    """A log sink for fake controllers that drops everything."""


def make_receiver(tmp_path: Path, **overrides: Any) -> Receiver:
    """A receiver on loopback with ephemeral ports and a self-signed certificate."""
    cfg = Config(
        cert=str(tmp_path / "lab.crt"),
        key=str(tmp_path / "lab.key"),
        self_signed=True,
        bind="127.0.0.1",
        https_port=0,
        http_ports=(0,),
        udp_port=0,
        game_udp_port=0,
        advertise_ip="127.0.0.1",
        log_dir=str(tmp_path / "logs"),
        stadiaconfig='json:{"fake":true}',
        echo=False,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    os.makedirs(tmp_path / "logs", exist_ok=True)
    return Receiver(cfg, EventLog(str(tmp_path / "logs" / "server.jsonl"), echo=False))


def single(rx: Receiver) -> ControllerSession:
    """The one controller the single-controller tests talk to."""
    return rx.single()


def http_records(rx: Receiver, handler_prefix: str) -> list[dict[str, Any]]:
    """The ``http_request`` records whose handler name starts with ``handler_prefix``."""
    return [
        r
        for r in rx.log.records
        if r["event"] == "http_request" and str(r.get("handler", "")).startswith(handler_prefix)
    ]


def fake(rx: Receiver, device: str, **kw: Any) -> FakeController:
    """A firmware-accurate fake (offerer, DTLS client, raw_dtls) that identifies itself as ``device``."""
    return FakeController(
        "127.0.0.1",
        rx.http.https_port,
        rx.protos,
        log=no_log,
        dtls_role="client",
        controlling=True,
        raw_dtls=True,
        device_id=device,
        cloud_device_id=f"cloud-{device}",
        **kw,
    )


async def connect_firmware_flow(rx: Receiver, **fake_kw: Any) -> FakeController:
    """Boot, offer, GMsg answer + ack, ICE/DTLS; the fake then runs OnConnected
    (ResolveDiscovery) by itself, as the firmware does."""
    fc = FakeController(
        "127.0.0.1",
        rx.http.https_port,
        rx.protos,
        log=no_log,
        dtls_role="client",
        controlling=True,
        raw_dtls=True,
        **fake_kw,
    )
    await fc.boot()
    await fc.start_udp()
    await fc.discover()
    await fc.poll_gmsg()
    await fc.poll_gmsg()
    fc.connect()
    await fc.wait_connected(10)
    return fc


async def wait_for(cond: Callable[[], Any], timeout: float = 3.0) -> None:
    """Poll ``cond`` every 20 ms until it is truthy, or fail after ``timeout`` seconds."""
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")
