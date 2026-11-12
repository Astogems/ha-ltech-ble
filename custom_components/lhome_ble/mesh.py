"""Bluetooth Mesh message building for L Home / Lete vendor lights.

Pure Python (only `cryptography`, which Home Assistant already ships), so the
PC test script can import it too.  Ported from BLE/lhome_mesh.py.

Vendor model, company ID 0x1111 (see BLE/MESH_PROTOCOL.md):
  Power       C7 1111  00 01 <01 on | 00 off>
  Brightness  C6 1111  00 01 D6 <0..255>
  CT          C6 1111  00 01 D5 <cool> <warm>    cool + warm = 255
"""
from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

MESH_PROXY_SERVICE_UUID = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

SEQ_MAX = 0xFFFFFF
DEFAULT_TTL = 16

_OP_POWER = bytes.fromhex("c71111")
_OP_SET = bytes.fromhex("c61111")


def _cmac(key: bytes, msg: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(msg)
    return c.finalize()


def _s1(m: bytes) -> bytes:
    return _cmac(b"\x00" * 16, m)


def k2(netkey: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    """Returns (NID, EncryptionKey, PrivacyKey)."""
    t = _cmac(_s1(b"smk2"), netkey)
    t1 = _cmac(t, p + b"\x01")
    t2 = _cmac(t, t1 + p + b"\x02")
    t3 = _cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(netkey: bytes) -> bytes:
    """Network ID — what a proxy advertises in 0x1828 service data (after a 0x00 byte)."""
    return _cmac(_cmac(_s1(b"smk3"), netkey), b"id64\x01")[8:]


def k4(appkey: bytes) -> int:
    """AID."""
    return _cmac(_cmac(_s1(b"smk4"), appkey), b"id6\x01")[15] & 0x3F


def beacon_key(netkey: bytes) -> bytes:
    """BeaconKey = k1(NetKey, s1("nkbk"), "id128" || 0x01) — authenticates secure network beacons."""
    return _cmac(_cmac(_s1(b"nkbk"), netkey), b"id128\x01")


def parse_secure_beacon(proxy_pdu: bytes, beacon_key_: bytes, network_id: bytes) -> tuple[int, bool] | None:
    """Secure Network Beacon from a proxy (proxy PDU type 1, beacon type 0x01).

    Returns (iv_index, iv_update_in_progress) if it is for our network and its
    authentication value checks out, else None.  Layout after the proxy header:
    01 | flags | NetworkID(8) | IVIndex(4, BE) | AuthValue(8)
    """
    p = proxy_pdu
    if len(p) != 23 or p[0] != 0x01 or p[1] != 0x01:
        return None
    flags, net_id, iv, auth = p[2], p[3:11], p[11:15], p[15:23]
    if net_id != network_id or _cmac(beacon_key_, bytes([flags]) + net_id + iv)[:8] != auth:
        return None
    return int.from_bytes(iv, "big"), bool(flags & 0x02)


def ct_value(kelvin: int, k_min: int = 2700, k_max: int = 6500) -> int:
    """Colour temperature -> D5 value <cool><warm>; 0xFF00 = k_max, 0x00FF = k_min."""
    kelvin = max(k_min, min(k_max, kelvin))
    cool = round(255 * (kelvin - k_min) / (k_max - k_min))
    return (cool << 8) | (255 - cool)


def power_payload(on: bool) -> bytes:
    return _OP_POWER + b"\x00\x01" + bytes([1 if on else 0])


def brightness_payload(level: int) -> bytes:
    return _OP_SET + b"\x00\x01\xd6" + bytes([max(0, min(255, level))])


def ct_payload(kelvin: int, k_min: int = 2700, k_max: int = 6500) -> bytes:
    return _OP_SET + b"\x00\x01\xd5" + struct.pack(">H", ct_value(kelvin, k_min, k_max))


def proxy_segments(proxy_pdu: bytes, max_len: int) -> list[bytes]:
    """Split a complete proxy PDU (type byte + payload) with proxy SAR so each
    write fits in one ATT write of `max_len` bytes (MTU - 3)."""
    if len(proxy_pdu) <= max_len:
        return [proxy_pdu]
    pdu_type = proxy_pdu[0] & 0x3F
    payload = proxy_pdu[1:]
    chunk = max_len - 1
    parts = [payload[i:i + chunk] for i in range(0, len(payload), chunk)]
    out = []
    for i, part in enumerate(parts):
        sar = 0b01 if i == 0 else 0b11 if i == len(parts) - 1 else 0b10
        out.append(bytes([(sar << 6) | pdu_type]) + part)
    return out


class MeshNetwork:
    """Encrypts access messages for one mesh network from one source address."""

    def __init__(self, netkey_hex: str, appkey_hex: str, ivindex: int, src: int) -> None:
        self.netkey = bytes.fromhex(netkey_hex)
        self.appkey = bytes.fromhex(appkey_hex)
        self.ivindex = ivindex
        self.src = src
        self.nid, self._enc_key, self._priv_key = k2(self.netkey)
        self.aid = k4(self.appkey)
        self.network_id = k3(self.netkey)
        self.beacon_key = beacon_key(self.netkey)

    def iv_from_beacon(self, proxy_pdu: bytes) -> int | None:
        """IV index to TRANSMIT with, per an authenticated beacon of this network.
        During an IV update the beacon carries the new index; nodes still send with index - 1."""
        res = parse_secure_beacon(proxy_pdu, self.beacon_key, self.network_id)
        if res is None:
            return None
        iv, updating = res
        return iv - 1 if updating and iv > 0 else iv

    def _net_nonce(self, seq: int, ttl: int) -> bytes:
        return (bytes([0x00, ttl & 0x7F]) + struct.pack(">I", seq)[1:]
                + struct.pack(">H", self.src) + b"\x00\x00" + struct.pack(">I", self.ivindex))

    def _app_nonce(self, seq: int, dst: int) -> bytes:
        return (bytes([0x01, 0x00]) + struct.pack(">I", seq)[1:]
                + struct.pack(">H", self.src) + struct.pack(">H", dst)
                + struct.pack(">I", self.ivindex))

    def proxy_pdu(self, access: bytes, seq: int, dst: int, ttl: int = DEFAULT_TTL) -> bytes:
        """Unsegmented AppKey access message -> complete proxy PDU (type 0, network)."""
        if not 0 <= seq <= SEQ_MAX:
            raise ValueError("sequence number out of range")
        if len(access) > 11:
            raise ValueError("access payload too long for an unsegmented message")
        upper = AESCCM(self.appkey, tag_length=4).encrypt(self._app_nonce(seq, dst), access, None)
        lower = bytes([0x40 | self.aid]) + upper  # SEG=0, AKF=1
        enc = AESCCM(self._enc_key, tag_length=4).encrypt(
            self._net_nonce(seq, ttl), struct.pack(">H", dst) + lower, None)
        header = bytes([ttl & 0x7F]) + struct.pack(">I", seq)[1:] + struct.pack(">H", self.src)
        e = Cipher(algorithms.AES(self._priv_key), modes.ECB()).encryptor()
        pecb = (e.update(b"\x00" * 5 + struct.pack(">I", self.ivindex) + enc[:7]) + e.finalize())[:6]
        obf = bytes(a ^ b for a, b in zip(header, pecb))
        return b"\x00" + bytes([((self.ivindex & 1) << 7) | self.nid]) + obf + enc
