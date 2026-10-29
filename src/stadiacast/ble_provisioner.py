"""Stadia Controller BLE Wi-Fi provisioner (``stadiacast-provision``).

Discovers a Stadia Controller in BLE setup mode and sends it Wi-Fi credentials over the OOBE
setup GATT service, the way the Stadia app did.  Today this is a manual step done once per
controller; making it automatic is on the roadmap.

    stadiacast-provision scan
    stadiacast-provision list-services --address ADDR
    stadiacast-provision windows --address ADDR
    stadiacast-provision provision --address ADDR --ssid SSID [--password PASS]

Protocol summary (service 0000fea0-...):
    0328fe40-002f-11e5-87d0-0002a5d5c51b  write   connect_to_network JSON
    0228fe40-002f-11e5-87d0-0002a5d5c51b  write   settings JSON (triggers a Wi-Fi rescan)
    0428fe40-002f-11e5-87d0-0002a5d5c51b  w+r     JSON response stream
    0c7c6160-fab2-11e4-9fbb-0002a5d5c51b  r+n     OOBE state byte
    127c6160-fab2-11e4-9fbb-0002a5d5c51b  r+n     OTA status JSON

The credential payload is a plain JSON object written bare to the connect characteristic; the
password travels RSA-encrypted with the device's public key (read from the crypto window).
Requires the ``ble`` extra (``bleak``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import gzip
import json
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # the ble extra is optional: main() says so
    BLEAK_AVAILABLE = False
else:
    BLEAK_AVAILABLE = True

FAST_PAIR_SERVICE_DATA_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
STADIA_NAME_HINTS = ("stadia", "setup")

# OOBE setup service characteristics (service 0000fea0-...), confirmed live.
CHAR_CONNECT = "0328fe40-002f-11e5-87d0-0002a5d5c51b"  # connect_to_network write
CHAR_SETTINGS = "0228fe40-002f-11e5-87d0-0002a5d5c51b"  # settings write (triggers a Wi-Fi rescan)
CHAR_RESPONSE = "0428fe40-002f-11e5-87d0-0002a5d5c51b"  # JSON response stream
CHAR_STATE = "0c7c6160-fab2-11e4-9fbb-0002a5d5c51b"  # OOBE state byte
CHAR_OTA_STATUS = "127c6160-fab2-11e4-9fbb-0002a5d5c51b"  # OTA status JSON
CHAR_IP = "0d7c6160-fab2-11e4-9fbb-0002a5d5c51b"  # IP address

# OOBE state machine values observed on CHAR_STATE.
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
    """The connect_to_network payload (confirmed working live).

    ``wpa_auth`` / ``wpa_cipher`` MUST be the values the device's own scan reports for the target
    network (Google enum family: wpa_auth 1=open, 2=WEP, 5=WPA-PSK, 7=WPA2-PSK; wpa_cipher 1=none,
    2=WEP, 3=TKIP, 4=CCMP).  Mismatched values fail silently with state 0x4f.
    """
    params: dict[str, Any] = {"ssid": ssid, "wpa_auth": wpa_auth, "wpa_cipher": wpa_cipher}
    if bssid:
        params["bssid"] = bssid
    if frequency:
        params["frequency"] = frequency
    if enc_passwd:
        params["enc_passwd"] = enc_passwd
    return params


def encrypt_password(password: str, public_key: Any) -> str:
    """RSA PKCS#1 v1.5, base64: the confirmed ``enc_passwd`` wire format."""
    ct = public_key.encrypt(password.encode(), padding.PKCS1v15())
    return base64.b64encode(ct).decode()


def load_public_key(bundle_json: bytes) -> Any:
    """The RSA public key from the crypto-bundle window JSON (pair 1a)."""
    bundle = json.loads(bundle_json.decode("utf-8", "replace"))
    spki = base64.b64decode(bundle["device_info"]["public_key"])
    return serialization.load_der_public_key(spki)


def window_payload(data: bytes) -> bytes:
    """Decompress a windowed read when it is gzip; a truncated stream is returned raw."""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except OSError:
            pass
    return data


def redact(message: str) -> str:
    """Hide the encrypted password in logged payloads."""
    return re.sub(r'"enc_passwd":\s*"[^"]*"', '"enc_passwd": "<redacted>"', message)


