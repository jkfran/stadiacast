"""stadiacast.ble_provisioner without a radio: the pure parts, and the whole provisioning flow against a
fake BLE stack (device discovery, the optional Wi-Fi scan, the RSA-encrypted credential write and
every way the controller can answer it)."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import time
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from stadiacast import ble_provisioner as ble

pytest.importorskip("bleak")  # the ble extra: the flow below raises and catches its error type


def test_connect_payload_and_redaction():
    payload = ble.build_connect_payload("home", "c2VjcmV0", 7, 4, bssid="aa:bb", frequency=2412)
    assert payload == {
        "ssid": "home", "wpa_auth": 7, "wpa_cipher": 4, "bssid": "aa:bb", "frequency": 2412, "enc_passwd": "c2VjcmV0",
    }  # fmt: skip
    assert "enc_passwd" not in ble.build_connect_payload("open", None, 1, 1)
    assert ble.redact('{"ssid":"home","enc_passwd":"c2VjcmV0"}') == '{"ssid":"home","enc_passwd": "<redacted>"}'


def test_window_payload_decompresses_gzip_only():
    assert ble.window_payload(gzip.compress(b'{"a":1}')) == b'{"a":1}'
    assert ble.window_payload(b"plain") == b"plain"
    assert ble.window_payload(b"\x1f\x8btruncated") == b"\x1f\x8btruncated"


def test_window_payload_survives_a_torn_gzip_stream():
    # gzip raises EOFError for a stream cut short and zlib.error for corrupt data, neither an OSError
    torn = gzip.compress(b'{"ssid": "net"}')[:-4]
    assert ble.window_payload(torn) == torn
    corrupt = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 8  # a gzip header, then an invalid block
    assert ble.window_payload(corrupt) == corrupt


class Paging:
    """A windowed-read pair that serves the given pages, then empty ones."""

    def __init__(self, *pages: bytes) -> None:
        self.pages = pages
        self.requested: list[int] = []

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:  # noqa: ARG002
        self.requested.append(int.from_bytes(data, "little"))

    async def read_gatt_char(self, char: str) -> bytes:  # noqa: ARG002
        page = self.requested[-1]
        return self.pages[page] if page < len(self.pages) else b""


@pytest.mark.usefixtures("fake_ble")  # the fixture also fast-forwards asyncio.sleep
@pytest.mark.parametrize(
    ("pages", "requested"),
    [
        pytest.param((b"a" * 512, b"b" * 300), [0, 1], id="a short page ends the window"),
        pytest.param((b"a" * 512, b"b" * 512), [0, 1, 2], id="an empty page ends a window of whole pages"),
        pytest.param((), [0], id="an empty window"),
    ],
)
def test_window_read_walks_pages_until_the_window_ends(pages, requested):
    client = Paging(*pages)
    assert asyncio.run(ble.window_read(client, ble.WINDOW_CRYPTO)) == b"".join(pages)
    assert client.requested == requested  # the page budget is not spent on a short window


@pytest.mark.usefixtures("fake_ble")
def test_window_read_reports_a_window_longer_than_its_page_budget():
    client = Paging(*[b"a" * 512] * 4)
    with pytest.raises(ble.ProvisioningError, match="longer than 3 pages"):
        asyncio.run(ble.window_read(client, ble.WINDOW_CRYPTO, pages=3))


def test_cli_requires_a_subcommand_and_knows_the_match_option(capsys):
    parser = ble.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["provision", "-a", "ADDR", "-s", "net", "--service-data-match", "7AC0"])
    assert (args.cmd, args.address, args.ssid, args.service_data_match) == ("provision", "ADDR", "net", "7AC0")
    assert not args.wifi_scan and (args.wpa_auth, args.wpa_cipher) == (None, None)  # the password decides
    args = parser.parse_args(["provision", "-s", "net", "--service-data-match", "7ac0", "--wifi-scan"])
    assert args.address is None and args.wifi_scan  # the match stands in for the address
    args = parser.parse_args(["provision", "-a", "ADDR", "-s", "net", "--wpa-auth", "5", "--wpa-cipher", "3"])
    assert (args.wpa_auth, args.wpa_cipher) == (5, 3)
    with pytest.raises(SystemExit):
        parser.parse_args(["provision", "-s", "net", "--service-data-match", "not hex"])
    assert "is not hexadecimal" in capsys.readouterr().err
    assert ble.STATE_NAMES[0x3C] == "connected"


def test_help_is_plain_text(capsys):
    with pytest.raises(SystemExit):
        ble.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "stadiacast-provision provision --address ADDR --ssid SSID" in out and "``" not in out


def test_advertiser_detection():
    fast_pair = SimpleNamespace(local_name=None, service_data={ble.FAST_PAIR_SERVICE_DATA_UUID: b"\x7a\xc0"})
    assert ble.is_stadia_advertiser(SimpleNamespace(name=None), fast_pair)
    assert ble.is_stadia_advertiser(
        SimpleNamespace(name="Stadia Setup"), SimpleNamespace(local_name=None, service_data={})
    )
    assert not ble.is_stadia_advertiser(SimpleNamespace(name="Kettle"), SimpleNamespace(local_name="", service_data={}))


# ---- the controller's key and the credentials ----------------------------------------------

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SCAN_RESULTS = [
    {"ssid": "neighbour", "bssid": "11:22:33:44:55:66", "signal_level": -80, "wpa_auth": 7, "wpa_cipher": 4},
    {
        "ssid": "hotspot",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "signal_level": -40,
        "wpa_auth": 5,
        "wpa_cipher": 3,
        "frequency": 2437,
    },
]


def spki(key: Any) -> str:
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def crypto_bundle(public_key: str | None = None) -> bytes:
    return json.dumps({"device_info": {"public_key": public_key or spki(PRIVATE_KEY)}}).encode()


def decrypted(payload: dict[str, Any]) -> bytes:
    return PRIVATE_KEY.decrypt(base64.b64decode(payload["enc_passwd"]), padding.PKCS1v15())


def test_public_key_and_password_encryption_round_trip():
    key = ble.load_public_key(crypto_bundle())
    assert key.key_size == 2048
    enc = ble.encrypt_password("hunter22", key)
    assert PRIVATE_KEY.decrypt(base64.b64decode(enc), padding.PKCS1v15()) == b"hunter22"


@pytest.mark.parametrize(
    "bundle",
    [
        pytest.param(b"not json", id="not JSON"),
        pytest.param(b"[1, 2]", id="a list"),
        pytest.param(b'{"device_info": "text"}', id="device_info is not an object"),
        pytest.param(b'{"device_info": {"public_key": 7}}', id="the key is a number"),
        pytest.param(crypto_bundle("!!!!AAAA"), id="not base64"),
        pytest.param(crypto_bundle(base64.b64encode(b"not a key").decode()), id="not DER"),
        pytest.param(crypto_bundle(spki(ec.generate_private_key(ec.SECP256R1()))), id="not an RSA key"),
        pytest.param(b"[" * 100_000, id="nested beyond the parser's depth"),
    ],
)
def test_a_key_window_that_is_not_an_rsa_key_is_reported_in_one_line(bundle):
    with pytest.raises(ble.ProvisioningError, match="public key"):
        ble.load_public_key(bundle)


def test_a_password_too_long_for_the_key_is_reported():
    small_key = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
    with pytest.raises(ble.ProvisioningError, match="too long for the controller's key"):
        ble.encrypt_password("x" * 200, small_key)


# ---- the provisioning flow against a fake controller --------------------------------------


class FakeClient:
    """The GATT surface the provisioner touches, with the controller's windowed reads.

    The class attributes say how this controller behaves; the ``fake_ble`` fixture resets them."""

    DEFAULTS: ClassVar[dict[str, Any]] = {
        "torn": frozenset(),  # windows that arrive cut short
        "answer": (0x1E, 0x3C),  # the states notified after the credential write, one at a time
        "answer_delay": 0.0,  # seconds the controller takes before that answer
        "leaves": None,  # where the controller drops the link: a characteristic, or "joined" (after its answer)
        "stale_state": None,  # a state notified as soon as the tool subscribes
        "scan_results": SCAN_RESULTS,
        "scan_state": ble.STATE_SCAN_DONE,  # what the state reads after the Wi-Fi scan was asked for
        "ip": bytes([192, 168, 2, 3]),
        "connect_error": None,
        "disconnect_error": None,
    }
    torn: ClassVar[frozenset[tuple[str, str]]]
    answer: ClassVar[tuple[int, ...]]
    answer_delay: ClassVar[float]
    leaves: ClassVar[str | None]
    stale_state: ClassVar[int | None]
    scan_results: ClassVar[Any]
    scan_state: ClassVar[int]
    ip: ClassVar[bytes]
    connect_error: ClassVar[BaseException | None]
    disconnect_error: ClassVar[BaseException | None]
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, device: Any, timeout: float | None = None, disconnected_callback: Any = None) -> None:  # noqa: ARG002
        self.device = device
        self.on_disconnect = disconnected_callback
        self.connected = False
        self.writes: list[tuple[str, bytes]] = []
        self.reads: list[str] = []
        self.notify: dict[str, Any] = {}
        self.state = 0x14
        self.pages: dict[str, int] = {}
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        if self.disconnect_error is not None:
            raise self.disconnect_error
        if self.connected:
            self._link_gone()  # bleak reports a disconnect it made itself in the same way

    def _link_gone(self) -> None:
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self)

    def _use(self, char: str) -> None:
        if self.leaves == char:
            self._link_gone()
        if not self.connected:
            raise ble.BleakError("Not connected")

    async def start_notify(self, char: str, callback: Any) -> None:
        self._use(char)
        self.notify[char] = callback
        if char == ble.CHAR_STATE and self.stale_state is not None:
            callback(None, bytearray([self.stale_state]))

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:  # noqa: ARG002
        self._use(char)
        self.writes.append((char, bytes(data)))
        if char == ble.CHAR_SETTINGS:
            self.state = self.scan_state
        elif char == ble.CHAR_CONNECT:
            asyncio.get_running_loop().call_later(self.answer_delay, self._answer_credentials)
        else:
            self.pages[char] = int.from_bytes(data, "little")

    def _answer_credentials(self) -> None:
        for state in self.answer:
            self.state = state
            self.notify[ble.CHAR_STATE](None, bytearray([state]))
        if self.leaves == "joined":
            self._link_gone()

    async def read_gatt_char(self, char: str) -> bytearray:
        self.reads.append(char)
        self._use(char)
        if char == ble.CHAR_STATE:
            return bytearray([self.state])
        if char == ble.CHAR_IP:
            return bytearray(self.ip)
        windows = {ble.WINDOW_SCAN: json.dumps(self.scan_results).encode(), ble.WINDOW_CRYPTO: crypto_bundle()}
        for pair, payload in windows.items():
            data_char, control_char = pair
            if char == data_char:
                blob = gzip.compress(payload)
                if pair in self.torn:
                    blob = blob[: len(blob) // 2]
                return bytearray(blob if self.pages.get(control_char, 0) == 0 else b"")
        raise AssertionError(f"unexpected read of {char}")

    @property
    def credentials(self) -> dict[str, Any]:
        """The connect_to_network payload the tool wrote."""
        (payload,) = [data for char, data in self.writes if char == ble.CHAR_CONNECT]
        return dict(json.loads(payload))


class FakeScanner:
    advertisement = SimpleNamespace(
        local_name="Stadia Setup", service_data={ble.FAST_PAIR_SERVICE_DATA_UUID: b"\x7a\xc0\x01"}
    )
    device = SimpleNamespace(address="11111111-2222-3333-4444-555555555555", name="Stadia Setup")
    in_range: ClassVar[list[tuple[Any, Any]]] = []

    @classmethod
    async def find_device_by_filter(cls, match: Any, timeout: float = 0) -> Any:  # noqa: ARG003
        return cls.device if match(cls.device, cls.advertisement) else None

    @classmethod
    async def discover(cls, timeout: float = 0, return_adv: bool = False) -> Any:  # noqa: ARG003
        return {device.address: (device, adv) for device, adv in cls.in_range}


ADDRESS = FakeScanner.device.address


@pytest.fixture
def fake_ble(monkeypatch):
    async def instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(ble.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(ble, "BleakClient", FakeClient)
    monkeypatch.setattr(ble, "BleakScanner", FakeScanner)
    for name, value in FakeClient.DEFAULTS.items():
        monkeypatch.setattr(FakeClient, name, value, raising=False)
    monkeypatch.setattr(FakeScanner, "in_range", [(FakeScanner.device, FakeScanner.advertisement)])
    FakeClient.instances.clear()
    return FakeClient


def provision(*args: Any, **kwargs: Any) -> FakeClient:
    """Run a provisioning with a wait far longer than a test may take, and return the fake it talked to."""
    started = time.monotonic()
    try:
        asyncio.run(ble.provision(*args, **{"wait": 60.0, **kwargs}))
    finally:
        assert time.monotonic() - started < 10, "the tool sat out its wait although the controller had answered"
    (client,) = FakeClient.instances
    return client


def test_provision_by_default_leaves_out_the_wifi_scan_and_assumes_wpa2(fake_ble, capsys):
    fake_ble.leaves = "joined"
    client = provision(ADDRESS, "hotspot", "hunter22")
    chars = [c for c, _ in client.writes]
    assert chars[0] == ble.WINDOW_CRYPTO[1]  # the key read
    assert ble.CHAR_SETTINGS not in chars and ble.WINDOW_SCAN[1] not in chars  # no Wi-Fi scan
    assert chars[-1] == ble.CHAR_CONNECT
    payload = client.credentials
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == ble.WPA2_PERSONAL == (7, 4)
    assert "bssid" not in payload and "frequency" not in payload  # the controller joins without them
    assert decrypted(payload) == b"hunter22"
    out = capsys.readouterr().out
    assert "Scanning" not in out and "STATE -> 0x1e (connecting)" in out and "STATE -> 0x3c (connected)" in out
    assert "<redacted>" in out and "hunter22" not in out
    assert out.rstrip().endswith("Done: the controller joined 'hotspot'.")
    assert ble.CHAR_IP not in client.reads  # the link was gone: nothing more to read


def test_provision_with_wifi_scan_copies_the_security_enums_and_writes_encrypted_credentials(fake_ble, capsys):
    fake_ble.leaves = "joined"
    client = provision(ADDRESS, "hotspot", "hunter22", wifi_scan=True)
    chars = [c for c, _ in client.writes]
    assert chars[0] == ble.WINDOW_CRYPTO[1]  # the key read comes before the Wi-Fi rescan
    assert ble.CHAR_SETTINGS in chars  # the scan is still triggered, after the key
    assert chars[-1] == ble.CHAR_CONNECT
    payload = client.credentials
    assert payload["ssid"] == "hotspot"
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (5, 3)  # the device's own scan entry, not a guess
    assert (payload["bssid"], payload["frequency"]) == ("aa:bb:cc:dd:ee:ff", 2437)
    assert decrypted(payload) == b"hunter22"
    out = capsys.readouterr().out
    assert "wpa_auth=5, wpa_cipher=3" in out
    assert "<redacted>" in out and "hunter22" not in out


def test_provision_matches_by_service_data_and_takes_an_open_network_without_a_password(fake_ble):
    fake_ble.leaves = "joined"
    client = provision(None, "hotspot", None, service_data_match="7AC0")
    payload = client.credentials
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == ble.OPEN_NETWORK == (1, 1)
    assert "enc_passwd" not in payload  # an open network: nothing to encrypt, no key read
    assert not any(c == ble.WINDOW_CRYPTO[1] for c, _ in client.writes)


@pytest.mark.parametrize("wifi_scan", [False, True])
def test_provision_takes_given_security_enums_over_the_default_and_the_scan(fake_ble, wifi_scan):
    fake_ble.leaves = "joined"
    client = provision(ADDRESS, "hotspot", "hunter22", wpa_auth=2, wpa_cipher=2, wifi_scan=wifi_scan)
    payload = client.credentials
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (2, 2)
    assert ("bssid" in payload) == wifi_scan  # the scan still gives the bssid and the frequency


# ---- how a provisioning ends -----------------------------------------------------------------


def test_the_link_dropping_after_the_write_is_the_controller_leaving_setup_mode(fake_ble, capsys):
    """What a controller usually does: "connecting", then the link goes, with no "connected" before."""
    fake_ble.answer = (0x1E,)
    fake_ble.leaves = "joined"
    provision(ADDRESS, "hotspot", "hunter22")
    assert capsys.readouterr().out.rstrip().endswith("Done: the controller has the credentials and left setup mode.")


def test_a_controller_that_reports_connected_and_keeps_the_link_is_watched_to_the_end(fake_ble, capsys):
    asyncio.run(ble.provision(ADDRESS, "hotspot", "hunter22", wait=0.05))
    (client,) = fake_ble.instances
    out = capsys.readouterr().out
    assert "Controller IP: 192.168.2.3" in out and out.rstrip().endswith("Done: the controller joined 'hotspot'.")
    assert not client.connected  # and the tool hung up


def test_a_controller_with_an_address_and_no_report_has_joined(fake_ble, capsys):
    fake_ble.answer = ()
    asyncio.run(ble.provision(ADDRESS, "hotspot", "hunter22", wait=0.05))
    assert capsys.readouterr().out.rstrip().endswith("Done: the controller is on the network.")


@pytest.mark.parametrize("ip", [ble.NO_ADDRESS, bytes(4), b""], ids=["loopback", "zero", "empty"])
def test_a_controller_that_reports_nothing_and_has_no_address_has_not_joined(fake_ble, ip):
    """The tool's own disconnect at the end is not the controller leaving setup mode."""
    fake_ble.answer = (0x1E,)
    fake_ble.ip = ip
    with pytest.raises(ble.ProvisioningError, match="gave no sign of joining 'hotspot' in 0 s"):
        asyncio.run(ble.provision(ADDRESS, "hotspot", "hunter22", wait=0.05))


