"""macOS virtual pads through OpenJoystickDriver's JSON-lines ``virtual feed`` CLI.

The signed OJD app owns the HID entitlement. Each pad owns a persistent CLI process;
pipe I/O stays on worker threads, away from the receiver's asyncio loop. Idle state
is repeated to keep feedback exchanges timely: OJD's own 500 ms idle heartbeat is
too slow for a browser's renewed 100 ms rumble effects. The protocol used here is
the MIT-licensed 0.6.0-alpha.1 release.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from stadiacast.errors import SetupError
from stadiacast.gamepad import GamepadState

PROFILES = ("sdl-compatible", "game-controller-compatible", "generic")
RELEASE_URL = "https://github.com/xsyetopz/OpenJoystickDriver/releases/tag/0.6.0-alpha.1"
MAX_PENDING = 32
# OJD plays each frame for at least 8 ms. Pace writes to avoid building a stale
# queue inside its CLI; our own queue coalesces stick motion, but retains edges.
FRAME_INTERVAL = 0.01
FEEDBACK_INTERVAL = 0.02
# Renewals cross the CLI's exchange timer and a worker thread. Allow two feedback
# intervals of jitter before expiring a bounded effect; explicit StopRumble is
# still immediate. Losing feedback still stops the motors after a bounded time.
RUMBLE_EXPIRY_GRACE = 2 * FEEDBACK_INTERVAL
BUTTONS = {
    4: "Start",
    5: "Back",
    6: "LeftStickClick",
    7: "RightStickClick",
    8: "LeftShoulder",
    9: "RightShoulder",
    10: "Guide",
    12: "South",
    13: "East",
    14: "West",
    15: "North",
}
DPAD = {0: "DpadUp", 1: "DpadDown", 2: "DpadLeft", 3: "DpadRight"}


def find_ojd(executable: str = "") -> str:
    """Find the CLI or the signed app; argv[0] must be ``ojd`` even for the app."""
    if executable:
        found = shutil.which(os.path.expanduser(executable))
        if found:
            return found
        raise SetupError(f"OJD executable not found: {executable}")
    found = shutil.which("ojd")
    if found:
        return found
    for directory in (Path("/Applications"), Path.home() / "Applications"):
        path = directory / "OpenJoystickDriver.app/Contents/MacOS/OpenJoystickDriver"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise SetupError(f"--gamepad ojd needs OpenJoystickDriver; install its signed app from {RELEASE_URL}")


def to_ojd_frame(xinput: Mapping[str, int]) -> dict[str, Any]:
    """XInput-layout state to OJD: positional buttons, Y down, normalized axes."""
    mask = xinput["wButtons"]

    def stick(name: str) -> float:
        value = xinput[name]
        return max(-1.0, min(1.0, value / (32768 if value < 0 else 32767)))

    return {
        "buttons": [name for bit, name in BUTTONS.items() if mask & (1 << bit)],
        "dpad": [name for bit, name in DPAD.items() if mask & (1 << bit)],
        "axes": {
            "LeftStickX": stick("sThumbLX"),
            "LeftStickY": -stick("sThumbLY"),
            "RightStickX": stick("sThumbRX"),
            "RightStickY": -stick("sThumbRY"),
            "LeftTrigger": max(0, min(255, xinput["bLeftTrigger"])) / 255,
            "RightTrigger": max(0, min(255, xinput["bRightTrigger"])) / 255,
        },
    }


def edge_key(state: Mapping[str, int]) -> tuple[int, int, int]:
    """Only stick-only updates may replace a queued report; retain trigger taps too."""
    return state["wButtons"], state["bLeftTrigger"], state["bRightTrigger"]


class OJDPad:
    """One virtual feed; ``apply`` is nonblocking, ``close`` reaps its subprocess."""

    slot: int | None = None  # macOS has no XInput player slot

    def __init__(self, label: str, executable: str, profile: str) -> None:
        if profile not in PROFILES:
            raise SetupError(f"unknown OJD profile: {profile}")
        self.label = label
        self._condition = threading.Condition()
        self._pending: deque[dict[str, int]] = deque([GamepadState().to_xinput()])
        self._closed = False
        self._error: str | None = None
        self._error_callback: Callable[[str], None] | None = None
        self._rumble_callback: Callable[[int, int], None] | None = None
        self._rumble_timer: threading.Timer | None = None
        self._rumble_generation = 0
        self._diagnostics: deque[str] = deque(maxlen=4)
        try:
            # OJD dispatches to its CLI by argv[0], not by a flag. Using executable
            # also supports an app installation without a /usr/local/bin symlink.
            self._process = subprocess.Popen(
                ["ojd", "--json", "--no-color", "--no-input", "virtual", "feed", "--as", profile],
                executable=executable,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as e:
            raise SetupError(f"cannot start OpenJoystickDriver: {e}") from e
        workers = (("stderr", self._read_errors), ("feedback", self._read_feedback), ("input", self._write))
        self._threads = [
            threading.Thread(target=target, name=f"ojd-{label}-{name}", daemon=True) for name, target in workers
        ]
        for thread in self._threads:
            thread.start()

    def on_error(self, callback: Callable[[str], None]) -> None:
        """Register asynchronous failure reporting, including an early process exit."""
        with self._condition:
            self._error_callback = callback
            error = self._error
        if error is not None:
            callback(error)

    def on_rumble(self, callback: Callable[[int, int], None]) -> None:
        with self._condition:
            self._rumble_callback = callback

    def apply(self, xinput: dict[str, int]) -> None:
        with self._condition:
            if self._error:
                raise SetupError(self._error)
            if self._closed:
                raise SetupError("OJD feed is closed")
            state = dict(xinput)
            if self._pending and edge_key(self._pending[-1]) == edge_key(state):
                self._pending[-1] = state
            elif len(self._pending) < MAX_PENDING:
                self._pending.append(state)
            else:
                # Disconnect rather than silently lose a release and leave a key held.
                raise SetupError("OJD input queue is full; the virtual controller cannot keep up")
            self._condition.notify_all()

    def reset(self) -> None:
        with self._condition:
            self._pending.clear()
        self._rumble(0, 0)
        self.apply(GamepadState().to_xinput())

    def _write(self) -> None:
        stream = self._process.stdin
        assert stream is not None
        next_frame = 0.0
        last_state = GamepadState().to_xinput()
        next_feedback = 0.0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._pending or self._closed or self._error,
                        timeout=max(0, next_feedback - time.monotonic()),
                    )
                    if self._error or (self._closed and not self._pending):
                        break
                    delay = next_frame - time.monotonic()
                    if delay > 0 and not self._closed:
                        self._condition.wait(delay)
                        continue
                    # virtual feed exchanges feedback on input, otherwise only
                    # every 500 ms. Re-submit the held state, never a neutral
                    # heartbeat that would release a user's held buttons.
                    state = self._pending.popleft() if self._pending else last_state
                stream.write(json.dumps(to_ojd_frame(state), separators=(",", ":")) + "\n")
                stream.flush()
                next_frame = time.monotonic() + FRAME_INTERVAL
                next_feedback = time.monotonic() + FEEDBACK_INTERVAL
                last_state = state
        except OSError:
            # The feedback reader waits for exit and includes the CLI's actual error.
            self._terminate()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _read_errors(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        while line := stream.readline(4096):
            with self._condition:
                self._diagnostics.append(line.strip())

    def _read_feedback(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        while line := stream.readline(16384):
            try:
                self._feedback(json.loads(line))
            except (ValueError, TypeError, KeyError) as e:
                self._fail(f"invalid OJD rumble feedback: {e}")
                return
        code = self._process.wait()
        self._threads[0].join(timeout=0.2)  # capture stderr before reporting an exit
        with self._condition:
            detail = " ".join(self._diagnostics)
        self._fail(f"OJD feed exited ({code}): {detail or 'check OpenJoystickDriver is running and its permissions'}")

    def _feedback(self, message: Any) -> None:
        if not isinstance(message, dict):
            raise ValueError("expected a JSON object")
        kind = message.get("type")
        if kind == "StopRumble":
            self._rumble(0, 0)
        elif kind == "SetRumble":
            levels = message["intensities"]
            if not isinstance(levels, dict):
                raise ValueError("expected rumble intensities")

            # OJD uses 16-bit levels; PadBackend callbacks use ViGEm's byte range.
            def motor(name: str) -> int:
                value = levels.get(name, 0)
                if type(value) is not int or not 0 <= value <= 65535:
                    raise ValueError(f"invalid {name} level")
                return round(value / 257)

            duration = message.get("durationMilliseconds")
            if duration is not None and (type(duration) is not int or not 0 <= duration <= 60000):
                raise ValueError("invalid rumble duration")
            self._rumble(motor("lowFrequency"), motor("highFrequency"), duration)

    def _rumble(self, left: int, right: int, duration: int | None = None) -> None:
        with self._condition:
            self._rumble_generation += 1
            generation = self._rumble_generation
            if self._rumble_timer is not None:
                self._rumble_timer.cancel()
                self._rumble_timer = None
            if self._closed or self._error:
                return
            if self._rumble_callback is not None:
                self._rumble_callback(left, right)
            if duration is not None:
                delay = duration / 1000 + (RUMBLE_EXPIRY_GRACE if duration else 0)
                self._rumble_timer = threading.Timer(delay, self._stop_rumble, args=(generation,))
                self._rumble_timer.daemon = True
                self._rumble_timer.start()

    def _stop_rumble(self, generation: int) -> None:
        with self._condition:
            if generation == self._rumble_generation:
                self._rumble(0, 0)

    def _fail(self, error: str) -> None:
        with self._condition:
            if self._closed or self._error:
                return
            self._error = error
            self._pending.clear()
            self._condition.notify_all()
            callback = self._error_callback
        self._terminate()
        if callback is not None:
            callback(error)

    def _terminate(self) -> None:
        try:
            self._process.terminate()
        except ProcessLookupError:
            pass

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._error_callback = self._rumble_callback = None
            if self._rumble_timer is not None:
                self._rumble_timer.cancel()
            self._pending.clear()
            self._pending.append(GamepadState().to_xinput())
            self._condition.notify_all()
        # Give the writer a chance to neutralize and close stdin (EOF removes the
        # device). A stuck CLI must not stall receiver shutdown indefinitely.
        try:
            self._process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            self._terminate()
            try:
                self._process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        for thread in self._threads:
            thread.join(timeout=0.5)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()