def is_stadia_advertiser(device: Any, adv: Any) -> bool:
    name = (adv.local_name or "") + (device.name or "")
    return FAST_PAIR_SERVICE_DATA_UUID in (adv.service_data or {}) or any(h in name.lower() for h in STADIA_NAME_HINTS)


async def window_read(client: Any, pair: tuple[str, str], pages: int = 16) -> bytes:
    """Read a windowed-read pair page by page via its control characteristic."""
    rd, wr = pair
    chunks = []
    for page in range(pages):
        await client.write_gatt_char(wr, bytes([page & 0xFF, page >> 8]), response=True)
        await asyncio.sleep(0.4)
        data = bytes(await client.read_gatt_char(rd))
        if not data:
            break
        chunks.append(data)
        if len(data) < 512:
            break
    return b"".join(chunks)


async def trigger_scan_and_get_network(client: Any, ssid: str, timeout_s: float = 20.0) -> dict[str, Any]:
    """Trigger a Wi-Fi scan (settings characteristic) and return the target network's scan entry.

    The security enums are copied from the device's own scan results rather than guessed.
    """
    await client.write_gatt_char(CHAR_SETTINGS, b"{}", response=True)
    for _ in range(int(timeout_s)):
        await asyncio.sleep(1.0)
        state = (await client.read_gatt_char(CHAR_STATE))[0]
        if state == STATE_SCAN_DONE:
            break
    results: list[dict[str, Any]] = json.loads(window_payload(await window_read(client, WINDOW_SCAN)))
    for net in results:
        if net.get("ssid") == ssid:
            return net
    raise ProvisioningError(f"SSID {ssid!r} not found in scan results: {[n.get('ssid') for n in results]}")


