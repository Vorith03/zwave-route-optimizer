"""Apply-last-optimization button for Z-Wave Route Optimizer."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .optimizer_v080 import RouteOptimizer


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the staged-apply button."""
    optimizer: RouteOptimizer = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZWaveRouteOptimizerApplyButton(entry, optimizer)])


class ZWaveRouteOptimizerApplyButton(ButtonEntity):
    """Apply the most recent staged whole-network optimization plan."""

    _attr_has_entity_name = True
    _attr_translation_key = "apply_last_optimization"
    _attr_icon = "mdi:check-network-outline"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, optimizer: RouteOptimizer) -> None:
        self._optimizer = optimizer
        self._attr_unique_id = f"{entry.entry_id}_apply_last_optimization"

    @property
    def available(self) -> bool:
        """Only enable the button while a valid write-ready staged plan exists."""
        return self._optimizer.can_apply_pending_plan

    async def async_press(self) -> None:
        """Apply the staged plan."""
        await self._optimizer.apply_last_network_optimization()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._optimizer.add_status_listener(self._status_updated))

    @callback
    def _status_updated(self) -> None:
        self.async_write_ha_state()
