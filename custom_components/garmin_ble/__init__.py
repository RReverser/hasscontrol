"""Garmin watch control over direct BLE (HassControl BLE fork).

Home Assistant acts as a BLE GATT peripheral on the host's Bluetooth adapter;
the HassControl Connect IQ app connects to it directly. See PROTOCOL.md.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_ADAPTER,
    CONF_IDLE_TIMEOUT,
    CONF_LABEL,
    CONF_SECRET,
    DEFAULT_ADAPTER,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_LABEL,
    DOMAIN,
)
from .server import GarminBleServer

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
SERVICE_ALLOW_PAIRING = "allow_pairing"


def _opt(entry: ConfigEntry, key: str, default):
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    server = GarminBleServer(
        hass,
        entry.entry_id,
        key=bytes.fromhex(entry.data[CONF_SECRET]),
        label=_opt(entry, CONF_LABEL, DEFAULT_LABEL),
        adapter=_opt(entry, CONF_ADAPTER, DEFAULT_ADAPTER),
        idle_timeout=int(_opt(entry, CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)),
    )
    try:
        await server.async_start()
    except Exception as err:  # noqa: BLE001
        await server.async_stop()
        raise ConfigEntryNotReady(f"BlueZ registration failed: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = server
    if not hass.services.has_service(DOMAIN, SERVICE_ALLOW_PAIRING):
        async def _allow_pairing(call: ServiceCall) -> None:
            for srv in hass.data.get(DOMAIN, {}).values():
                srv.allow_pairing(call.data["seconds"])

        hass.services.async_register(DOMAIN, SERVICE_ALLOW_PAIRING, _allow_pairing, schema=vol.Schema(
            {vol.Optional("seconds", default=120): vol.All(vol.Coerce(int), vol.Range(min=0, max=600))}))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    server: GarminBleServer | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if server is not None:
        await server.async_stop()
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_ALLOW_PAIRING)
    return ok
