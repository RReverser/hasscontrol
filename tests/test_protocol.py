import importlib.util
import os
import struct
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "protocol", os.path.join(os.path.dirname(__file__), "..", "custom_components", "garmin_ble", "protocol.py"))
p = importlib.util.module_from_spec(_spec)
sys.modules["protocol"] = p
_spec.loader.exec_module(p)

KEY = bytes(range(16))
NONCE = bytes.fromhex("0102030405060708")


def test_command_roundtrip_and_counter():
    s = p.Session(NONCE)
    for ctr in range(1, 600):
        f = p.build_command(KEY, NONCE, ctr, p.OP_GET, bytes([3]))
        assert len(f) <= 20
        op, got, payload = s.verify(KEY, f)
        assert (op, got, payload) == (p.OP_GET, ctr, bytes([3]))


def test_replay_rejected():
    s = p.Session(NONCE)
    f1 = p.build_command(KEY, NONCE, 1, p.OP_LIST)
    s.verify(KEY, f1)
    with pytest.raises(p.ProtocolError) as e:
        s.verify(KEY, f1)  # reconstructs to 257: outside window
    assert e.value.status == p.ST_BAD_AUTH


def test_gap_within_window_accepted_beyond_rejected():
    s = p.Session(NONCE)
    s.verify(KEY, p.build_command(KEY, NONCE, 1, p.OP_LIST))
    s.verify(KEY, p.build_command(KEY, NONCE, 20, p.OP_LIST))
    with pytest.raises(p.ProtocolError):
        s.verify(KEY, p.build_command(KEY, NONCE, 60, p.OP_LIST))


def test_wrong_key_or_nonce_rejected():
    s = p.Session(NONCE)
    with pytest.raises(p.ProtocolError):
        s.verify(bytes(16), p.build_command(KEY, NONCE, 1, p.OP_LIST))
    with pytest.raises(p.ProtocolError):
        s.verify(KEY, p.build_command(KEY, bytes(8), 1, p.OP_LIST))


def test_max_payload_fits():
    f = p.build_command(KEY, NONCE, 1, p.OP_ACTION, bytes(14))
    assert len(f) == 20
    with pytest.raises(ValueError):
        p.build_command(KEY, NONCE, 1, p.OP_ACTION, bytes(15))


def test_entity_roundtrip_through_fragments():
    attrs = {"friendly_name": "Living Room Lights ✓", "unit_of_measurement": "°C",
             "device_class": "temperature", "icon": "mdi:thermometer",
             "options": ["a", "b c"], "min": 0, "max": 30.5, "step": 0.5}
    msg = p.encode_entity(7, "sensor.living_room_temperature", "21.5", attrs)
    fr, re = p.Fragmenter(), p.Reassembler()
    frags = fr.split(msg)
    assert all(len(x) <= 20 for x in frags)
    out = None
    for x in frags:
        out = re.feed(x)
    d = p.decode_entity(out)
    assert d == {"idx": 7, "entity_id": "sensor.living_room_temperature", "state": "21.5",
                 "friendly_name": "Living Room Lights ✓", "unit": "°C", "device_class": "temperature",
                 "icon": "mdi:thermometer", "options": ["a", "b c"], "min": "0", "max": "30.5", "step": "0.5"}


def test_reassembler_recovers_from_lost_fragment():
    fr, re = p.Fragmenter(), p.Reassembler()
    a = fr.split(bytes(50))
    b = fr.split(b"\x83\x05")
    assert re.feed(a[0]) is None  # a[1], a[2] lost
    assert re.feed(b[0]) == b"\x83\x05"


def test_float_arg():
    assert p.unpack_float(struct.pack(">f", 21.5)) == 21.5