@pytest.mark.parametrize(
    ("how", "hint"),
    [
        pytest.param({}, "check the network name and the password; for a network that is not", id="default"),
        pytest.param({"wifi_scan": True}, "check the password", id="security from the scan"),
        pytest.param({"wpa_auth": 5}, "the values given to --wpa-auth and --wpa-cipher", id="security by hand"),
        pytest.param({"password": None}, "if the network has a password, give it", id="open"),
    ],
)
def test_a_failed_join_ends_the_watch_at_once_with_what_to_check(fake_ble, how, hint):
    fake_ble.answer = (0x1E, ble.STATE_JOIN_FAILED)
    fake_ble.scan_results = [{"ssid": "hotspot", "wpa_auth": 1 if "password" in how else 7, "wpa_cipher": 4}]
    with pytest.raises(ble.ProvisioningError, match=r"did not join 'hotspot' \(STATE 0x4f\)") as failure:
        provision(ADDRESS, "hotspot", **{"password": "hunter22", **how})
    assert hint in str(failure.value)
    assert ("--wifi-scan" in str(failure.value)) == (how == {})  # suggested only where it can help
    assert ble.CHAR_IP not in fake_ble.instances[0].reads


@pytest.mark.parametrize("state", [0x34, 0x4E])
def test_credentials_the_controller_could_not_parse_are_a_failure(fake_ble, state):
    fake_ble.answer = (state,)
    with pytest.raises(ble.ProvisioningError, match=f"could not read the credentials .STATE 0x{state:02x}"):
        provision(ADDRESS, "hotspot", "hunter22")


