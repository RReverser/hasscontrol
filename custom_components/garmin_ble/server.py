"""Protocol server: sessions, allowlist, dispatch to Home Assistant services."""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from dataclasses import dataclass, field

from homeassistant.components import persistent_notification
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant import config_entries
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from . import protocol as p
from .const import DOMAIN, SIGNAL_BATTERY
from .peripheral import BlePeripheral, PairingHandler

_LOGGER = logging.getLogger(__name__)

# A watch left waiting for approval is dropped with its bond after this long.
PENDING_TTL = 24 * 3600

_ON_OFF = {"light", "switch", "fan", "input_boolean", "automation", "siren", "humidifier"}

# (domain, action) -> service. Anything not listed is refused.
_SERVICES: dict[tuple[str, int], str] = {}
for _d in _ON_OFF:
    _SERVICES[(_d, p.ACT_TURN_ON)] = "turn_on"
    _SERVICES[(_d, p.ACT_TURN_OFF)] = "turn_off"
for _d in ("scene", "script"):
    _SERVICES[(_d, p.ACT_TURN_ON)] = "turn_on"
_SERVICES[("script", p.ACT_TURN_OFF)] = "turn_off"
_SERVICES[("lock", p.ACT_LOCK)] = "lock"
_SERVICES[("lock", p.ACT_UNLOCK)] = "unlock"
_SERVICES[("cover", p.ACT_OPEN)] = "open_cover"
_SERVICES[("cover", p.ACT_CLOSE)] = "close_cover"
_SERVICES[("cover", p.ACT_COVER_TOGGLE)] = "toggle"
_SERVICES[("valve", p.ACT_OPEN)] = "open_valve"
_SERVICES[("valve", p.ACT_CLOSE)] = "close_valve"
_SERVICES[("valve", p.ACT_COVER_TOGGLE)] = "toggle"
for _d in ("button", "input_button"):
    _SERVICES[(_d, p.ACT_PRESS)] = "press"
for _d in ("select", "input_select"):
    _SERVICES[(_d, p.ACT_SELECT_OPTION)] = "select_option"
for _d in ("number", "input_number"):
    _SERVICES[(_d, p.ACT_SET_VALUE)] = "set_value"


@dataclass
class _Conn:
    session: p.Session | None = None
    key: bytes | None = None
    last_seen: float = field(default_factory=time.monotonic)


