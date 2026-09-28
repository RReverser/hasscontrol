"""Wire format for the HassControl BLE protocol (see PROTOCOL.md).

Pure Python, no Home Assistant imports, so it can be unit-tested and reused by
the desktop test client.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field

FRAME_MAX = 20
TAG_LEN = 4
PROTOCOL_VERSION = 1
CTR_WINDOW = 32

# Watch -> HA ops
OP_HELLO = 0x01
OP_LIST = 0x02
OP_GET = 0x03
OP_ACTION = 0x04
OP_BATTERY = 0x05
OP_BYE = 0x07

# HA -> watch message types
MSG_CHALLENGE = 0x81
MSG_ENTITY = 0x82
MSG_LIST_END = 0x83
MSG_RESULT = 0x84

# RESULT status codes
ST_OK = 0
ST_BAD_AUTH = 1
ST_BAD_INDEX = 2
ST_NOT_ALLOWED = 3
ST_SERVICE_ERROR = 4
ST_BAD_FRAME = 5
ST_NO_SESSION = 6

# ACTION codes
ACT_TURN_ON = 0
ACT_TURN_OFF = 1
ACT_LOCK = 2
ACT_UNLOCK = 3
ACT_CLOSE = 4
ACT_OPEN = 5
ACT_COVER_TOGGLE = 6
ACT_PRESS = 7
ACT_SELECT_OPTION = 0x10
ACT_SET_VALUE = 0x11

# ENTITY TLV tags
T_ENTITY_ID = 1
T_STATE = 2
T_NAME = 3
T_UNIT = 4
T_DEVICE_CLASS = 5
T_ICON = 6
T_OPTIONS = 7
T_MIN = 8
T_MAX = 9
T_STEP = 10

OPTIONS_SEP = "\x1f"


class ProtocolError(Exception):
    """Frame could not be parsed or authenticated."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def mac(key: bytes, nonce: bytes, ctr: int, op: int, payload: bytes) -> bytes:
    """Truncated HMAC-SHA256 tag covering nonce, full counter, op and payload."""
    msg = nonce + struct.pack(">I", ctr & 0xFFFFFFFF) + bytes([op]) + payload
    return hmac.new(key, msg, hashlib.sha256).digest()[:TAG_LEN]


def build_command(key: bytes, nonce: bytes, ctr: int, op: int, payload: bytes = b"") -> bytes:
    """Build an authenticated watch->HA frame (used by tests and the desktop client)."""
    frame = bytes([op, ctr & 0xFF]) + payload + mac(key, nonce, ctr, op, payload)
    if len(frame) > FRAME_MAX:
        raise ValueError(f"frame too long: {len(frame)}")
    return frame


def build_hello() -> bytes:
    return bytes([OP_HELLO, PROTOCOL_VERSION])


@dataclass
class Session:
    """Per-connection authentication state held by the HA side."""

    nonce: bytes
    last_ctr: int = 0

    def reconstruct(self, ctr8: int) -> int:
        cand = (self.last_ctr & ~0xFF) | ctr8
        if cand <= self.last_ctr:
            cand += 0x100
        return cand

    def verify(self, key: bytes, frame: bytes) -> tuple[int, int, bytes]:
        """Authenticate a command frame. Returns (op, ctr, payload)."""
        if len(frame) < 2 + TAG_LEN or len(frame) > FRAME_MAX:
            raise ProtocolError(ST_BAD_FRAME, "bad frame length")
        op, ctr8 = frame[0], frame[1]
        payload, tag = frame[2:-TAG_LEN], frame[-TAG_LEN:]
        ctr = self.reconstruct(ctr8)
        if ctr - self.last_ctr > CTR_WINDOW:
            raise ProtocolError(ST_BAD_AUTH, "counter outside window")
        if not hmac.compare_digest(mac(key, self.nonce, ctr, op, payload), tag):
            raise ProtocolError(ST_BAD_AUTH, "bad tag")
        self.last_ctr = ctr
        return op, ctr, payload