def test_the_first_state_that_settles_it_counts(fake_ble):
    fake_ble.answer = (0x1E, 0x3C, ble.STATE_JOIN_FAILED)
    provision(ADDRESS, "hotspot", "hunter22")  # joined: the later state does not undo it
    fake_ble.instances.clear()
    fake_ble.answer = (0x1E, ble.STATE_JOIN_FAILED, 0x3C)
    with pytest.raises(ble.ProvisioningError, match="did not join"):
        provision(ADDRESS, "hotspot", "hunter22")


def test_a_failure_reported_before_the_credentials_went_out_is_not_about_them(fake_ble, capsys):
    """A controller left in its failed state by an earlier attempt may say so again on subscription:
    the watch still waits for what it answers to these credentials."""
    fake_ble.stale_state = ble.STATE_JOIN_FAILED
    fake_ble.answer_delay = 0.1
    fake_ble.leaves = "joined"
    provision(ADDRESS, "hotspot", "hunter22")
    assert capsys.readouterr().out.rstrip().endswith("Done: the controller joined 'hotspot'.")


def test_a_failing_disconnect_does_not_undo_a_provisioning(fake_ble):
    fake_ble.leaves = "joined"
    fake_ble.disconnect_error = OSError("The object has been closed.")
    provision(ADDRESS, "hotspot", "hunter22")


