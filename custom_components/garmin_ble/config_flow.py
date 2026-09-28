"""Config flow: adapter and exposure label; approval of paired watches."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADAPTER,
    CONF_IDLE_TIMEOUT,
    CONF_LABEL,
    DEFAULT_ADAPTER,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_LABEL,
    DOMAIN,
)

CONF_FORGET = "forget"


class GarminBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._watch: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADAPTER])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Garmin BLE ({user_input[CONF_ADAPTER]})", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_LABEL, default=DEFAULT_LABEL): str,
                vol.Required(CONF_ADAPTER, default=DEFAULT_ADAPTER): str,
                vol.Required(CONF_IDLE_TIMEOUT, default=DEFAULT_IDLE_TIMEOUT): vol.All(int, vol.Range(min=5, max=600)),
            }),
        )

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """A watch paired over Bluetooth and asks for access (opened by the server)."""
        await self.async_set_unique_id(f"watch_{discovery_info['address']}")
        self._abort_if_unique_id_configured()
        self._watch = discovery_info
        self.context["title_placeholders"] = {"address": discovery_info["address"]}
        return await self.async_step_approve()

    async def async_step_approve(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            server = self.hass.data.get(DOMAIN, {}).get(self._watch["entry_id"])
            if server is None or not await server.approve(self._watch["address"]):
                return self.async_abort(reason="not_pending")
            return self.async_abort(reason="watch_approved")
        return self.async_show_form(
            step_id="approve",
            data_schema=vol.Schema({}),
            description_placeholders={"address": self._watch["address"], "code": self._watch["code"]},
        )

    async def async_step_ignore(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Ignore on an approval card: drop the watch and its bond instead of
        remembering the address as ignored, so it can pair again later."""
        uid = user_input["unique_id"]
        if uid.startswith("watch_"):
            for server in self.hass.data.get(DOMAIN, {}).values():
                await server.reject(uid[len("watch_"):])
            return self.async_abort(reason="watch_ignored")
        return await super().async_step_ignore(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return GarminBleOptionsFlow()


class GarminBleOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        server = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        known = sorted({**server.watches, **server.pending}) if server else []
        if user_input is not None:
            forget = user_input.pop(CONF_FORGET, [])
            if forget and server is not None:
                await server.forget(forget)
            return self.async_create_entry(data=user_input)
        cur = {**self.config_entry.data, **self.config_entry.options}
        schema = {
            vol.Required(CONF_LABEL, default=cur.get(CONF_LABEL, DEFAULT_LABEL)): str,
            vol.Required(CONF_IDLE_TIMEOUT, default=cur.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)):
                vol.All(int, vol.Range(min=5, max=600)),
        }
        if known:
            schema[vol.Optional(CONF_FORGET, default=[])] = cv.multi_select(
                {a: f"{a} ({'approved' if a in server.watches else 'waiting for approval'})" for a in known})
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