class GarminBleServer:
    def __init__(self, hass: HomeAssistant, entry_id: str, label: str,
                 adapter: str, idle_timeout: int) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._label = label
        self._idle = idle_timeout
        self._conns: dict[str, _Conn] = {}
        self._entities: list[str] = []
        self._frag = p.Fragmenter()
        self._unsubs: list = []
        self._unsub_state = None
        # approved watches: BLE address -> {"key": hex, "code": pairing code, "at": epoch s}
        self._watches: dict[str, dict] = {}
        # bonded but not approved yet: BLE address -> {"code", "at"}
        self._pending: dict[str, dict] = {}
        self._flows_started: set[str] = set()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.watches")
        self.periph = BlePeripheral(adapter, self._on_write, self._on_device, _Pairing(self))

    # ---- lifecycle -------------------------------------------------------
    async def async_start(self) -> None:
        data = (await self._store.async_load()) or {}
        if "approved" in data:
            self._watches, self._pending = data["approved"], data.get("pending", {})
        else:  # first format: {address: {...}} of approved watches
            self._watches = data
        for info in self._watches.values():
            info.setdefault("key", os.urandom(16).hex())
        await self._save()
        self._refresh_entities()
        await self.periph.start()
        await self._expire_pending()
        for addr in self._pending:
            self._ask_approval(addr)
        self._unsubs.append(async_track_time_interval(
            self.hass, self._check_idle, timedelta(seconds=5)))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._expire_pending, timedelta(hours=1)))

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        if self._unsub_state:
            self._unsub_state()
        for dev in list(self._conns):
            await self.periph.disconnect(dev)
        await self.periph.stop()

    # ---- pairing and approval ----------------------------------------------
    # 1. The watch pairs (LE Secure Connections, numeric comparison; the user
    #    confirms the code on the watch). HA's agent accepts the bond at once
    #    and records the code: a bond alone grants nothing.
    # 2. HA opens a discovery flow ("Allow Garmin watch X? code NNNNNN") that
    #    the user can approve at any time; until then HELLO gets NOT_APPROVED
    #    and the watch keeps retrying.
    # 3. Once approved, HA sends the watch its own command key (MSG_KEY) over
    #    the encrypted link; every command is signed with it.
    # CMD/EVT need an LE Secure Connections encrypted link (secure-write /
    # secure-notify), so only bonded devices get this far.

    async def _save(self) -> None:
        await self._store.async_save({"approved": self._watches, "pending": self._pending})

    @property
    def watches(self) -> dict[str, dict]:
        return dict(self._watches)

    @property
    def pending(self) -> dict[str, dict]:
        return dict(self._pending)

    async def bonded(self, device: str, passkey: int) -> None:
        """A watch completed pairing with this code; it now awaits approval.

        A new bond for an already approved address drops that approval: the
        address alone proves nothing, only the bond it was approved with does.
        """
        addr = _address(device)
        if self._watches.pop(addr, None) is not None:
            _LOGGER.warning("approved watch %s paired again; approval required again", addr)
        self._pending[addr] = {"code": f"{passkey:06d}", "at": int(time.time())}
        self._flows_started.discard(addr)
        await self._save()
        self._ask_approval(addr)

    def is_ignored(self, device: str) -> bool:
        """The user chose Ignore on this watch's approval card (HA keeps an
        ignored entry for it, removable under Devices & services)."""
        uid = f"watch_{_address(device)}"
        return any(e.unique_id == uid and e.source == config_entries.SOURCE_IGNORE
                   for e in self.hass.config_entries.async_entries(DOMAIN))

    async def drop_pending(self, addr: str) -> None:
        """Remove a watch waiting for approval and its bond."""
        if self._pending.pop(addr, None) is None:
            return
        self._flows_started.discard(addr)
        self._abort_flows(addr)
        await self.periph.remove_device(_path(self.periph.adapter_path, addr))
        await self._save()
        _LOGGER.info("watch %s not approved; bond removed", addr)

    async def _expire_pending(self, _now=None) -> None:
        cutoff = time.time() - PENDING_TTL
        for addr in [a for a, info in self._pending.items() if info.get("at", 0) < cutoff]:
            await self.drop_pending(addr)

    @callback
    def _abort_flows(self, addr: str) -> None:
        """Close the approval card for this watch, if one is open."""
        flows = getattr(self.hass.config_entries, "flow", None)
        if flows is None:
            return
        for flow in flows.async_progress_by_handler(DOMAIN):
            if flow["context"].get("unique_id") == f"watch_{addr}":
                flows.async_abort(flow["flow_id"])

    @callback
    def _ask_approval(self, addr: str) -> None:
        if addr in self._flows_started:
            return
        self._flows_started.add(addr)
        discovery_flow.async_create_flow(
            self.hass, DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={"entry_id": self._entry_id, "address": addr, "code": self._pending[addr]["code"]},
        )

    async def approve(self, addr: str) -> bool:
        info = self._pending.pop(addr, None)
        if info is None:
            return False
        self._watches[addr] = {**info, "key": os.urandom(16).hex()}
        self._flows_started.discard(addr)
        await self._save()
        _LOGGER.info("watch %s approved", addr)
        return True

    async def forget(self, addrs: list[str]) -> None:
        """Drop watches (approved or pending) and their BlueZ bonds."""
        for addr in addrs:
            self._watches.pop(addr, None)
            self._pending.pop(addr, None)
            self._flows_started.discard(addr)
            self._abort_flows(addr)
            await self.periph.remove_device(_path(self.periph.adapter_path, addr))
        await self._save()

    def is_approved(self, device: str) -> bool:
        return _address(device) in self._watches

    # ---- exposure --------------------------------------------------------
    def _refresh_entities(self) -> None:
        labels = lr.async_get(self.hass)
        label = labels.async_get_label_by_name(self._label) or labels.async_get_label(self._label)
        ids: list[str] = []
        if label is not None:
            ids = sorted(e.entity_id for e in er.async_entries_for_label(er.async_get(self.hass), label.label_id))
        if ids != self._entities:
            self._entities = ids
            if self._unsub_state:
                self._unsub_state()
            self._unsub_state = async_track_state_change_event(self.hass, ids, self._on_state) if ids else None
        _LOGGER.debug("exposed entities: %s", ids)

    @property
    def exposed(self) -> list[str]:
        return list(self._entities)

    def _entity_msg(self, idx: int) -> bytes:
        eid = self._entities[idx]
        st = self.hass.states.get(eid)
        if st is None:
            return p.encode_entity(idx, eid, "unavailable", {})
        return p.encode_entity(idx, eid, st.state, dict(st.attributes))

    def _send(self, msg: bytes) -> None:
        self.periph.notify(self._frag.split(msg))

    # ---- events ----------------------------------------------------------
    @callback
    def _on_device(self, device: str, connected: bool) -> None:
        if connected:
            self._conns[device] = _Conn()
            _LOGGER.debug("central connected: %s", device)
        else:
            self._conns.pop(device, None)
            _LOGGER.debug("central disconnected: %s", device)

    @callback
    def _on_state(self, event: Event) -> None:
        if not any(c.session for c in self._conns.values()):
            return
        eid = event.data["entity_id"]
        if eid in self._entities:
            self._send(self._entity_msg(self._entities.index(eid)))

    @callback
    def _check_idle(self, _now) -> None:
        now = time.monotonic()
        for dev, conn in list(self._conns.items()):
            if now - conn.last_seen > self._idle:
                _LOGGER.debug("dropping idle central %s", dev)
                self._conns.pop(dev, None)
                self.hass.async_create_task(self.periph.disconnect(dev))

    async def _on_write(self, device: str, frame: bytes) -> None:
        conn = self._conns.setdefault(device, _Conn())
        if not frame:
            return
        if frame[0] == p.OP_HELLO:
            addr = _address(device)
            if len(frame) < 3 or frame[1] != p.PROTOCOL_VERSION:
                self._send(p.encode_result(0, p.ST_BAD_FRAME))
                return
            if addr not in self._watches:
                if addr in self._pending:
                    conn.last_seen = time.monotonic()  # the watch waits connected
                    self._send(p.encode_result(0, p.ST_NOT_APPROVED))
                    self._ask_approval(addr)
                else:
                    _LOGGER.warning("HELLO from %s refused: not paired", device)
                    self._send(p.encode_result(0, p.ST_NOT_PAIRED))
                return
            conn.key = bytes.fromhex(self._watches[addr]["key"])
            if not frame[2] & p.HELLO_HAS_KEY:
                self._send(p.encode_key(conn.key))
            self._refresh_entities()
            conn.session = p.Session(os.urandom(8))
            conn.last_seen = time.monotonic()
            self._send(p.encode_challenge(conn.session.nonce, len(self._entities)))
            return
        if conn.session is None:
            self._send(p.encode_result(frame[1] if len(frame) > 1 else 0, p.ST_NO_SESSION))
            return
        try:
            op, ctr, payload = conn.session.verify(conn.key, frame)
        except p.ProtocolError as err:
            _LOGGER.warning("rejected frame from %s: %s", device, err)
            self._send(p.encode_result(frame[1] if len(frame) > 1 else 0, err.status))
            return
        conn.last_seen = time.monotonic()
        try:
            await self._dispatch(device, op, ctr, payload)
        except p.ProtocolError as err:
            self._send(p.encode_result(ctr, err.status))

    async def _dispatch(self, device: str, op: int, ctr: int, payload: bytes) -> None:
        if op == p.OP_LIST:
            for i in range(len(self._entities)):
                self._send(self._entity_msg(i))
            self._send(p.encode_list_end(len(self._entities)))
        elif op == p.OP_GET:
            self._send(self._entity_msg(self._index(payload)))
        elif op == p.OP_ACTION:
            idx, action, arg = p.parse_action_payload(payload)
            eid = self._entities[self._index(bytes([idx]))]
            status = await self._run_action(eid, action, arg)
            self._send(p.encode_result(ctr, status))
        elif op == p.OP_BATTERY:
            if len(payload) < 1:
                raise p.ProtocolError(p.ST_BAD_FRAME, "short BATTERY")
            async_dispatcher_send(self.hass, SIGNAL_BATTERY.format(self._entry_id),
                                  payload[0], bool(payload[1]) if len(payload) > 1 else None)
            self._send(p.encode_result(ctr, p.ST_OK))
        elif op == p.OP_PING:
            pass  # keep-alive: verifying it already refreshed last_seen
        elif op == p.OP_BYE:
            self._conns.pop(device, None)
            await self.periph.disconnect(device)
        else:
            raise p.ProtocolError(p.ST_BAD_FRAME, f"unknown op {op}")

    def _index(self, payload: bytes) -> int:
        if not payload or payload[0] >= len(self._entities):
            raise p.ProtocolError(p.ST_BAD_INDEX, "bad index")
        return payload[0]

    async def _run_action(self, eid: str, action: int, arg: bytes) -> int:
        domain = eid.split(".", 1)[0]
        service = _SERVICES.get((domain, action))
        if service is None:
            return p.ST_NOT_ALLOWED
        data: dict = {"entity_id": eid}
        if action == p.ACT_SELECT_OPTION:
            st = self.hass.states.get(eid)
            opts = list(st.attributes.get("options", [])) if st else []
            if not arg or arg[0] >= len(opts):
                return p.ST_BAD_INDEX
            data["option"] = opts[arg[0]]
        elif action == p.ACT_SET_VALUE:
            data["value"] = p.unpack_float(arg)
        t0 = time.monotonic()
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s.%s on %s failed: %s", domain, service, eid, err)
            return p.ST_SERVICE_ERROR
        _LOGGER.debug("%s.%s on %s took %.0f ms", domain, service, eid, (time.monotonic() - t0) * 1000)
        return p.ST_OK


def _address(device_path: str) -> str:
    return device_path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()


def _path(adapter_path: str, address: str) -> str:
    return f"{adapter_path}/dev_{address.replace(':', '_').upper()}"


class _Pairing(PairingHandler):
    """BlueZ agent policy (see GarminBleServer pairing section)."""

    def __init__(self, server: GarminBleServer) -> None:
        self._s = server

    async def confirm(self, device: str, passkey: int | None, kind: str) -> bool:
        if passkey is None:
            # Just Works: no code, so nothing to show for approval
            _LOGGER.warning("pairing request from %s refused: %s has no code", device, kind)
            return False
        if self._s.is_ignored(device):
            _LOGGER.warning("pairing request from %s refused: ignored in HA", device)
            return False
        await self._s.bonded(device, passkey)
        _LOGGER.info("bonded with %s (code %06d), awaiting approval", device, passkey)
        return True

    def display(self, device: str, passkey: int) -> None:
        # Passkey Entry with HA displaying (not offered by a Fenix 7): logged only
        _LOGGER.info("pairing passkey for %s: %06d", device, passkey)

    def cancel(self) -> None:
        _LOGGER.info("pairing cancelled by BlueZ")
