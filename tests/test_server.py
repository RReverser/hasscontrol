"""Exercise GarminBleServer against a real HomeAssistant core with a fake BLE peripheral."""
import asyncio
import os
import struct
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402

from custom_components.garmin_ble import protocol as p  # noqa: E402
from custom_components.garmin_ble import server as srv  # noqa: E402

KEY = bytes(range(16))


class FakePeriph:
    def __init__(self, adapter, on_write, on_device, pairing=None):
        self.on_write, self.on_device, self.pairing = on_write, on_device, pairing
        self.frames, self.disconnected, self.removed = [], [], []
        self.adapter_path = f"/org/bluez/{adapter}"

    async def remove_device(self, path):
        self.removed.append(path)

    async def start(self):
        pass

    async def stop(self):
        pass

    def notify(self, frames):
        self.frames += frames

    async def disconnect(self, dev):
        self.disconnected.append(dev)

    def messages(self):
        r, out = p.Reassembler(), []
        for f in self.frames:
            m = r.feed(f)
            if m is not None:
                out.append(m)
        self.frames = []
        return out


@pytest.fixture
def hass_env(monkeypatch):
    monkeypatch.setattr(srv, "BlePeripheral", FakePeriph)

    async def make():
        hass = HomeAssistant(tempfile.mkdtemp())
        await er.async_load(hass)
        await lr.async_load(hass)
        label = lr.async_get(hass).async_create("garmin")
        reg = er.async_get(hass)
        calls = []
        for eid, state, attrs in (
            ("input_boolean.kettle", "off", {"friendly_name": "Kettle"}),
            ("input_select.mode", "home", {"options": ["home", "away", "night"]}),
            ("input_number.temp", "20.0", {"min": 10, "max": 30, "step": 0.5}),
            ("lock.front_door", "locked", {}),
            ("switch.not_exposed", "off", {}),
        ):
            domain, obj = eid.split(".")
            e = reg.async_get_or_create(domain, "test", obj, suggested_object_id=obj)
            if eid != "switch.not_exposed":
                reg.async_update_entity(e.entity_id, labels={label.label_id})
            hass.states.async_set(e.entity_id, state, attrs)

        def handler(call: ServiceCall):
            calls.append((call.domain, call.service, dict(call.data)))
            if call.service in ("turn_on", "turn_off"):
                hass.states.async_set(call.data["entity_id"], call.service[5:], {"friendly_name": "Kettle"})

        for d, s in (("input_boolean", "turn_on"), ("input_boolean", "turn_off"), ("input_select", "select_option"),
                     ("input_number", "set_value"), ("lock", "unlock"), ("switch", "turn_on")):
            hass.services.async_register(d, s, handler)
        server = srv.GarminBleServer(hass, "e1", KEY, "garmin", "hci0", 30)
        await server.async_start()
        server._watches["AA"] = {"code": "000000", "at": 0}  # dev_AA used by _hello()
        return hass, server, calls

    return make


async def _hello(server):
    dev = "/org/bluez/hci0/dev_AA"
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello())
    ch = server.periph.messages()[0]
    assert ch[0] == p.MSG_CHALLENGE and ch[9] == p.PROTOCOL_VERSION
    return dev, ch[1:9], ch[10]