# ---- what can go wrong before the credentials are written -----------------------------------


@pytest.mark.parametrize(
    ("ssid", "password", "how", "message"),
    [
        ("", "hunter22", {}, "a network name has 1 to 32 bytes"),
        ("x" * 33, "hunter22", {}, "a network name has 1 to 32 bytes"),
        ("hotspot", "short", {}, "8 to 63 characters"),
        ("hotspot", "x" * 64, {}, "8 to 63 characters"),
        ("hotspot", "hunter22", {"wpa_auth": 1}, "an open network, which has no password"),
        ("hotspot", None, {"wpa_auth": 7}, "needs a password"),
    ],
)
def test_credentials_no_network_can_have_are_refused_before_any_bluetooth(fake_ble, ssid, password, how, message):
    with pytest.raises(ble.ProvisioningError, match=message):
        asyncio.run(ble.provision(ADDRESS, ssid, password, **how))
    assert fake_ble.instances == []


def test_a_key_of_64_hex_digits_and_a_passphrase_with_spaces_are_passwords(fake_ble):
    fake_ble.leaves = "joined"
    assert decrypted(provision(ADDRESS, "hotspot", "ab" * 32).credentials) == b"ab" * 32
    fake_ble.instances.clear()
    assert decrypted(provision(ADDRESS, "hotspot", "  spaced out  ").credentials) == b"  spaced out  "