async def scan() -> None:
    """Scan for Fast Pair (0xFE2C) advertisers or Stadia-named devices."""
    print("Scanning for BLE devices (10s)...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    matches = [(d, adv) for d, adv in devices.values() if is_stadia_advertiser(d, adv)]
    if not matches:
        print("No Stadia/Fast Pair devices found. (Is the controller in pairing mode?)")
        return
    for device, adv in matches:
        fp = adv.service_data.get(FAST_PAIR_SERVICE_DATA_UUID)
        print(f"{device.address}  {adv.local_name or device.name!r}")
        if fp:
            print(f"    Fast Pair service data: {fp.hex()}")


def on_state(_char: Any, data: bytearray) -> None:
    if data:
        state = data[-1]
        print(f"    STATE -> 0x{state:02x} ({STATE_NAMES.get(state, 'unknown')})")


def on_ota(_char: Any, data: bytearray) -> None:
    print(f"    OTA: {bytes(data)!r}")


async def dump_windows(address: str) -> None:
    """Read all windowed-read pairs and print their decompressed payloads."""
    async with BleakClient(address) as client:
        print(f"Connected to {address} (MTU {client.mtu_size})")
        for name, pair in (
            ("state", WINDOW_STATE),
            ("scan", WINDOW_SCAN),
            ("info", WINDOW_INFO),
            ("crypto (1a)", WINDOW_CRYPTO),
        ):
            raw = await window_read(client, pair)
            out = window_payload(raw)
            print(f"=== {name}: {len(raw)} raw -> {len(out)} bytes")
            print(out[:4000].decode("utf-8", "replace"))


async def list_services(address: str) -> None:
    async with BleakClient(address) as client:
        print(f"Connected to {address} (MTU {client.mtu_size})")
        for service in client.services:
            print(f"Service {service.uuid}  ({service.description})")
            for char in service.characteristics:
                print(f"  Char {char.uuid}  [{','.join(char.properties)}]")
                for desc in char.descriptors:
                    print(f"    Desc {desc.uuid}")


async def provision(
    address: str,
    ssid: str,
    password: str | None,
    *,
    wpa_auth: int | None = None,
    wpa_cipher: int | None = None,
    service_data_match: str | None = None,
    wait: float = 45.0,
) -> None:
    """Scan, encrypt the password with the device key and write the connect payload."""
    print(f"Looking for {address} (up to 30s)...")
    wanted = service_data_match.lower() if service_data_match else None

    def match(d: Any, adv: Any) -> bool:
        if d.address.lower() == address.lower():
            return True
        if wanted is None:
            return False
        data = bytes((adv.service_data or {}).get(FAST_PAIR_SERVICE_DATA_UUID, b""))
        return wanted in data.hex()

    device = await BleakScanner.find_device_by_filter(match, timeout=30.0)
    if device is None:
        raise ProvisioningError("controller not advertising (pairing mode timed out?)")
    print(f"Connecting to {device.address}...")
    async with BleakClient(device, timeout=30.0) as client:
        print(f"Connected to {device.address} (MTU {client.mtu_size})")
        await client.start_notify(CHAR_STATE, on_state)
        await client.start_notify(CHAR_OTA_STATUS, on_ota)

        # 1. scan and copy the device-reported security enums for the target
        print(f"Scanning for {ssid!r}...")
        net = await trigger_scan_and_get_network(client, ssid)
        print(
            f"Found: {net['ssid']} ({net['signal_level']} dBm, bssid {net['bssid']}, "
            f"wpa_auth={net['wpa_auth']}, wpa_cipher={net['wpa_cipher']})"
        )

        # 2. fetch the device RSA public key and encrypt the password
        enc = None
        if password:
            bundle = window_payload(await window_read(client, WINDOW_CRYPTO))
            public_key = load_public_key(bundle)
            print(f"Device RSA public key: {public_key.key_size} bits")
            enc = encrypt_password(password, public_key)

        payload = build_connect_payload(
            ssid,
            enc,
            wpa_auth if wpa_auth is not None else net["wpa_auth"],
            wpa_cipher if wpa_cipher is not None else net["wpa_cipher"],
            bssid=net.get("bssid"),
            frequency=net.get("frequency"),
        )
        message = json.dumps(payload, separators=(",", ":"))
        print(f"-> {CHAR_CONNECT[:2]}: {redact(message)}")
        data = message.encode()
        max_write = client.mtu_size - 3  # ATT opcode + handle overhead
        if len(data) > max_write:
            raise ProvisioningError(
                f"payload {len(data)}B exceeds ATT write capacity {max_write}B (MTU {client.mtu_size})"
            )
        await client.write_gatt_char(CHAR_CONNECT, data, response=True)
        print("Sent. Watching state (0x3c = connected; the BLE link dropping after the write")
        print("is also a success sign: the controller leaves setup mode once provisioned)...")
        try:
            await asyncio.sleep(wait)
        finally:
            try:
                ip = await client.read_gatt_char(CHAR_IP)
                if bytes(ip) != b"\x7f\x00\x00\x01":
                    print(f"Controller IP: {'.'.join(str(b) for b in ip)}")
            except Exception as e:  # the link usually drops once provisioning completes
                print(f"(final read: {e}; link dropped, likely provisioning complete)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stadiacast-provision", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="scan for the controller (Fast Pair 0xFE2C)")
    p_ls = sub.add_parser("list-services", help="dump the live GATT table")
    p_ls.add_argument("--address", "-a", required=True)
    p_wd = sub.add_parser("windows", help="dump windowed-read payloads (state, scan, info, crypto)")
    p_wd.add_argument("--address", "-a", required=True)
    p_pr = sub.add_parser("provision", help="scan, then send Wi-Fi credentials")
    p_pr.add_argument("--address", "-a", required=True, help="BLE address (a CoreBluetooth UUID on macOS)")
    p_pr.add_argument("--ssid", "-s", required=True)
    p_pr.add_argument("--password", "-p", help="omit to be prompted securely (or set STADIA_WIFI_PW)")
    p_pr.add_argument("--wpa-auth", type=int, default=None, help="override (default: copy from device scan results)")
    p_pr.add_argument("--wpa-cipher", type=int, default=None, help="override (default: copy from device scan results)")
    p_pr.add_argument(
        "--service-data-match",
        metavar="HEX",
        default=None,
        help="also accept a device whose Fast Pair service data contains this hex (addresses are not stable on macOS)",
    )
    p_pr.add_argument("--wait", type=float, default=45.0, help="seconds to watch the state after the write")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
                password = getpass.getpass(f"Wi-Fi password for {args.ssid!r} (blank for open): ").strip() or None
            asyncio.run(
                provision(
                    args.address,
                    args.ssid,
                    password,
                    wpa_auth=args.wpa_auth,
                    wpa_cipher=args.wpa_cipher,
                    service_data_match=args.service_data_match,
                    wait=args.wait,
                )
            )
    except KeyboardInterrupt:
        print()
    except ProvisioningError as e:
        print(f"stadiacast-provision: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
