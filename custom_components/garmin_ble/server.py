"""Protocol server: sessions, allowlist, dispatch to Home Assistant services."""
from __future__ import annotations

import asyncio
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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from . import protocol as p
from .const import (
    DOMAIN,
    PAIRING_CONFIRM_SECONDS,
    PAIRING_MODE_SECONDS,
    SIGNAL_BATTERY,
    SIGNAL_PAIRING,
)
from .peripheral import BlePeripheral, PairingHandler

_LOGGER = logging.getLogger(__name__)

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
    last_seen: float = field(default_factory=time.monotonic)


class GarminBleServer:
    def __init__(self, hass: HomeAssistant, entry_id: str, key: bytes, label: str,
                 adapter: str, idle_timeout: int) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._key = key
        self._label = label
        self._idle = idle_timeout
        self._conns: dict[str, _Conn] = {}
        self._entities: list[str] = []
        self._frag = p.Fragmenter()
        self._unsubs: list = []
        self._unsub_state = None
        self._pair_until = 0.0
        self._pending: tuple[str, int, asyncio.Future] | None = None
        # approved watches: BLE address -> {"code": pairing code, "at": epoch s}
        self._watches: dict[str, dict] = {}
        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.watches")
        self.periph = BlePeripheral(adapter, self._on_write, self._on_device, _Pairing(self))

    # ---- lifecycle -------------------------------------------------------
    async def async_start(self) -> None:
        self._watches = (await self._store.async_load()) or {}
        self._refresh_entities()
        await self.periph.start()
        self._unsubs.append(async_track_time_interval(
            self.hass, self._check_idle, timedelta(seconds=5)))

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        if self._unsub_state:
            self._unsub_state()
        for dev in list(self._conns):
            await self.periph.disconnect(dev)
        await self.periph.stop()

    # ---- pairing -----------------------------------------------------------
    # Only an approved watch may use the service: it must have completed LE
    # Secure Connections pairing (numeric comparison) while pairing mode was
    # on AND the user must have confirmed the 6-digit code in HA. BlueZ keeps
    # the bond; the GATT characteristics require an authenticated, encrypted
    # link (see peripheral.py), and HELLO checks the address against the
    # approved list, so another bonded device (a speaker, a phone) is refused
    # too.

    def start_pairing_mode(self, seconds: int = PAIRING_MODE_SECONDS) -> None:
        self._pair_until = time.monotonic() + seconds if seconds > 0 else 0.0
        _LOGGER.info("pairing mode %s", f"on for {seconds}s" if seconds > 0 else "off")
        self._signal_pairing()

    @property
    def pairing_mode(self) -> bool:
        return time.monotonic() < self._pair_until

    @property
    def pending_code(self) -> str | None:
        return f"{self._pending[1]:06d}" if self._pending else None

    @property
    def watches(self) -> dict[str, dict]:
        return dict(self._watches)

    def _signal_pairing(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_PAIRING.format(self._entry_id))

    def reject_pending(self) -> None:
        if self._pending is not None and not self._pending[2].done():
            self._pending[2].set_result(False)

    def confirm_pending(self) -> bool:
        """The user confirmed in HA that the watch shows the same code."""
        if self._pending is None or self._pending[2].done():
            return False
        self._pending[2].set_result(True)
        return True

    async def request_confirmation(self, device: str, passkey: int) -> bool:
        if not self.pairing_mode:
            _LOGGER.warning("pairing request from %s refused: pairing mode is off", device)
            return False
        if self._pending is not None and not self._pending[2].done():
            _LOGGER.warning("pairing request from %s refused: another one is pending", device)
            return False
        fut: asyncio.Future = self.hass.loop.create_future()
        self._pending = (device, passkey, fut)
        self._signal_pairing()
        try:
            ok = await asyncio.wait_for(fut, PAIRING_CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            ok = False
            _LOGGER.warning("pairing request from %s: not confirmed in HA within %ss",
                            device, PAIRING_CONFIRM_SECONDS)
        finally:
            self._pending = None
            self._signal_pairing()
        if ok:
            addr = _address(device)
            self._watches[addr] = {"code": f"{passkey:06d}", "at": int(time.time())}
            await self._store.async_save(self._watches)
            self._pair_until = 0.0
            _LOGGER.info("watch %s approved", addr)
            self._signal_pairing()
        return ok

    async def forget_watches(self) -> None:
        """Drop every approved watch and its BlueZ bond."""
        for addr in list(self._watches):
            await self.periph.remove_device(_path(self.periph.adapter_path, addr))
        self._watches = {}
        await self._store.async_save(self._watches)
        self._signal_pairing()

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
            if not self.is_approved(device):
                _LOGGER.warning("HELLO from %s refused: not an approved watch", device)
                self._send(p.encode_result(0, p.ST_NOT_PAIRED))
                return
            self._refresh_entities()
            conn.session = p.Session(os.urandom(8))
            conn.last_seen = time.monotonic()
            self._send(p.encode_challenge(conn.session.nonce, len(self._entities)))
            return
        if conn.session is None:
            self._send(p.encode_result(frame[1] if len(frame) > 1 else 0, p.ST_NO_SESSION))
            return
        try:
            op, ctr, payload = conn.session.verify(self._key, frame)
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
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s.%s on %s failed: %s", domain, service, eid, err)
            return p.ST_SERVICE_ERROR
        return p.ST_OK


def _address(device_path: str) -> str:
    return device_path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()


def _path(adapter_path: str, address: str) -> str:
    return f"{adapter_path}/dev_{address.replace(':', '_').upper()}"


class _Pairing(PairingHandler):
    """BlueZ agent policy (see GarminBleServer pairing section)."""

    def __init__(self, server: GarminBleServer) -> None:
        self._s = server

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self._s.hass, message, title="Garmin watch pairing", notification_id=f"{DOMAIN}_pairing")

    async def confirm(self, device: str, passkey: int | None, kind: str) -> bool:
        if passkey is None:
            # Just Works cannot be verified by anyone: never accepted
            _LOGGER.warning("pairing request from %s refused: %s has no code to compare", device, kind)
            return False
        code = f"{passkey:06d}"
        if self._s.pairing_mode:
            self._notify(f"A watch wants to pair. Code: **{code}**\n\nIf the watch shows the same code, "
                         f"press **Confirm watch pairing** within {PAIRING_CONFIRM_SECONDS} s, "
                         "then confirm on the watch.")
        ok = await self._s.request_confirmation(device, passkey)
        self._notify(f"Watch {_address(device)} paired (code {code})." if ok
                     else f"Pairing request from {_address(device)} (code {code}) was refused.")
        return ok

    def display(self, device: str, passkey: int) -> None:
        # Passkey Entry with HA displaying: the watch would type the code. Not
        # offered by a Fenix 7 (it asks for numeric comparison); logged only.
        _LOGGER.info("pairing passkey for %s: %06d", device, passkey)

    def cancel(self) -> None:
        self._s.reject_pending()  # BlueZ gave up on this request