@pytest.mark.parametrize(
    ("scanned", "password", "message"),
    [
        ({"wpa_auth": 1, "wpa_cipher": 1}, "hunter22", "an open network, which has no password"),
        ({"wpa_auth": 7, "wpa_cipher": 4}, None, "needs a password"),
        ({"wpa_auth": 7}, "hunter22", "does not give the security of 'hotspot'"),
        ({"wpa_auth": "7", "wpa_cipher": 4}, "hunter22", "does not give the security of 'hotspot'"),
    ],
)
def test_what_the_scan_says_about_the_network_is_checked_against_the_password(fake_ble, scanned, password, message):
    fake_ble.scan_results = [{"ssid": "hotspot", **scanned}]
    with pytest.raises(ble.ProvisioningError, match=message):
        provision(ADDRESS, "hotspot", password, wifi_scan=True)
    assert ble.CHAR_CONNECT not in [c for c, _ in fake_ble.instances[0].writes]


@pytest.mark.parametrize(
    ("results", "message"),
    [
        pytest.param(SCAN_RESULTS, "does not show 'elsewhere'; it shows 'neighbour', 'hotspot'", id="not in range"),
        pytest.param([], "does not show 'elsewhere'; it shows no network", id="nothing in range"),
        pytest.param({"ssid": "elsewhere"}, "scan results could not be read", id="an object"),
        pytest.param(None, "scan results could not be read", id="null"),
        pytest.param(["elsewhere", 7], "does not show 'elsewhere'; it shows no network", id="not objects"),
    ],
)
def test_scan_results_without_the_network_are_reported_in_one_line(fake_ble, results, message):
    fake_ble.scan_results = results
    with pytest.raises(ble.ProvisioningError, match=message):
        provision(ADDRESS, "elsewhere", "hunter22", wifi_scan=True)


