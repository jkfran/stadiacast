"""Test configuration.

The suite talks to itself over loopback (HTTPS, UDP) and drives asyncio with ``asyncio.run()``.
Two pytest plugins that some development machines have installed globally (``pytest-socket``,
pulled in by ``pytest-homeassistant-custom-component``) break that: sockets are disabled for
every test by default, and the plugin's ``enable_event_loop_debug`` fixture expects a current
event loop at setup, which the previous test's ``asyncio.run()`` has torn down.  The fixture
below undoes both; it is a no-op when the plugins are absent.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

try:
    import pytest_socket
except ImportError:  # pragma: no cover - plain environments
    pytest_socket = None

HA_PLUGIN = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None


@pytest.fixture(autouse=True)
def _loopback_friendly_environment():
    if pytest_socket is not None:
        pytest_socket.enable_socket()
    yield
    if HA_PLUGIN:
        try:
            asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
