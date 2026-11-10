"""Stadia Controller BLE Wi-Fi provisioner (``stadiacast-provision``).

Finds a Stadia Controller in Bluetooth LE setup mode and sends it Wi-Fi credentials over its
setup GATT service, the way the Stadia app did.  Today this is a manual step done once per
controller; making it automatic is on the roadmap.

    stadiacast-provision scan
    stadiacast-provision provision --address ADDR --ssid SSID [--password PASS] [--wifi-scan]
    stadiacast-provision list-services --address ADDR
    stadiacast-provision windows --address ADDR

The credentials are one JSON object written to a characteristic of the setup service, with the
password RSA-encrypted under the controller's public key; ``docs/protocol.md`` describes the
service in its section on Wi-Fi provisioning.  Requires the ``ble`` extra (``bleak``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import getpass
import gzip
import json
import os
import re
import sys
import zlib
from collections.abc import Sequence
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:  # the ble extra is optional: main() says so
    BLEAK_AVAILABLE = False
else:
    BLEAK_AVAILABLE = True

DESCRIPTION = """\
Send Wi-Fi credentials to a Stadia Controller in Bluetooth setup mode (its light blinks orange),
the way the Stadia app did.

    stadiacast-provision scan
    stadiacast-provision provision --address ADDR --ssid SSID

Needs the ble extra: pip install 'stadiacast[ble]'.
"""

FAST_PAIR_SERVICE_DATA_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
STADIA_NAME_HINTS = ("stadia", "setup")

# The characteristics of the setup service (0000fea0-...).
CHAR_CONNECT = "0328fe40-002f-11e5-87d0-0002a5d5c51b"  # connect_to_network write
CHAR_SETTINGS = "0228fe40-002f-11e5-87d0-0002a5d5c51b"  # settings write (triggers a Wi-Fi rescan)
CHAR_RESPONSE = "0428fe40-002f-11e5-87d0-0002a5d5c51b"  # JSON response stream
CHAR_STATE = "0c7c6160-fab2-11e4-9fbb-0002a5d5c51b"  # setup state byte
CHAR_OTA_STATUS = "127c6160-fab2-11e4-9fbb-0002a5d5c51b"  # OTA status JSON
CHAR_IP = "0d7c6160-fab2-11e4-9fbb-0002a5d5c51b"  # IP address

# The values the controller reports on CHAR_STATE.
STATE_NAMES = {
    0x14: "scan/netcheck start",
    0x16: "scan/netcheck done",
    0x1E: "connecting",
    0x34: "JSON parse failure",
    0x3C: "connected",
    0x4E: "command JSON parse failure",
    0x4F: "credential error / connect failed",
}
STATE_SCAN_DONE = 0x16
STATE_CONNECTED = 0x3C
STATE_JOIN_FAILED = 0x4F
#: The states that end a provisioning in failure: the controller could not read the credentials
#: (0x34, 0x4e) or could not join the network with them.
FAILURE_STATES = (0x34, 0x4E, STATE_JOIN_FAILED)

# The security enums written without a Wi-Fi scan (build_connect_payload): WPA2-Personal (AES)
# with a password, an open network without one.
WPA2_PERSONAL = (7, 4)
OPEN_NETWORK = (1, 1)
WPA_AUTH_OPEN = 1
WPA_AUTHS_WITH_PASSPHRASE = (5, 7)  # WPA-PSK and WPA2-PSK
NO_ADDRESS = b"\x7f\x00\x00\x01"  # what CHAR_IP reads while the controller is not on a network


def _window_data_char(page: str) -> str:
    return f"{page}7c6160-fab2-11e4-9fbb-0002a5d5c51b"


def _window_control_char(page: str) -> str:
    return f"{page}6c6160-fab2-11e4-9fbb-0002a5d5c51b"


# Windowed-read pairs: write the page offset to the control characteristic, read gzip data from
# the paired data characteristic in 512-byte windows.
WINDOW_STATE = (_window_data_char("1c"), _window_control_char("1c"))  # device state JSON
WINDOW_SCAN = (_window_data_char("15"), _window_control_char("15"))  # scan results JSON
WINDOW_INFO = (_window_data_char("19"), _window_control_char("19"))  # device info JSON
WINDOW_CRYPTO = (_window_data_char("1a"), _window_control_char("1a"))  # crypto bundle (public key, certs)
WINDOW_PAGE_SIZE = 512


class ProvisioningError(Exception):
    """A step of the provisioning flow failed; the message is for the user."""


def build_connect_payload(
    ssid: str,
    enc_passwd: str | None,
    wpa_auth: int,
    wpa_cipher: int,
    bssid: str | None = None,
    frequency: int | None = None,
) -> dict[str, Any]:
    """The connect_to_network payload.

    ``wpa_auth`` / ``wpa_cipher`` must match the network's security (Google enum family:
    wpa_auth 1=open, 2=WEP, 5=WPA-PSK, 7=WPA2-PSK; wpa_cipher 1=none, 2=WEP, 3=TKIP, 4=CCMP);
    with other values the controller reports state 0x4f.  ``bssid`` and ``frequency`` come from
    the controller's own scan and can be left out: the controller then finds the network itself.
    """
    params: dict[str, Any] = {"ssid": ssid, "wpa_auth": wpa_auth, "wpa_cipher": wpa_cipher}
    if bssid:
        params["bssid"] = bssid
    if frequency:
        params["frequency"] = frequency
    if enc_passwd:
        params["enc_passwd"] = enc_passwd
    return params


def check_ssid(ssid: str) -> None:
    if not 1 <= len(ssid.encode()) <= 32:
        raise ProvisioningError("a network name has 1 to 32 bytes")


def check_password(ssid: str, password: str | None, wpa_auth: int) -> None:
    """Refuse a password the network's security cannot have, before the controller spends a
    join on it."""
    if wpa_auth == WPA_AUTH_OPEN:
        if password:
            raise ProvisioningError(
                f"wpa_auth 1 makes {ssid!r} an open network, which has no password: leave the password out"
            )
        return
    if not password:
        raise ProvisioningError(
            f"{ssid!r} needs a password (wpa_auth {wpa_auth}): give it at the prompt, with --password "
            "or in STADIA_WIFI_PW"
        )
    if wpa_auth in WPA_AUTHS_WITH_PASSPHRASE:
        is_passphrase = 8 <= len(password.encode()) <= 63
        if not is_passphrase and not re.fullmatch(r"[0-9a-fA-F]{64}", password):
            raise ProvisioningError("a WPA or WPA2 password has 8 to 63 characters, or is a key of 64 hex digits")


def encrypt_password(password: str, public_key: rsa.RSAPublicKey) -> str:
    """The ``enc_passwd`` value: the password under RSA PKCS#1 v1.5, in base64."""
    try:
        ct = public_key.encrypt(password.encode(), padding.PKCS1v15())
    except ValueError as e:  # longer than this key can take
        raise ProvisioningError("the password is too long for the controller's key") from e
    return base64.b64encode(ct).decode()


