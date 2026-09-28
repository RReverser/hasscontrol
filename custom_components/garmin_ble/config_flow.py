"""Config flow: pick the label that selects exposed entities; generate the watch secret."""
from __future__ import annotations

import re
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

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

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


class GarminBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            secret = (user_input.get(CONF_SECRET) or "").strip() or secrets.token_hex(16)
            if not _HEX32.match(secret):
                errors[CONF_SECRET] = "bad_secret"
            else:
                await self.async_set_unique_id(user_input[CONF_ADAPTER])
                self._abort_if_unique_id_configured()
                self._data = {**user_input, CONF_SECRET: secret.lower()}
                return await self.async_step_secret()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_LABEL, default=DEFAULT_LABEL): str,
                vol.Required(CONF_ADAPTER, default=DEFAULT_ADAPTER): str,
                vol.Required(CONF_IDLE_TIMEOUT, default=DEFAULT_IDLE_TIMEOUT): vol.All(int, vol.Range(min=5, max=600)),
                vol.Optional(CONF_SECRET, default=""): str,
            }),
            errors=errors,
        )

    async def async_step_secret(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the secret once so it can be copied into the watch app settings."""
        if user_input is not None:
            return self.async_create_entry(title=f"Garmin BLE ({self._data[CONF_ADAPTER]})", data=self._data)
        return self.async_show_form(
            step_id="secret",
            data_schema=vol.Schema({}),
            description_placeholders={"secret": self._data[CONF_SECRET]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return GarminBleOptionsFlow()


class GarminBleOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cur = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_LABEL, default=cur.get(CONF_LABEL, DEFAULT_LABEL)): str,
                vol.Required(CONF_IDLE_TIMEOUT, default=cur.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)):
                    vol.All(int, vol.Range(min=5, max=600)),
            }),
        )
