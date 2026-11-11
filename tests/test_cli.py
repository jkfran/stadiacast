"""stadiacast.cli: presets, flag validation and the module entry point."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from stadiacast import __version__, cli
from stadiacast.cli import DEFAULT_CERT, DEFAULT_KEY, main, parse_args
from stadiacast.config import PRESETS, Config
from stadiacast.errors import SetupError
from stadiacast.helpers import DNS_SINK, HELPERS, SNTP_SERVER, helper_command

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
    assert (cfg.cert, cfg.key, cfg.cert_given) == (DEFAULT_CERT, DEFAULT_KEY, False)
    assert parse_args(["--cert", "x.pem"]).cert_given is True
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


def test_audio_flags():
    cfg = parse_args(["--audio-source", "system", "--audio-volume", "0.5"])
    assert (cfg.audio_source, cfg.audio_volume) == ("system", 0.5)
    assert (cfg.audio_application, cfg.audio_follow_volume) == ("lowdelay", True)  # games, the slider followed
    cfg = parse_args(["--audio-application", "audio", "--audio-follow-volume", "off"])
    assert (cfg.audio_application, cfg.audio_follow_volume) == ("audio", False)
    for bad in (["--audio-application", "voip"], ["--audio-follow-volume", "yes"]):
        with pytest.raises(SystemExit):
            parse_args(bad)


def test_every_helper_command_line_runs_its_helper():
    """The names the receiver spawns are real modules: each command line starts its helper (``--help``)."""
    env = {**os.environ, "PYTHONPATH": SRC}
    expected = [sys.executable, "-m", "stadiacast.helpers.dns_sink", "--port", "5353"]
    assert helper_command(DNS_SINK, ["--port", "5353"]) == expected
    for name in HELPERS:
        out = subprocess.run(
            helper_command(name, ["--help"]), capture_output=True, text=True, env=env, check=True, timeout=60
        )
        assert "--log" in out.stdout


def test_main_prints_what_the_user_can_fix_and_a_bug_keeps_its_traceback(tmp_path, monkeypatch, capsys):
    assert main(["--rpc-response", f"Nope={tmp_path / 'missing.bin'}"]) == 2  # read by the command line itself
    err = capsys.readouterr().err
    assert err.startswith("stadiacast: ") and "missing.bin" in err

    def amain_raising(error: Exception):
        async def amain(_cfg: object) -> int:
            raise error

        return amain

    monkeypatch.setattr(cli, "amain", amain_raising(SetupError("--gamepad vigem needs the gamepad extra")))
    assert main([]) == 2
    assert capsys.readouterr().err == "stadiacast: --gamepad vigem needs the gamepad extra\n"
    monkeypatch.setattr(cli, "amain", amain_raising(RuntimeError("a bug")))
    with pytest.raises(RuntimeError, match="a bug"):
        main([])


def test_audio_devices_lists_the_capture_devices_or_says_what_to_install(monkeypatch, capsys):
    from stadiacast import capture

    monkeypatch.setattr(capture, "list_devices", lambda: ["Loopback Output", "Built-in Microphone"])
    with pytest.raises(SystemExit) as exc:
        parse_args(["--audio-devices"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.splitlines() == ["Loopback Output", "Built-in Microphone"]

    def no_extra() -> list[str]:
        raise SetupError("system audio capture needs the audio extra")

    monkeypatch.setattr(capture, "list_devices", no_extra)
    with pytest.raises(SystemExit) as exc:
        parse_args(["--audio-devices"])
    assert exc.value.code == 2
    assert capsys.readouterr().err == "stadiacast: system audio capture needs the audio extra\n"


def test_helpers_run_through_the_executable_when_frozen(monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert helper_command(SNTP_SERVER, []) == [sys.executable, "--helper", "sntp_server"]
    assert main(["--helper", "nothing"]) == 2
    assert "dns_sink, sntp_server" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["--helper", "sntp_server", "--help"])  # dispatched: the helper's own argparse answers
    assert exc.value.code == 0
    assert "--port" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["--http-ports", "80,abc"], "--http-ports: '80,abc' is not a comma separated list of port numbers"),
        (["--http-ports", "80,70000"], "--http-ports: '80,70000' has a port outside 0 to 65535"),
        (["--rpc-wire-id", "xyz"], "--rpc-wire-id: 'xyz' is not a number"),
        (["--rpc-fields", "RegisterController={bad"], "--rpc-fields RegisterController: the value is not JSON"),
    ],
)
def test_a_bad_value_is_reported_with_the_flag_it_was_given_to(capsys, argv, said):
    with pytest.raises(SystemExit) as stop:
        parse_args(argv)
    assert stop.value.code == 2 and said in capsys.readouterr().err


def test_a_bad_offer_file_is_reported_with_its_flag(tmp_path, capsys):
    (tmp_path / "offer.json").write_text("{bad")
    with pytest.raises(SystemExit):
        parse_args(["--offer-json", str(tmp_path / "offer.json")])
    assert "--offer-json" in capsys.readouterr().err


def lab_config(tmp_path: Path, **overrides: object) -> Config:
    settings: dict = {
        "cert": str(tmp_path / "lab.crt"),
        "key": str(tmp_path / "lab.key"),
        "cert_given": True,
        "self_signed": True,
        "bind": "127.0.0.1",
        "https_port": 0,
        "http_ports": (0,),
        "udp_port": 0,
        "game_udp_port": 0,
        "advertise_ip": "127.0.0.1",
        "log_dir": str(tmp_path / "run"),
        "echo": False,
    }
    return Config(**{**settings, **overrides})


def events(tmp_path: Path) -> list[str]:
    lines = (tmp_path / "run" / "server.jsonl").read_text().splitlines()
    return [json.loads(line)["event"] for line in lines]


def test_the_receiver_is_stopped_when_its_task_is_cancelled(tmp_path, capsys):
    """On Windows Ctrl-C cancels the task instead of setting the stop event: the receiver, which
    owns helper processes, streams and virtual pads, is stopped all the same."""

    async def run():
        task = asyncio.get_running_loop().create_task(cli.amain(lab_config(tmp_path)))
        for _ in range(200):
            if "ready: https" in capsys.readouterr().out:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert events(tmp_path)[-1] == "stopped" and "ready" in events(tmp_path)


def test_a_shutdown_request_stops_the_receiver_as_ctrl_c_does(tmp_path, capsys):
    async def run() -> int:
        task = asyncio.get_running_loop().create_task(cli.amain(lab_config(tmp_path)))
        port = None
        for _ in range(200):
            said = re.search(r"ready: https :\d+  http \[(\d+)\]", capsys.readouterr().out)
            if said:
                port = int(said.group(1))
                break
            await asyncio.sleep(0.01)
        assert port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /_shutdown HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 200 OK")
        writer.close()
        return await asyncio.wait_for(task, 10)

    assert asyncio.run(run()) == 0
    assert events(tmp_path)[-1] == "stopped" and "shutdown_requested" in events(tmp_path)


def test_a_start_that_fails_stops_what_it_started_and_reports_the_port(tmp_path, capsys):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        with pytest.raises(OSError, match=rf"cannot listen on 127\.0\.0\.1:{port} \(https\): TCP port {port} is taken"):
            asyncio.run(cli.amain(lab_config(tmp_path, https_port=port)))
    assert events(tmp_path)[-1] == "stopped" and "ready" not in events(tmp_path)
    assert "ready: https" not in capsys.readouterr().out