def test_unreadable_results_of_a_scan_that_never_ended_say_so(fake_ble):
    fake_ble.scan_state = 0x14
    fake_ble.torn = frozenset({ble.WINDOW_SCAN})
    with pytest.raises(ble.ProvisioningError, match="did not report the end of its scan in 20 s"):
        provision(ADDRESS, "hotspot", "hunter22", wifi_scan=True)


@pytest.mark.usefixtures("fake_ble")
def test_a_controller_that_is_not_advertising_is_reported():
    with pytest.raises(ble.ProvisioningError, match="was not found: it has to be in setup mode"):
        asyncio.run(ble.provision("unknown-address", "hotspot", "hunter22"))
    with pytest.raises(ble.ProvisioningError, match="was not found"):
        asyncio.run(ble.provision(None, "hotspot", "hunter22", service_data_match="ffff"))


@pytest.mark.parametrize(("window", "what"), [(ble.WINDOW_CRYPTO, "public key"), (ble.WINDOW_SCAN, "scan results")])
def test_provision_reports_a_window_cut_short_and_writes_nothing(fake_ble, window, what):
    fake_ble.torn = frozenset({window})
    with pytest.raises(ble.ProvisioningError, match=f"{what} could not be read"):
        provision(ADDRESS, "hotspot", "hunter22", wifi_scan=True)
    assert ble.CHAR_CONNECT not in [c for c, _ in fake_ble.instances[0].writes]


