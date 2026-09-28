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
        server = srv.GarminBleServer(hass, "e1", "garmin", "hci0", 30)
        await server.async_start()
        server._watches["AA"] = {"code": "000000", "at": 0, "key": KEY.hex()}  # dev_AA used by _hello()
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
async def test_hello_without_key_gets_key(hass_env):
    hass, server, _ = await hass_env()
    dev = "/org/bluez/hci0/dev_AA"
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello(has_key=False))
    msgs = server.periph.messages()
    assert msgs[0] == p.encode_key(KEY)
    assert msgs[1][0] == p.MSG_CHALLENGE
    await server._on_write(dev, bytes([p.OP_HELLO, 1]))  # old protocol
    assert server.periph.messages() == [p.encode_result(0, p.ST_BAD_FRAME)]
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_async_approval(hass_env, monkeypatch):
    hass, server, _ = await hass_env()
    flows = []
    monkeypatch.setattr(srv.discovery_flow, "async_create_flow",
                        lambda hass, domain, context, data: flows.append(data))
    pairing = server.periph.pairing
    dev = "/org/bluez/hci0/dev_90_F1_57_AB_AA_08"
    addr = "90:F1:57:AB:AA:08"
    # Just Works has no code to compare: refused
    assert await pairing.confirm(dev, None, "just_works") is False
    # numeric comparison: bond accepted at once, approval asked once
    assert await pairing.confirm(dev, 654321, "numeric_comparison") is True
    assert server.pending == {addr: {"code": "654321", "at": server.pending[addr]["at"]}}
    assert flows == [{"entry_id": "e1", "address": addr, "code": "654321"}]
    assert not server.is_approved(dev)
    # not approved yet: HELLO refused, no second flow
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello(has_key=False))
    assert server.periph.messages() == [p.encode_result(0, p.ST_NOT_APPROVED)]
    assert len(flows) == 1
    # approved any time later: key issued on next HELLO
    assert await server.approve(addr) is True
    assert await server.approve(addr) is False
    assert server.is_approved(dev) and addr not in server.pending
    key = bytes.fromhex(server.watches[addr]["key"])
    await server._on_write(dev, p.build_hello(has_key=False))
    msgs = server.periph.messages()
    assert msgs[0] == p.encode_key(key) and msgs[1][0] == p.MSG_CHALLENGE
    nonce = msgs[1][1:9]
    await server._on_write(dev, p.build_command(key, nonce, 1, p.OP_LIST))
    assert server.periph.messages()[-1][0] == p.MSG_LIST_END
    # new bond for an approved address: approval dropped, asked again
    assert await pairing.confirm(dev, 111111, "numeric_comparison") is True
    assert not server.is_approved(dev) and server.pending[addr]["code"] == "111111"
    assert len(flows) == 2
    # forget: bond removed, HELLO refused as unpaired
    await server.forget(["AA", addr])
    assert server.periph.removed == ["/org/bluez/hci0/dev_AA", "/org/bluez/hci0/dev_90_F1_57_AB_AA_08"]
    await server._on_write(dev, p.build_hello())
    assert server.periph.messages() == [p.encode_result(0, p.ST_NOT_PAIRED)]
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_approve_flow(hass_env):
    from custom_components.garmin_ble.config_flow import GarminBleConfigFlow
    from custom_components.garmin_ble.const import DOMAIN

    hass, server, _ = await hass_env()
    from homeassistant import config_entries
    hass.data[DOMAIN] = {"e1": server}
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await server.periph.pairing.confirm("/org/bluez/hci0/dev_11_22", 42, "numeric_comparison")

    def flow():
        f = GarminBleConfigFlow()
        f.hass, f.handler, f.flow_id = hass, DOMAIN, "x"
        f.context = {"source": "integration_discovery"}
        return f

    f = flow()
    res = await f.async_step_integration_discovery({"entry_id": "e1", "address": "11:22", "code": "000042"})
    assert res["type"] == "form" and res["step_id"] == "approve"
    assert res["description_placeholders"] == {"address": "11:22", "code": "000042"}
    assert (await f.async_step_approve({}))["reason"] == "watch_approved"
    assert "11:22" in server.watches
    f = flow()
    await f.async_step_integration_discovery({"entry_id": "e1", "address": "11:22", "code": "000042"})
    assert (await f.async_step_approve({}))["reason"] == "not_pending"
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_pending_limits_ignore_and_expiry(hass_env, monkeypatch):
    hass, server, _ = await hass_env()
    monkeypatch.setattr(srv.discovery_flow, "async_create_flow", lambda *a, **k: None)
    pairing = server.periph.pairing

    def dev(n):
        return f"/org/bluez/hci0/dev_00_00_00_00_00_0{n}"

    # at most PENDING_MAX watches wait; the next new one is refused
    for n in range(srv.PENDING_MAX):
        assert await pairing.confirm(dev(n), 100 + n, "numeric_comparison") is True
    assert await pairing.confirm(dev(9), 999, "numeric_comparison") is False
    # a waiting watch may pair again (new code replaces the old one)
    assert await pairing.confirm(dev(0), 555, "numeric_comparison") is True
    assert server.pending["00:00:00:00:00:00"]["code"] == "000555"
    # Ignore: dropped with its bond, refused for a while, then allowed again
    await server.reject("00:00:00:00:00:00")
    assert "00:00:00:00:00:00" not in server.pending
    assert server.periph.removed == [dev(0)]
    assert await pairing.confirm(dev(0), 1, "numeric_comparison") is False
    server._refused_until["00:00:00:00:00:00"] = 0
    assert await pairing.confirm(dev(0), 1, "numeric_comparison") is True
    # expiry after PENDING_TTL
    server.pending["00:00:00:00:00:01"]["at"] -= srv.PENDING_TTL + 1
    await server._expire_pending()
    assert "00:00:00:00:00:01" not in server.pending
    assert server.periph.removed[-1] == dev(1)
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_ignore_flow_rejects_watch(hass_env):
    from homeassistant import config_entries
    from custom_components.garmin_ble.config_flow import GarminBleConfigFlow
    from custom_components.garmin_ble.const import DOMAIN

    hass, server, _ = await hass_env()
    hass.data[DOMAIN] = {"e1": server}
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await server.periph.pairing.confirm("/org/bluez/hci0/dev_11_22", 42, "numeric_comparison")
    f = GarminBleConfigFlow()
    f.hass, f.handler, f.flow_id, f.context = hass, DOMAIN, "y", {"source": "ignore"}
    res = await f.async_step_ignore({"unique_id": "watch_11:22", "title": "x"})
    assert res["type"] == "abort" and res["reason"] == "watch_ignored"
    assert "11:22" not in server.pending and server.periph.removed == ["/org/bluez/hci0/dev_11_22"]
    await hass.async_stop(force=True)