def load_public_key(bundle_json: bytes) -> rsa.RSAPublicKey:
    """The RSA public key from the crypto window's JSON (pair 1a)."""
    try:
        bundle = json.loads(bundle_json.decode("utf-8", "replace"))
        key = serialization.load_der_public_key(base64.b64decode(bundle["device_info"]["public_key"]))
    except (ValueError, KeyError, TypeError, RecursionError, UnsupportedAlgorithm) as e:
        raise ProvisioningError(
            f"the controller's public key could not be read ({len(bundle_json)} bytes): run the command again"
        ) from e
    if not isinstance(key, rsa.RSAPublicKey):
        raise ProvisioningError(f"the controller's public key is not an RSA key ({type(key).__name__})")
    return key


def window_payload(data: bytes) -> bytes:
    """Decompress a windowed read when it is gzip; one that does not decompress is returned as read."""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except (OSError, EOFError, zlib.error):  # a stream cut short raises EOFError, corrupt data zlib.error
            pass
    return data


def redact(message: str) -> str:
    """Hide the encrypted password in logged payloads."""
    return re.sub(r'"enc_passwd":\s*"[^"]*"', '"enc_passwd": "<redacted>"', message)


def is_stadia_advertiser(device: Any, adv: Any) -> bool:
    name = (adv.local_name or "") + (device.name or "")
    return FAST_PAIR_SERVICE_DATA_UUID in (adv.service_data or {}) or any(h in name.lower() for h in STADIA_NAME_HINTS)