@pytest.mark.parametrize("leaves", [ble.WINDOW_CRYPTO[0], ble.CHAR_CONNECT], ids=["at the key read", "at the write"])
def test_a_link_dropped_before_the_credentials_were_taken_is_a_failure(fake_ble, leaves):
    fake_ble.leaves = leaves
    with pytest.raises(ble.ProvisioningError, match="dropped the Bluetooth link before it had the credentials") as e:
        provision(ADDRESS, "hotspot", "hunter22")
    assert "Not connected" in str(e.value) and "--wifi-scan" not in str(e.value)
    assert ble.CHAR_CONNECT not in [c for c, _ in fake_ble.instances[0].writes]


@pytest.mark.parametrize(
    ("leaves", "security"),
    [
        pytest.param(ble.WINDOW_SCAN[0], "--wpa-auth and --wpa-cipher (see --help)", id="before the results"),
        pytest.param(ble.CHAR_CONNECT, "--wpa-auth 5 --wpa-cipher 3", id="with the results read"),
    ],
)
def test_a_link_dropped_after_the_wifi_scan_says_to_leave_the_scan_out(fake_ble, leaves, security):
    fake_ble.leaves = leaves
    with pytest.raises(ble.ProvisioningError, match="after the controller's Wi-Fi scan was asked for") as failure:
        provision(ADDRESS, "hotspot", "hunter22", wifi_scan=True)
    assert f"Run the command again without --wifi-scan, giving {security}" in str(failure.value)


# ---- the command line ----------------------------------------------------------------------


def test_main_reports_a_missing_ble_extra(monkeypatch, capsys):
    monkeypatch.setattr(ble, "BLEAK_AVAILABLE", False)
    assert ble.main(["scan"]) == 2
    assert "stadiacast[ble]" in capsys.readouterr().err