@pytest.mark.asyncio
async def test_full_flow(hass_env):
    hass, server, calls = await hass_env()
    dev, nonce, count = await _hello(server)
    assert count == 4  # not_exposed excluded
    ctr = 0

    async def cmd(op, payload=b""):
        nonlocal ctr
        ctr += 1
        await server._on_write(dev, p.build_command(KEY, nonce, ctr, op, payload))
        await hass.async_block_till_done()
        return server.periph.messages()

    msgs = await cmd(p.OP_LIST)
    ents = [p.decode_entity(m) for m in msgs[:-1]]
    assert msgs[-1] == p.encode_list_end(4)
    ids = [e["entity_id"] for e in ents]
    assert ids == sorted(ids) and "switch.not_exposed" not in ids
    idx = {e["entity_id"]: e["idx"] for e in ents}
    assert ents[idx["input_select.mode"]]["options"] == ["home", "away", "night"]

    # toggle on: RESULT OK plus pushed ENTITY with new state
    msgs = await cmd(p.OP_ACTION, bytes([idx["input_boolean.kettle"], p.ACT_TURN_ON]))
    kinds = [m[0] for m in msgs]
    assert p.encode_result(ctr, p.ST_OK) in msgs and p.MSG_ENTITY in kinds
    assert [p.decode_entity(m) for m in msgs if m[0] == p.MSG_ENTITY][0]["state"] == "on"

    msgs = await cmd(p.OP_ACTION, bytes([idx["input_select.mode"], p.ACT_SELECT_OPTION, 2]))
    assert calls[-1] == ("input_select", "select_option", {"entity_id": "input_select.mode", "option": "night"})

    msgs = await cmd(p.OP_ACTION, bytes([idx["input_number.temp"], p.ACT_SET_VALUE]) + struct.pack(">f", 21.5))
    assert calls[-1][2]["value"] == 21.5

    # wrong action for domain -> NOT_ALLOWED, no call
    n = len(calls)
    msgs = await cmd(p.OP_ACTION, bytes([idx["input_boolean.kettle"], p.ACT_UNLOCK]))
    assert msgs == [p.encode_result(ctr, p.ST_NOT_ALLOWED)] and len(calls) == n

    msgs = await cmd(p.OP_GET, bytes([99]))
    assert msgs == [p.encode_result(ctr, p.ST_BAD_INDEX)]

    # replay of an old frame is rejected
    await server._on_write(dev, p.build_command(KEY, nonce, 1, p.OP_LIST))
    assert server.periph.messages() == [p.encode_result(1, p.ST_BAD_AUTH)]

    msgs = await cmd(p.OP_BYE)
    assert server.periph.disconnected == [dev]
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_no_session_and_wrong_key(hass_env):
    hass, server, calls = await hass_env()
    dev = "/org/bluez/hci0/dev_BB"
    await server._on_write(dev, p.build_command(KEY, bytes(8), 1, p.OP_LIST))
    assert server.periph.messages() == [p.encode_result(1, p.ST_NO_SESSION)]
    dev, nonce, _ = await _hello(server)
    await server._on_write(dev, p.build_command(bytes(16), nonce, 1, p.OP_ACTION, bytes([0, 0])))
    assert server.periph.messages() == [p.encode_result(1, p.ST_BAD_AUTH)]
    assert calls == []
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_idle_disconnect(hass_env):
    hass, server, _ = await hass_env()
    dev, _, _ = await _hello(server)
    server._conns[dev].last_seen -= 31
    server._check_idle(None)
    await hass.async_block_till_done()
    assert server.periph.disconnected == [dev]
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_unapproved_device_refused(hass_env):
    hass, server, calls = await hass_env()
    dev = "/org/bluez/hci0/dev_BB_CC"
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello())
    assert server.periph.messages() == [p.encode_result(0, p.ST_NOT_PAIRED)]
    assert server._conns[dev].session is None
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_pairing_requires_mode_and_ha_confirmation(hass_env, monkeypatch):
    hass, server, _ = await hass_env()
    monkeypatch.setattr(srv, "PAIRING_CONFIRM_SECONDS", 0.2)
    pairing = server.periph.pairing
    dev = "/org/bluez/hci0/dev_90_F1_57_AB_AA_08"
    # pairing mode off: refused at once
    assert await pairing.confirm(dev, 123456, "numeric_comparison") is False
    server.start_pairing_mode(60)
    # Just Works has no code to compare: always refused
    assert await pairing.confirm(dev, None, "just_works") is False
    # not confirmed in HA in time: refused
    assert await pairing.confirm(dev, 111111, "numeric_comparison") is False
    assert not server.is_approved(dev)
    # confirmed in HA while pending: approved, persisted, pairing mode closed
    task = hass.async_create_task(pairing.confirm(dev, 654321, "numeric_comparison"))
    for _ in range(50):
        if server.pending_code == "654321":
            break
        await asyncio.sleep(0.01)
    assert server.pending_code == "654321"
    assert server.confirm_pending() is True
    assert await task is True
    assert server.is_approved(dev) and not server.pairing_mode
    assert "90:F1:57:AB:AA:08" in server.watches
    # approved watch gets a session
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello())
    assert server.periph.messages()[0][0] == p.MSG_CHALLENGE
    # forget: bond removed, HELLO refused again
    await server.forget_watches()
    assert server.periph.removed == ["/org/bluez/hci0/dev_AA", "/org/bluez/hci0/dev_90_F1_57_AB_AA_08"]
    await server._on_write(dev, p.build_hello())
    assert server.periph.messages() == [p.encode_result(0, p.ST_NOT_PAIRED)]
    await hass.async_block_till_done()
    await hass.async_stop(force=True)