def _tlv(tag: int, value) -> bytes:
    if value is None:
        return b""
    data = str(value).encode("utf-8")[:255]
    return bytes([tag, len(data)]) + data


def encode_entity(idx: int, entity_id: str, state: str | None, attrs: dict) -> bytes:
    out = bytearray([MSG_ENTITY, idx & 0xFF])
    out += _tlv(T_ENTITY_ID, entity_id)
    out += _tlv(T_STATE, state)
    out += _tlv(T_NAME, attrs.get("friendly_name"))
    out += _tlv(T_UNIT, attrs.get("unit_of_measurement"))
    out += _tlv(T_DEVICE_CLASS, attrs.get("device_class"))
    out += _tlv(T_ICON, attrs.get("icon"))
    options = attrs.get("options")
    if isinstance(options, (list, tuple)) and options:
        out += _tlv(T_OPTIONS, OPTIONS_SEP.join(str(o) for o in options))
    for tag, key in ((T_MIN, "min"), (T_MAX, "max"), (T_STEP, "step")):
        if attrs.get(key) is not None:
            out += _tlv(tag, attrs[key])
    return bytes(out)


def decode_entity(msg: bytes) -> dict:
    """Inverse of encode_entity (for tests and the desktop client)."""
    if msg[0] != MSG_ENTITY:
        raise ValueError("not an ENTITY message")
    names = {T_ENTITY_ID: "entity_id", T_STATE: "state", T_NAME: "friendly_name",
             T_UNIT: "unit", T_DEVICE_CLASS: "device_class", T_ICON: "icon",
             T_OPTIONS: "options", T_MIN: "min", T_MAX: "max", T_STEP: "step"}
    out: dict = {"idx": msg[1]}
    i = 2
    while i + 2 <= len(msg):
        tag, ln = msg[i], msg[i + 1]
        val = msg[i + 2:i + 2 + ln].decode("utf-8", "replace")
        if tag == T_OPTIONS:
            val = val.split(OPTIONS_SEP)
        out[names.get(tag, f"t{tag}")] = val
        i += 2 + ln
    return out


def encode_challenge(nonce: bytes, count: int) -> bytes:
    return bytes([MSG_CHALLENGE]) + nonce + bytes([PROTOCOL_VERSION, min(count, 255)])


def encode_list_end(count: int) -> bytes:
    return bytes([MSG_LIST_END, min(count, 255)])


def encode_result(ctr: int, status: int) -> bytes:
    return bytes([MSG_RESULT, ctr & 0xFF, status])


@dataclass
class Fragmenter:
    """Splits messages into <=20-byte notifications with a 1-byte header."""

    seq: int = 0

    def split(self, msg: bytes) -> list[bytes]:
        chunk = FRAME_MAX - 1
        parts = [msg[i:i + chunk] for i in range(0, len(msg), chunk)] or [b""]
        seq = self.seq
        self.seq = (self.seq + 1) & 0x7F
        return [bytes([seq | (0x80 if n == len(parts) - 1 else 0)]) + p for n, p in enumerate(parts)]


@dataclass
class Reassembler:
    """Inverse of Fragmenter (for tests and the desktop client)."""

    _buf: bytearray = field(default_factory=bytearray)
    _seq: int | None = None

    def feed(self, frag: bytes) -> bytes | None:
        hdr, data = frag[0], frag[1:]
        seq = hdr & 0x7F
        if self._seq is not None and seq != self._seq:
            self._buf = bytearray()  # a fragment was lost; start over
        self._seq = seq
        self._buf += data
        if hdr & 0x80:
            msg = bytes(self._buf)
            self._buf, self._seq = bytearray(), None
            return msg
        return None


def parse_action_payload(payload: bytes) -> tuple[int, int, bytes]:
    if len(payload) < 2:
        raise ProtocolError(ST_BAD_FRAME, "short ACTION")
    return payload[0], payload[1], payload[2:]


def unpack_float(arg: bytes) -> float:
    if len(arg) != 4:
        raise ProtocolError(ST_BAD_FRAME, "bad float")
    return struct.unpack(">f", arg)[0]
