"""BlueZ GATT peripheral (service + advertisement) over D-Bus, via dbus-fast.

No Home Assistant imports: callers pass plain callbacks. Verified on HA OS
(BlueZ, Intel USB controller) that the HA core container may register GATT
applications and advertisements on the host's adapter.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

_LOGGER = logging.getLogger(__name__)

SVC_UUID = "6a1e0001-4c7d-4b4e-9a2b-3c8f1d2e5a01"
CMD_UUID = "6a1e0002-4c7d-4b4e-9a2b-3c8f1d2e5a01"
EVT_UUID = "6a1e0003-4c7d-4b4e-9a2b-3c8f1d2e5a01"
LOCAL_NAME = "HA-Watch"
ADV_MIN_INTERVAL_MS = 60
ADV_MAX_INTERVAL_MS = 100

APP_PATH = "/io/hasscontrol/garmin_ble"
ADV_PATH = "/io/hasscontrol_adv/adv0"  # outside APP_PATH: dbus-fast's ObjectManager lists all children

WriteCb = Callable[[str, bytes], Awaitable[None]]
DeviceCb = Callable[[str, bool], None]


class _Service(ServiceInterface):
    def __init__(self, path: str, chars: list[str]) -> None:
        super().__init__("org.bluez.GattService1")
        self.path = path
        self._chars = chars

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return SVC_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":  # noqa: N802
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Characteristics(self) -> "ao":  # noqa: N802
        return self._chars


class _Characteristic(ServiceInterface):
    def __init__(self, path: str, uuid: str, service: str, flags: list[str]) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self.path = path
        self._uuid = uuid
        self._service = service
        self._flags = flags
        self._value = b""
        self.notifying = False
        self.on_write: Callable[[bytes, dict], None] | None = None

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: N802
        return self._service

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: N802
        return self._value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: N802
        return self._value

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: N802
        if self.on_write is not None:
            self.on_write(bytes(value), options)

    @method()
    def StartNotify(self):  # noqa: N802
        self.notifying = True

    @method()
    def StopNotify(self):  # noqa: N802
        self.notifying = False

    def push(self, value: bytes) -> None:
        self._value = value
        if self.notifying:
            self.emit_properties_changed({"Value": value})


class _Advertisement(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.bluez.LEAdvertisement1")

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: N802
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":  # noqa: N802
        return [SVC_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":  # noqa: N802
        return LOCAL_NAME

    # Advertising interval in ms. The kernel default is 1.28 s, against which
    # a Connect IQ central (simulator + nRF52 dongle) needed ~9-11 s per
    # connection and often timed out; at 60-100 ms, 11 of 11 probe runs
    # connected, 2-7 s (median 3 s) after the watch saw the advert. HCI traces on
    # HA OS confirmed BlueZ applies these values (LE Set Adv Params 0x60/0xa0).
    @dbus_property(access=PropertyAccess.READ)
    def MinInterval(self) -> "u":  # noqa: N802
        return ADV_MIN_INTERVAL_MS

    @dbus_property(access=PropertyAccess.READ)
    def MaxInterval(self) -> "u":  # noqa: N802
        return ADV_MAX_INTERVAL_MS

    @method()
    def Release(self):  # noqa: N802
        _LOGGER.debug("advertisement released by BlueZ")


class BlePeripheral:
    """Owns one D-Bus connection, the GATT objects and their registration."""

    def __init__(self, adapter: str, on_write: WriteCb, on_device: DeviceCb) -> None:
        self._adapter_path = f"/org/bluez/{adapter}"
        self._on_write = on_write
        self._on_device = on_device
        self._bus: MessageBus | None = None
        self._registered = False
        self._tasks: set[asyncio.Task] = set()
        svc_path = APP_PATH + "/service0"
        self._cmd = _Characteristic(svc_path + "/char0", CMD_UUID, svc_path, ["write"])
        self._evt = _Characteristic(svc_path + "/char1", EVT_UUID, svc_path, ["notify"])
        self._svc = _Service(svc_path, [self._cmd.path, self._evt.path])
        self._adv = _Advertisement()
        self._cmd.on_write = self._handle_write

    @property
    def registered(self) -> bool:
        return self._registered

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _handle_write(self, value: bytes, options: dict) -> None:
        dev = options.get("device")
        device = dev.value if isinstance(dev, Variant) else str(dev or "")
        self._spawn(self._on_write(device, value))

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        for obj in (self._svc, self._cmd, self._evt):
            self._bus.export(obj.path, obj)
        self._bus.export(ADV_PATH, self._adv)
        self._bus.add_message_handler(self._on_signal)
        for rule in (
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesAdded'",
            "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'",
        ):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                             "AddMatch", "s", [rule])
        await self.register()

    async def _call(self, dest, path, iface, member, sig="", body=None):
        assert self._bus is not None
        reply = await self._bus.call(Message(destination=dest, path=path, interface=iface,
                                             member=member, signature=sig, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}: {reply.body}")
        return reply

    async def register(self) -> None:
        """Register GATT app + advertisement with BlueZ (idempotent-ish)."""
        self._registered = False
        await self._call("org.bluez", self._adapter_path, "org.bluez.GattManager1",
                         "RegisterApplication", "oa{sv}", [APP_PATH, {}])
        await self._call("org.bluez", self._adapter_path, "org.bluez.LEAdvertisingManager1",
                         "RegisterAdvertisement", "oa{sv}", [ADV_PATH, {}])
        self._registered = True
        _LOGGER.info("GATT service and advertisement registered on %s", self._adapter_path)

    async def _reregister_soon(self) -> None:
        try:
            await self.register()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("re-registration failed: %s", err)

    def _on_signal(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "PropertiesChanged" and msg.path.startswith(self._adapter_path + "/dev_"):
            iface, changed = msg.body[0], msg.body[1]
            if iface == "org.bluez.Device1" and "Connected" in changed:
                self._on_device(msg.path, bool(changed["Connected"].value))
        elif msg.member == "InterfacesAdded" and msg.body and msg.body[0] == self._adapter_path:
            # adapter re-appeared (reset/replug): registrations are gone
            if "org.bluez.GattManager1" in msg.body[1]:
                self._spawn(self._reregister_soon())
        elif msg.member == "NameOwnerChanged" and msg.body[0] == "org.bluez" and msg.body[2]:
            # bluetoothd restarted
            self._spawn(self._reregister_soon())

    def notify(self, frames: list[bytes]) -> None:
        for f in frames:
            self._evt.push(f)

    async def disconnect(self, device_path: str) -> None:
        try:
            await self._call("org.bluez", device_path, "org.bluez.Device1", "Disconnect")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("disconnect %s failed: %s", device_path, err)

    async def stop(self) -> None:
        if self._bus is None:
            return
        for iface, member, path in (("org.bluez.LEAdvertisingManager1", "UnregisterAdvertisement", ADV_PATH),
                                    ("org.bluez.GattManager1", "UnregisterApplication", APP_PATH)):
            try:
                await self._call("org.bluez", self._adapter_path, iface, member, "o", [path])
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s failed: %s", member, err)
        self._bus.disconnect()
        self._bus = None
        self._registered = False
