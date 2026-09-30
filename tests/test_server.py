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
        from homeassistant import config_entries
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
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


async def _pair(server, dev, code, approve_after=None, approve=True):
    """Run the agent's confirm like BlueZ does; submit the card meanwhile."""
    task = asyncio.ensure_future(server.periph.pairing.confirm(dev, code, "numeric_comparison"))
    addr = srv._address(dev)
    for _ in range(100):
        if addr in server.pending:
            break
        await asyncio.sleep(0.01)
    if approve_after is not None:
        await asyncio.sleep(approve_after)
        if approve:
            assert server.approve(addr) is not None
        else:
            server.reject(addr)
    return await task


@pytest.mark.asyncio
async def test_pairing_waits_for_card(hass_env, monkeypatch):
    hass, server, _ = await hass_env()
    flows = []
    monkeypatch.setattr(srv.discovery_flow, "async_create_flow",
                        lambda hass, domain, context, data: flows.append(data))
    pairing = server.periph.pairing
    dev = "/org/bluez/hci0/dev_90_F1_57_AB_AA_08"
    addr = "90:F1:57:AB:AA:08"
    # Just Works has no code to compare: refused
    assert await pairing.confirm(dev, None, "just_works") is False
    # card opens with the code; the watch disconnects (declined or timed out
    # there): refused, card closed
    task = asyncio.ensure_future(pairing.confirm(dev, 111111, "numeric_comparison"))
    await asyncio.sleep(0.05)
    server._on_device(dev, False)
    assert await task is False
    assert flows[-1] == {"entry_id": "e1", "address": addr, "code": "111111"}
    assert addr not in server.pending and not server.is_approved(dev)
    assert server.approve(addr) is None  # late submit does nothing
    # BlueZ cancels (Agent1.Cancel): refused
    task = asyncio.ensure_future(pairing.confirm(dev, 333333, "numeric_comparison"))
    await asyncio.sleep(0.05)
    pairing.cancel()
    assert await task is False
    # Ignore / refuse: False
    assert await _pair(server, dev, 222222, approve_after=0.01, approve=False) is False
    # submitted in HA, then the watch declines: nothing stored, card told
    task = asyncio.ensure_future(pairing.confirm(dev, 555555, "numeric_comparison"))
    await asyncio.sleep(0.05)
    outcome = server.approve(addr)
    assert await task is True  # HA's side of the comparison is yes
    server._on_device(dev, False)  # watch declined: link drops
    assert await outcome is False and not server.is_approved(dev)
    # submitted, and the watch confirms (BlueZ: Paired): stored with a key,
    # card told, link dropped so the watch reconnects bonded
    task = asyncio.ensure_future(pairing.confirm(dev, 654321, "numeric_comparison"))
    await asyncio.sleep(0.05)
    outcome = server.approve(addr)
    assert await task is True
    assert not server.is_approved(dev)  # not before the pairing completes
    pairing.paired(dev)
    assert await outcome is True
    await hass.async_block_till_done()
    assert server.periph.disconnected == [dev]
    assert server.is_approved(dev) and server.watches[addr]["code"] == "654321"
    key = bytes.fromhex(server.watches[addr]["key"])
    server._on_device(dev, True)
    await server._on_write(dev, p.build_hello(has_key=False))
    msgs = server.periph.messages()
    assert msgs[0] == p.encode_key(key) and msgs[1][0] == p.MSG_CHALLENGE
    # a pairing HA did not confirm changes nothing
    pairing.paired("/org/bluez/hci0/dev_11_22")
    await hass.async_block_till_done()
    assert server.periph.disconnected == [dev]
    # forget: bond removed, HELLO refused
    await server.forget([addr])
    assert server.periph.removed[-1] == dev
    await server._on_write(dev, p.build_hello())
    assert server.periph.messages() == [p.encode_result(0, p.ST_NOT_PAIRED)]
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_card_flow_and_ignore(hass_env, monkeypatch):
    from homeassistant import config_entries
    from custom_components.garmin_ble.config_flow import GarminBleConfigFlow
    from custom_components.garmin_ble.const import DOMAIN

    hass, server, _ = await hass_env()
    hass.data[DOMAIN] = {"e1": server}
    monkeypatch.setattr(srv.discovery_flow, "async_create_flow", lambda *a, **k: None)
    dev = "/org/bluez/hci0/dev_11_22"

    def flow(source):
        f = GarminBleConfigFlow()
        f.hass, f.handler, f.flow_id = hass, DOMAIN, "x"
        f.context = {"source": source}
        return f

    task = asyncio.ensure_future(server.periph.pairing.confirm(dev, 42, "numeric_comparison"))
    for _ in range(100):
        if "11:22" in server.pending:
            break
        await asyncio.sleep(0.01)
    f = flow("integration_discovery")
    res = await f.async_step_integration_discovery({"entry_id": "e1", "address": "11:22", "code": "000042"})
    assert res["step_id"] == "approve" and res["description_placeholders"]["code"] == "000042"
    assert f.context["title_placeholders"] == {"address": "11:22", "code": "000042"}
    res = await f.async_step_approve({})
    assert res["type"] == "progress" and res["progress_action"] == "confirm_on_watch"
    assert await task is True
    server.paired("/org/bluez/hci0/dev_11_22")
    await f._wait_task
    res = await f.async_step_confirm_on_watch()
    assert res["type"] == "progress_done" and res["step_id"] == "paired"
    assert (await f.async_step_paired())["reason"] == "watch_paired"
    f = flow("integration_discovery")
    await f.async_step_integration_discovery({"entry_id": "e1", "address": "11:22", "code": "000042"})
    assert (await f.async_step_approve({}))["reason"] == "not_pending"
    # Ignore: the waiting pairing is refused, and later ones while ignored
    task = asyncio.ensure_future(server.periph.pairing.confirm(dev, 43, "numeric_comparison"))
    await asyncio.sleep(0.05)
    res = await flow(config_entries.SOURCE_IGNORE).async_step_ignore({"unique_id": "watch_11:22", "title": "x"})
    assert res["type"] == "create_entry"
    assert await task is False
    entry = config_entries.ConfigEntry(
        domain=DOMAIN, source=config_entries.SOURCE_IGNORE, unique_id="watch_11:22", title="x",
        data={}, options={}, version=1, minor_version=1, discovery_keys={}, subentries_data=None)
    hass.config_entries._entries[entry.entry_id] = entry  # what HA does when the flow finishes
    assert await server.periph.pairing.confirm(dev, 44, "numeric_comparison") is False
    del hass.config_entries._entries[entry.entry_id]
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_ping_keeps_session(hass_env):
    hass, server, _ = await hass_env()
    dev, nonce, _ = await _hello(server)
    server._conns[dev].last_seen -= 25
    await server._on_write(dev, p.build_command(KEY, nonce, 1, p.OP_PING))
    assert server.periph.messages() == []
    assert srv.time.monotonic() - server._conns[dev].last_seen < 1
    await hass.async_stop(force=True)
