"""Watch battery level reported over BLE."""
from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_BATTERY, SIGNAL_PAIRING


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([WatchBattery(entry), PairingStatus(entry, hass.data[DOMAIN][entry.entry_id])])


class WatchBattery(RestoreSensor):
    _attr_has_entity_name = True
    _attr_name = "Watch battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_watch_battery"
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            self._attr_native_value = last.native_value
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_BATTERY.format(self._entry_id), self._update))

    @callback
    def _update(self, percent: int, charging: bool | None) -> None:
        self._attr_native_value = percent
        if charging is not None:
            self._attr_extra_state_attributes = {"charging": charging}
        self.async_write_ha_state()


class PairingStatus(SensorEntity):
    """Pairing state: off, pairing mode, or the code waiting for confirmation."""

    _attr_has_entity_name = True
    _attr_name = "Watch pairing"
    _attr_icon = "mdi:bluetooth-settings"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, server) -> None:
        self._entry_id = entry.entry_id
        self._server = server
        self._attr_unique_id = f"{entry.entry_id}_pairing"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_PAIRING.format(self._entry_id), self._update))
        self._update()

    @callback
    def _update(self) -> None:
        code = self._server.pending_code
        if code is not None:
            self._attr_native_value = f"confirm {code}"
        elif self._server.pairing_mode:
            self._attr_native_value = "pairing mode"
        else:
            self._attr_native_value = "off"
        self._attr_extra_state_attributes = {"paired_watches": sorted(self._server.watches)}
        if self.hass is not None:
            self.async_write_ha_state()
