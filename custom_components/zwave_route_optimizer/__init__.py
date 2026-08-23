"""Z-Wave Route Optimizer integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.components.zwave_js.helpers import async_get_node_from_device_id

from .const import (
    CONF_ZWAVE_ENTRY_ID,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_REPEATERS,
    DEFAULT_MIN_IMPROVEMENT,
    DEFAULT_PASSES,
    DEFAULT_ROUNDS,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_WARMUP,
    DOMAIN,
    SERVICE_OPTIMIZE_NETWORK,
    SERVICE_OPTIMIZE_NODE,
)
from .optimizer import RouteOptimizer

_LOGGER = logging.getLogger(__name__)

ATTR_DEVICE_ID = "device_id"
ATTR_APPLY = "apply"
ATTR_ROUNDS = "rounds"
ATTR_PASSES = "passes"
ATTR_WARMUP = "warmup"
ATTR_MAX_REPEATERS = "max_repeaters"
ATTR_MAX_CANDIDATES = "max_candidates"
ATTR_MIN_IMPROVEMENT = "min_improvement"
ATTR_SETTLE_SECONDS = "settle_seconds"
ATTR_INCLUDE_AUTO = "include_auto"
ATTR_REFRESH_NEIGHBORS = "refresh_neighbors"
ATTR_APPLY_RETURN_ROUTE = "apply_return_route"
ATTR_ALLOW_UNVALIDATED_RETURN_ROUTE = "allow_unvalidated_return_route"

PLATFORMS = [Platform.SENSOR]

COMMON_SCHEMA = {
    vol.Optional(ATTR_APPLY, default=False): cv.boolean,
    vol.Optional(ATTR_ROUNDS, default=DEFAULT_ROUNDS): vol.All(
        vol.Coerce(int), vol.Range(min=2, max=20)
    ),
    vol.Optional(ATTR_WARMUP, default=DEFAULT_WARMUP): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=5)
    ),
    vol.Optional(ATTR_MAX_REPEATERS, default=DEFAULT_MAX_REPEATERS): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=4)
    ),
    vol.Optional(ATTR_MAX_CANDIDATES, default=DEFAULT_MAX_CANDIDATES): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=60)
    ),
    vol.Optional(ATTR_MIN_IMPROVEMENT, default=DEFAULT_MIN_IMPROVEMENT): vol.All(
        vol.Coerce(float), vol.Range(min=0, max=100)
    ),
    vol.Optional(ATTR_SETTLE_SECONDS, default=DEFAULT_SETTLE_SECONDS): vol.All(
        vol.Coerce(float), vol.Range(min=0, max=5)
    ),
    vol.Optional(ATTR_INCLUDE_AUTO, default=True): cv.boolean,
    vol.Optional(ATTR_REFRESH_NEIGHBORS, default=False): cv.boolean,
    vol.Optional(ATTR_APPLY_RETURN_ROUTE, default=False): cv.boolean,
    vol.Optional(ATTR_ALLOW_UNVALIDATED_RETURN_ROUTE, default=False): cv.boolean,
}

NODE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): cv.string, **COMMON_SCHEMA},
    extra=vol.PREVENT_EXTRA,
)
NETWORK_SCHEMA = vol.Schema(
    {
        **COMMON_SCHEMA,
        vol.Optional(ATTR_PASSES, default=DEFAULT_PASSES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
    },
    extra=vol.PREVENT_EXTRA,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Z-Wave Route Optimizer config entry."""
    hass.data.setdefault(DOMAIN, {})
    optimizer = RouteOptimizer(hass, entry.data[CONF_ZWAVE_ENTRY_ID])
    hass.data[DOMAIN][entry.entry_id] = optimizer

    # Config flow enforces one integration entry, so one pair of global
    # actions maps unambiguously to one Z-Wave network.
    if not hass.services.has_service(DOMAIN, SERVICE_OPTIMIZE_NODE):

        async def _optimize_node(call: ServiceCall) -> dict[str, Any]:
            active = _get_optimizer(hass)
            try:
                node = async_get_node_from_device_id(
                    hass, call.data[ATTR_DEVICE_ID]
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

            try:
                return await active.optimize_node(
                    node,
                    **_options_from_call(call),
                )
            except HomeAssistantError:
                raise
            except Exception as err:
                _LOGGER.exception("Unexpected single-node optimization failure")
                raise HomeAssistantError(str(err)) from err

        async def _optimize_network(call: ServiceCall) -> dict[str, Any]:
            active = _get_optimizer(hass)
            try:
                return await active.optimize_network(
                    passes=call.data[ATTR_PASSES],
                    **_options_from_call(call),
                )
            except HomeAssistantError:
                raise
            except Exception as err:
                _LOGGER.exception("Unexpected network optimization failure")
                raise HomeAssistantError(str(err)) from err

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_OPTIMIZE_NODE,
            _optimize_node,
            schema=NODE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_OPTIMIZE_NETWORK,
            _optimize_network,
            schema=NETWORK_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Z-Wave Route Optimizer."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_OPTIMIZE_NODE)
        hass.services.async_remove(DOMAIN, SERVICE_OPTIMIZE_NETWORK)
        hass.data.pop(DOMAIN, None)

    return True


def _get_optimizer(hass: HomeAssistant) -> RouteOptimizer:
    """Get the configured optimizer."""
    configured = hass.data.get(DOMAIN, {})
    if len(configured) != 1:
        raise HomeAssistantError(
            "Z-Wave Route Optimizer is not configured or is in an ambiguous state."
        )
    return next(iter(configured.values()))


def _options_from_call(call: ServiceCall) -> dict[str, Any]:
    """Extract common optimizer options."""
    data = call.data
    return {
        "apply": data[ATTR_APPLY],
        "rounds": data[ATTR_ROUNDS],
        "warmup": data[ATTR_WARMUP],
        "max_repeaters": data[ATTR_MAX_REPEATERS],
        "max_candidates": data[ATTR_MAX_CANDIDATES],
        "min_improvement": data[ATTR_MIN_IMPROVEMENT],
        "settle_seconds": data[ATTR_SETTLE_SECONDS],
        "include_auto": data[ATTR_INCLUDE_AUTO],
        "refresh_neighbors": data[ATTR_REFRESH_NEIGHBORS],
        "apply_return_route": data[ATTR_APPLY_RETURN_ROUTE],
        "allow_unvalidated_return_route": data[ATTR_ALLOW_UNVALIDATED_RETURN_ROUTE],
    }