def describe(error: BaseException) -> str:
    """A Bluetooth error in a few words: some arrive without a message, some with more than one."""
    if isinstance(error, TimeoutError) and not str(error):
        return "timed out"
    if type(error).__str__ is BaseException.__str__ and len(error.args) > 1 and isinstance(error.args[0], str):
        return error.args[0]  # "no Bluetooth adapter" and its like carry (message, reason): the message
    return str(error) or type(error).__name__


async def window_read(client: Any, pair: tuple[str, str], pages: int = 24) -> bytes:
    """Read a windowed-read pair page by page via its control characteristic.

    Up to 24 pages of 512 bytes by default: the crypto bundle alone has taken 13.
    """
    rd, wr = pair
    chunks = []
    for page in range(pages):
        await client.write_gatt_char(wr, bytes([page & 0xFF, page >> 8]), response=True)
        await asyncio.sleep(0.4)
        data = bytes(await client.read_gatt_char(rd))
        if not data:
            break
        chunks.append(data)
        if len(data) < WINDOW_PAGE_SIZE:
            break
    else:
        raise ProvisioningError(f"a window of the controller is longer than {pages} pages of {WINDOW_PAGE_SIZE} bytes")
    return b"".join(chunks)


async def trigger_scan_and_get_network(client: Any, ssid: str, timeout_s: float = 20.0) -> dict[str, Any]:
    """Trigger a Wi-Fi scan (settings characteristic) and return the target network's scan entry.

    The security enums are copied from the device's own scan results rather than guessed.
    """
    await client.write_gatt_char(CHAR_SETTINGS, b"{}", response=True)
    scan_done = False
    for _ in range(int(timeout_s)):
        await asyncio.sleep(1.0)
        if bytes(await client.read_gatt_char(CHAR_STATE))[:1] == bytes([STATE_SCAN_DONE]):
            scan_done = True
            break
    raw = window_payload(await window_read(client, WINDOW_SCAN))
    try:
        results = json.loads(raw)
    except (ValueError, RecursionError):
        results = None
    if not isinstance(results, list):
        unfinished = "" if scan_done else f" (it did not report the end of its scan in {timeout_s:.0f} s)"
        raise ProvisioningError(
            f"the controller's scan results could not be read ({len(raw)} bytes){unfinished}: run the command again"
        )
    networks = [net for net in results if isinstance(net, dict)]
    for net in networks:
        if net.get("ssid") == ssid:
            return net
    seen = ", ".join(repr(net.get("ssid")) for net in networks) or "no network"
    raise ProvisioningError(f"the controller's scan does not show {ssid!r}; it shows {seen}")


