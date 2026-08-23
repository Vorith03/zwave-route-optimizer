"""Diagnostic status sensor for Z-Wave Route Optimizer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .optimizer import RouteOptimizer


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the optimizer status sensor."""
    optimizer: RouteOptimizer = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZWaveRouteOptimizerStatusSensor(entry, optimizer)])


class ZWaveRouteOptimizerStatusSensor(SensorEntity):
    """Expose live progress for long-running manual optimization actions."""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:routes"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, optimizer: RouteOptimizer) -> None:
        """Initialize the status entity."""
        self._optimizer = optimizer
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        """Return running/idle state."""
        return str(self._optimizer.status.get("state", "idle"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current progress details."""
        status = self._optimizer.status
        return {key: value for key, value in status.items() if key != "state"}

    async def async_added_to_hass(self) -> None:
        """Subscribe to optimizer progress updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self._optimizer.add_status_listener(self._status_updated))

    @callback
    def _status_updated(self) -> None:
        """Write a new state when optimizer progress changes."""
        self.async_write_ha_state()