def test_main_needs_an_address_or_a_match(capsys):
    with pytest.raises(SystemExit) as stop:
        ble.main(["provision", "-s", "hotspot", "-p", "hunter22"])
    assert stop.value.code == 2 and "needs --address or --service-data-match" in capsys.readouterr().err


def test_main_provisions_and_exits_zero(fake_ble, capsys):
    fake_ble.leaves = "joined"
    assert ble.main(["provision", "-a", ADDRESS, "-s", "hotspot", "-p", "hunter22"]) == 0
    captured = capsys.readouterr()
    assert "Done: the controller joined 'hotspot'." in captured.out and captured.err == ""


def test_main_reports_a_failed_provisioning_in_one_line(fake_ble, capsys):
    fake_ble.answer = (ble.STATE_JOIN_FAILED,)
    assert ble.main(["provision", "-a", ADDRESS, "-s", "hotspot", "-p", "hunter22"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("stadiacast-provision: the controller did not join 'hotspot'") and "Traceback" not in err
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (TimeoutError(), "timed out"),
        (OSError("The object has been closed."), "The object has been closed."),
        (OSError(5, "Input/output error"), "[Errno 5] Input/output error"),
        (ble.BleakError("Bluetooth device is turned off"), "Bluetooth device is turned off"),
        # the shape of bleak's "Bluetooth is not available" error: a message and a reason
        (ble.BleakError("No Bluetooth adapter found", 1), "No Bluetooth adapter found"),
    ],
)
def test_main_reports_a_connection_that_fails_in_one_line(fake_ble, capsys, error, said):
    fake_ble.connect_error = error
    assert ble.main(["provision", "-a", ADDRESS, "-s", "hotspot", "-p", "hunter22"]) == 1
    assert capsys.readouterr().err == f"stadiacast-provision: Bluetooth: {said}\n"


def test_main_exits_130_when_interrupted(monkeypatch, capsys):
    async def interrupted(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ble, "provision", interrupted)
    assert ble.main(["provision", "-a", ADDRESS, "-s", "hotspot", "-p", "hunter22"]) == 130
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("typed", "sent"),
    [("  spaced out  ", b"  spaced out  "), ("", None), ("   ", None)],
    ids=["a passphrase keeps its spaces", "blank is an open network", "only spaces is blank"],
)
def test_main_prompts_for_the_password_and_sends_it_as_typed(fake_ble, monkeypatch, typed, sent):
    fake_ble.leaves = "joined"
    monkeypatch.delenv("STADIA_WIFI_PW", raising=False)
    monkeypatch.setattr(ble.getpass, "getpass", lambda _prompt: typed)
    assert ble.main(["provision", "-a", ADDRESS, "-s", "hotspot"]) == 0
    payload = fake_ble.instances[0].credentials
    assert (decrypted(payload) if "enc_passwd" in payload else None) == sent
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (ble.WPA2_PERSONAL if sent else ble.OPEN_NETWORK)


def test_scan_lists_the_controllers_in_range(fake_ble, capsys):  # noqa: ARG001
    nameless = SimpleNamespace(address="AA-BB", name=None)
    kettle = SimpleNamespace(address="CC-DD", name="Kettle")
    FakeScanner.in_range = [
        (FakeScanner.device, FakeScanner.advertisement),
        (nameless, SimpleNamespace(local_name=None, service_data={ble.FAST_PAIR_SERVICE_DATA_UUID: b"\x00\xcf\xe1"})),
        (kettle, SimpleNamespace(local_name="Kettle", service_data=None)),
    ]
    assert ble.main(["scan"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"{ADDRESS}  Stadia Setup" in lines and "AA-BB" in lines  # no name: the address alone
    assert "    Fast Pair service data: 00cfe1" in lines and not any("Kettle" in line for line in lines)
    FakeScanner.in_range = []
    assert ble.main(["scan"]) == 0
    assert "No controller found" in capsys.readouterr().out