async def scan() -> None:
    """Scan for Fast Pair (0xFE2C) advertisers or Stadia-named devices."""
    print("Scanning for BLE devices (10s)...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    matches = [(d, adv) for d, adv in devices.values() if is_stadia_advertiser(d, adv)]
    if not matches:
        print("No controller found. Is it in setup mode (its light blinks orange)?")
        return
    for device, adv in matches:
        name = adv.local_name or device.name
        print(f"{device.address}  {name}" if name else device.address)
        fp = (adv.service_data or {}).get(FAST_PAIR_SERVICE_DATA_UUID)
        if fp:
            print(f"    Fast Pair service data: {bytes(fp).hex()}")


def on_state(_char: Any, data: bytearray) -> None:
    if data:
        state = data[-1]
        print(f"    STATE -> 0x{state:02x} ({STATE_NAMES.get(state, 'unknown')})")


def on_ota(_char: Any, data: bytearray) -> None:
    print(f"    OTA: {bytes(data)!r}")


async def dump_windows(address: str) -> None:
    """Read all windowed-read pairs and print their decompressed payloads."""
    async with BleakClient(address) as client:
        print(f"Connected to {address}")
        for name, pair in (
            ("state", WINDOW_STATE),
            ("scan", WINDOW_SCAN),
            ("info", WINDOW_INFO),
            ("crypto (1a)", WINDOW_CRYPTO),
        ):
            try:
                raw = await window_read(client, pair)
            except ProvisioningError as e:
                print(f"=== {name}: {e}")
                continue
            out = window_payload(raw)
            print(f"=== {name}: {len(raw)} raw -> {len(out)} bytes")
            print(out[:4000].decode("utf-8", "replace"))


async def list_services(address: str) -> None:
    async with BleakClient(address) as client:
        print(f"Connected to {address}")
        for service in client.services:
            print(f"Service {service.uuid}  ({service.description})")
            for char in service.characteristics:
                print(f"  Char {char.uuid}  [{','.join(char.properties)}]")
                for desc in char.descriptors:
                    print(f"    Desc {desc.uuid}")


def _scanned_security(ssid: str, value: Any) -> int:
    """A security enum from the controller's scan entry for ``ssid``."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProvisioningError(
            f"the controller's scan does not give the security of {ssid!r}: "
            "give it with --wpa-auth and --wpa-cipher (see --help)"
        )
    return value


def _join_failure(ssid: str, state: int, *, password: str | None, wifi_scan: bool, security_given: bool) -> str:
    """What to tell the user when the controller reports ``state`` after the credentials."""
    if state != STATE_JOIN_FAILED:
        return (
            f"the controller could not read the credentials (STATE 0x{state:02x}, {STATE_NAMES[state]}): "
            "run the command again"
        )
    if security_given:
        hint = "check the network name, the password and the values given to --wpa-auth and --wpa-cipher"
    elif wifi_scan:
        hint = "check the password" if password else "run the command again"
    elif password:
        hint = (
            "check the network name and the password; for a network that is not WPA2-Personal (AES), "
            "run it again with --wifi-scan"
        )
    else:
        hint = "check the network name; if the network has a password, give it"
    return f"the controller did not join {ssid!r} (STATE 0x{state:02x}): {hint}"


async def provision(
    address: str | None,
    ssid: str,
    password: str | None,
    *,
    wpa_auth: int | None = None,
    wpa_cipher: int | None = None,
    service_data_match: str | None = None,
    wait: float = 45.0,
    wifi_scan: bool = False,
) -> None:
    """Read the device key and write the credentials with the password encrypted.

    The security enums are WPA2-Personal (AES) with a password and an open network without one,
    unless ``wpa_auth`` and ``wpa_cipher`` say otherwise; the payload carries no ``bssid`` or
    ``frequency``, and the controller finds the network by itself.  With ``wifi_scan`` the
    controller scans for Wi-Fi networks first, and the enums (unless given), the bssid and the
    frequency are copied from what it reports for ``ssid``.

    The device is found by ``address`` or, with ``service_data_match``, by a fragment of its Fast
    Pair service data.  Returns once the controller has the credentials: it reported that it
    joined, or dropped the Bluetooth link, which it does on leaving setup mode.  Everything else
    is a :class:`ProvisioningError`.
    """
    check_ssid(ssid)
    auth, cipher = WPA2_PERSONAL if password else OPEN_NETWORK
    auth = wpa_auth if wpa_auth is not None else auth
    cipher = wpa_cipher if wpa_cipher is not None else cipher
    if not wifi_scan or wpa_auth is not None:
        check_password(ssid, password, auth)  # with a scan and no value given, the scan says which security
    wanted = service_data_match.lower() if service_data_match else None

    def match(d: Any, adv: Any) -> bool:
        if address and d.address.lower() == address.lower():
            return True
        if wanted is None:
            return False
        data = bytes((adv.service_data or {}).get(FAST_PAIR_SERVICE_DATA_UUID, b""))
        return wanted in data.hex()

    print(f"Looking for {address or 'the controller'} (up to 30s)...")
    device = await BleakScanner.find_device_by_filter(match, timeout=30.0)
    if device is None:
        raise ProvisioningError(
            "the controller was not found: it has to be in setup mode (its light blinks orange) and in range; "
            "`stadiacast-provision scan` lists what is"
        )

    states: list[int] = []
    link_dropped = False
    settled = asyncio.Event()  # the watch after the write has its answer

    def on_state_seen(char: Any, data: bytearray) -> None:
        on_state(char, data)
        if data:
            states.append(data[-1])
            if data[-1] in FAILURE_STATES:
                settled.set()

    def on_disconnect(_client: Any) -> None:
        nonlocal link_dropped
        link_dropped = True
        settled.set()

    print(f"Connecting to {device.address}...")
    client = BleakClient(device, timeout=30.0, disconnected_callback=on_disconnect)
    await client.connect()
    try:
        print(f"Connected to {device.address}")
        scan_asked = False
        net: dict[str, Any] = {}
        seen_before = 0  # the states notified before the credentials went out say nothing about them
        try:
            await client.start_notify(CHAR_STATE, on_state_seen)
            await client.start_notify(CHAR_OTA_STATUS, on_ota)

            # 1. the device RSA public key, before any Wi-Fi scan: some controllers drop the BLE link
            # a few seconds after that scan, and the key is the longest read of the exchange
            enc = None
            if password:
                bundle = window_payload(await window_read(client, WINDOW_CRYPTO))
                public_key = load_public_key(bundle)
                print(f"Device RSA public key: {public_key.key_size} bits")
                enc = encrypt_password(password, public_key)

            # 2. with wifi_scan, the security the controller's own scan reports for the target, with its
            # bssid and frequency; values given by hand win
            if wifi_scan:
                print(f"Scanning for {ssid!r}...")
                scan_asked = True
                net = await trigger_scan_and_get_network(client, ssid)
                print(
                    f"Found {ssid!r}: wpa_auth={net.get('wpa_auth')}, wpa_cipher={net.get('wpa_cipher')}, "
                    f"bssid {net.get('bssid')}, {net.get('signal_level')} dBm"
                )
                if wpa_auth is None:
                    auth = _scanned_security(ssid, net.get("wpa_auth"))
                if wpa_cipher is None:
                    cipher = _scanned_security(ssid, net.get("wpa_cipher"))
                check_password(ssid, password, auth)

            # 3. the credentials
            payload = build_connect_payload(ssid, enc, auth, cipher, net.get("bssid"), net.get("frequency"))
            message = json.dumps(payload, separators=(",", ":"))
            print(f"-> {CHAR_CONNECT[:2]}: {redact(message)}")
            seen_before = len(states)
            settled.clear()
            await client.write_gatt_char(CHAR_CONNECT, message.encode(), response=True)
        except (BleakError, OSError) as e:
            if scan_asked:
                security = "--wpa-auth and --wpa-cipher (see --help)"
                if net:  # the scan was read before the link failed: these are the values it gave
                    security = f"--wpa-auth {auth} --wpa-cipher {cipher}"
                raise ProvisioningError(
                    f"the Bluetooth link failed after the controller's Wi-Fi scan was asked for ({describe(e)}); "
                    f"some controllers drop it then. Run the command again without --wifi-scan, giving {security}"
                ) from e
            if link_dropped:
                raise ProvisioningError(
                    f"the controller dropped the Bluetooth link before it had the credentials ({describe(e)}): "
                    "check that it is still in setup mode and run the command again"
                ) from e
            raise

        # 4. the outcome.  The controller reports 0x3c once it has joined and then drops the link,
        # which is how it leaves setup mode; it often drops the link without reporting anything.
        print("Sent. Waiting for the controller (it drops the Bluetooth link when it leaves setup mode)...")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(settled.wait(), wait)
        # the first state that settles it counts: a controller that joined does not report a failure later
        verdict = next((s for s in states[seen_before:] if s == STATE_CONNECTED or s in FAILURE_STATES), None)
        failed = verdict is not None and verdict in FAILURE_STATES
        ip = b""
        if not link_dropped and not failed:
            try:
                ip = bytes(await asyncio.wait_for(client.read_gatt_char(CHAR_IP), 5.0))
            except (BleakError, OSError):  # the link is going down
                link_dropped = True
        left_setup_mode = link_dropped  # taken here: our own disconnect below is reported as a drop too
    finally:
        with contextlib.suppress(BleakError, OSError):
            await client.disconnect()

    on_network = len(ip) == 4 and ip not in (NO_ADDRESS, bytes(4))
    if on_network:
        print(f"Controller IP: {'.'.join(str(b) for b in ip)}")
    if verdict is not None and failed:
        security_given = wpa_auth is not None or wpa_cipher is not None
        raise ProvisioningError(
            _join_failure(ssid, verdict, password=password, wifi_scan=wifi_scan, security_given=security_given)
        )
    if verdict == STATE_CONNECTED:
        print(f"Done: the controller joined {ssid!r}.")
    elif left_setup_mode:
        print("Done: the controller has the credentials and left setup mode.")
    elif on_network:
        print("Done: the controller is on the network.")
    else:
        raise ProvisioningError(
            f"the controller gave no sign of joining {ssid!r} in {wait:.0f} s: check that it is still in setup "
            "mode and run the command again (--wait sets how long to watch)"
        )


def _hex_digits(text: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]+", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not hexadecimal")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stadiacast-provision", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list the controllers in setup mode (Fast Pair 0xFE2C advertisers)")
    p_ls = sub.add_parser("list-services", help="dump the live GATT table")
    p_ls.add_argument("--address", "-a", required=True)
    p_wd = sub.add_parser("windows", help="dump windowed-read payloads (state, scan, info, crypto)")
    p_wd.add_argument("--address", "-a", required=True)
    p_pr = sub.add_parser("provision", help="send Wi-Fi credentials to a controller in setup mode")
    p_pr.add_argument("--address", "-a", help="the controller's BLE address, as scan prints it (a UUID on macOS)")
    p_pr.add_argument("--ssid", "-s", required=True, help="the Wi-Fi network's name")
    p_pr.add_argument("--password", "-p", help="omit to be prompted securely (or set STADIA_WIFI_PW)")
    p_pr.add_argument(
        "--wpa-auth",
        type=int,
        default=None,
        help="the network's security: 7 WPA2-PSK (the default with a password), 1 open (the default without), "
        "5 WPA-PSK, 2 WEP",
    )
    p_pr.add_argument(
        "--wpa-cipher",
        type=int,
        default=None,
        help="the network's cipher: 4 CCMP/AES (the default with a password), 1 none (the default without), "
        "3 TKIP, 2 WEP",
    )
    p_pr.add_argument(
        "--wifi-scan",
        action="store_true",
        help="have the controller scan for Wi-Fi networks first and copy your network's security from what it "
        "reports, for a network that is not WPA2-Personal (--wpa-auth and --wpa-cipher still win); some "
        "controllers drop the Bluetooth link after that scan",
    )
    p_pr.add_argument(
        "--service-data-match",
        metavar="HEX",
        type=_hex_digits,
        default=None,
        help="find the controller by a fragment of its Fast Pair service data, as scan prints it, instead of "
        "or next to --address (addresses are not stable on macOS)",
    )
    p_pr.add_argument(
        "--wait", type=float, default=45.0, help="seconds to wait for the controller after the write (default: 45)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "provision" and not (args.address or args.service_data_match):
        parser.error("provision needs --address or --service-data-match")
    if not BLEAK_AVAILABLE:
        print("bleak is not installed: install the BLE extra (pip install 'stadiacast[ble]')", file=sys.stderr)
        return 2
    try:
        if args.cmd == "scan":
            asyncio.run(scan())
        elif args.cmd == "list-services":
            asyncio.run(list_services(args.address))
        elif args.cmd == "windows":
            asyncio.run(dump_windows(args.address))
        elif args.cmd == "provision":
            password = args.password or os.environ.get("STADIA_WIFI_PW") or None
            if password is None:
                # a passphrase may begin or end with a space: only an answer of nothing but spaces is blank
                answer = getpass.getpass(f"Wi-Fi password for {args.ssid!r} (blank for open): ")
                password = answer if answer.strip() else None
            asyncio.run(
                provision(
                    args.address,
                    args.ssid,
                    password,
                    wpa_auth=args.wpa_auth,
                    wpa_cipher=args.wpa_cipher,
                    service_data_match=args.service_data_match,
                    wait=args.wait,
                    wifi_scan=args.wifi_scan,
                )
            )
    except KeyboardInterrupt:
        print()
        return 130
    except ProvisioningError as e:
        print(f"stadiacast-provision: {e}", file=sys.stderr)
        return 1
    except (BleakError, OSError) as e:  # the Bluetooth stack: no connection, a dropped link, Bluetooth off
        print(f"stadiacast-provision: Bluetooth: {describe(e)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
