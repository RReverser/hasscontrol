"""Pairing controls: pairing mode, code confirmation, forgetting watches."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    server = hass.data[DOMAIN][entry.entry_id]
    add([PairWatch(entry, server), ConfirmPairing(entry, server), ForgetWatches(entry, server)])


class _Base(ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _key = ""

    def __init__(self, entry: ConfigEntry, server) -> None:
        self._server = server
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"


class PairWatch(_Base):
    """Opens pairing mode: pairing requests are only considered while it is on."""

    _attr_name = "Pair watch"
    _attr_icon = "mdi:bluetooth-connect"
    _key = "pair_watch"

    async def async_press(self) -> None:
        self._server.start_pairing_mode()


class ConfirmPairing(_Base):
    """Confirms that the watch shows the code HA shows."""

    _attr_name = "Confirm watch pairing"
    _attr_icon = "mdi:check-decagram"
    _key = "confirm_pairing"

    async def async_press(self) -> None:
        self._server.confirm_pending()


class ForgetWatches(_Base):
    """Removes every approved watch and its Bluetooth bond."""

    _attr_name = "Forget paired watches"
    _attr_icon = "mdi:bluetooth-off"
    _key = "forget_watches"

    async def async_press(self) -> None:
        await self._server.forget_watches()
