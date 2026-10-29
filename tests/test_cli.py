"""stadiacast.cli: presets, flag validation and the module entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stadiacast import __version__
from stadiacast.cli import DEFAULT_CERT, DEFAULT_KEY, parse_args
from stadiacast.config import PRESETS

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_preset_live_is_the_proven_configuration_and_flags_override_it():
    cfg = parse_args(["--preset", "live"])
    assert cfg.preset == "live" and cfg.clearcut_block == "tls" and cfg.rpc_wire_id == 0x75
    assert (cfg.gmsg_hold, cfg.dtls_role, cfg.sntp, cfg.boreal) == (20.0, "server", True, "off")
    assert (cfg.display_rpc_trigger, cfg.display_rpc_delay, cfg.ice_fast_checks) == ("dtls", 0.6, 1.0)
    for key, value in PRESETS["live"].items():
        assert getattr(cfg, key) == value
    cfg = parse_args(["--preset", "live", "--clearcut-block", "off", "--gmsg-hold", "5", "--no-sntp"])
    assert cfg.clearcut_block == "off" and cfg.gmsg_hold == 5.0 and cfg.sntp is False
    cfg = parse_args([])
    assert cfg.preset is None and cfg.clearcut_block == "off" and cfg.gmsg_hold == 0.0
    assert (cfg.cert, cfg.key) == (DEFAULT_CERT, DEFAULT_KEY)
    assert cfg.http_ports == (80, 8008) and cfg.dns_redirect == ("time.google.com",)


def test_list_flags_and_clock_pin_real():
    cfg = parse_args(["--http-ports", "", "--dns-redirect", "", "--clock-pin", "real"])
    assert cfg.http_ports == () and cfg.dns_redirect == () and cfg.clock_pin is None
    cfg = parse_args(["--http-ports", "80, 8080", "--clock-pin", "auto", "--rpc-wire-id", "0x74"])
    assert cfg.http_ports == (80, 8080) and cfg.clock_pin == "auto" and cfg.rpc_wire_id == 0x74


def test_rumble_refresh_must_beat_the_haptics_watchdog():
    """A refresh <= 0 would never yield (busy loop); >= 5 s lets the watchdog stop every sustained rumble."""
    assert parse_args(["--rumble-refresh", "4.5"]).rumble_refresh == 4.5
    for bad in ("0", "-1", "5", "nan"):
        with pytest.raises(SystemExit):
            parse_args(["--rumble-refresh", bad])


def test_protocol_limits_are_enforced_on_the_command_line():
    with pytest.raises(SystemExit):
        parse_args(["--rpc-wire-id", "0xfffa"])  # dropped by the controller
    with pytest.raises(SystemExit):
        parse_args(["--start-game", "k" * 24])
    assert parse_args(["--start-game"]).start_game == "auto"
    assert parse_args(["--start-game", "k" * 23]).start_game == "k" * 23


def test_module_entry_point_prints_help():
    env = {**os.environ, "PYTHONPATH": SRC}
    out = subprocess.run(
        [sys.executable, "-m", "stadiacast", "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    assert "--preset {live}" in out.stdout and "game session" in out.stdout


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"stadiacast {__version__}"
