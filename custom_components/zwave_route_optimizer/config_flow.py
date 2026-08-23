"""Config flow for Z-Wave Route Optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from .const import CONF_ZWAVE_ENTRY_ID, DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure Z-Wave Route Optimizer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the optimizer against an existing Z-Wave integration."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        zwave_entries = [
            entry
            for entry in self.hass.config_entries.async_entries("zwave_js")
            if not entry.disabled_by
        ]
        if not zwave_entries:
            return self.async_abort(reason="no_zwave")

        if user_input is not None:
            source_id = user_input[CONF_ZWAVE_ENTRY_ID]
            source = self.hass.config_entries.async_get_entry(source_id)
            if source is None or source.domain != "zwave_js":
                return self.async_abort(reason="no_zwave")
            await self.async_set_unique_id(source_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Z-Wave Route Optimizer — {source.title}",
                data={CONF_ZWAVE_ENTRY_ID: source_id},
            )

        if len(zwave_entries) == 1:
            source = zwave_entries[0]
            await self.async_set_unique_id(source.entry_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Z-Wave Route Optimizer — {source.title}",
                data={CONF_ZWAVE_ENTRY_ID: source.entry_id},
            )

        choices = {entry.entry_id: entry.title for entry in zwave_entries}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ZWAVE_ENTRY_ID): vol.In(choices)}
            ),
        )
