"""Manual Z-Wave source-route benchmarking and optimization."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import math
import statistics
import time
from typing import Any, Callable, Iterable

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ADAPTIVE_DIRECT_MEDIAN_MS,
    ADAPTIVE_DIRECT_WORST_MS,
    ADAPTIVE_ONE_HOP_MEDIAN_MS,
    ADAPTIVE_ONE_HOP_PATHS,
    ADAPTIVE_ONE_HOP_WORST_MS,
    BPS_TO_ROUTE_SPEED,
    DEFAULT_SAMPLE_INTERVAL,
    ROUTE_SPEED_TO_BPS,
    VALID_BPS,
)

_LOGGER = logging.getLogger(__name__)

# @zwave-js/core RouteKind values serialized over Z-Wave JS Server.
ROUTE_KIND_NONE = 0x00
ROUTE_KIND_LWR = 0x01
ROUTE_KIND_NLWR = 0x02
ROUTE_KIND_APPLICATION = 0x10


class RouteRestoreError(HomeAssistantError):
    """Raised when the optimizer cannot restore a node's starting pinned-route state."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A controller-to-node priority-route candidate."""

    repeaters: tuple[int, ...] | None
    speed: int | None
    label: str
    rf_score: float = 0.0
    rf_evidence: tuple[str, ...] = ()

    @property
    def is_auto(self) -> bool:
        """Return whether this candidate removes the application priority route."""
        return self.repeaters is None


@dataclass(frozen=True, slots=True)
class PriorityState:
    """Pinned application route plus the effective route observed at snapshot time."""

    application: Candidate | None
    effective_route: dict[str, Any] | None
    route_kind: Any

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe diagnostic data."""
        return {
            "application_priority_route": (
                None
                if self.application is None
                else {
                    "repeaters": list(self.application.repeaters or ()),
                    "route_speed": self.application.speed,
                    "route_speed_kbps": RouteOptimizer._route_speed_kbps(
                        self.application.speed
                    ),
                }
            ),
            "effective_route": self.effective_route,
            "route_kind": self.route_kind,
        }


@dataclass(slots=True)
class BenchResult:
    """Benchmark result for one route."""

    candidate: Candidate
    samples_ms: list[float]
    failures: int
    warmup_samples_ms: list[float]
    wake_failures: int = 0
    baseline_median_ms: float | None = None
    stopped_early: bool = False
    stop_reason: str | None = None
    planned_rounds: int = 0

    @property
    def median_ms(self) -> float:
        """Median scored route latency."""
        return (
            statistics.median(self.samples_ms)
            if self.samples_ms
            else float("inf")
        )

    @property
    def worst_ms(self) -> float:
        """Worst scored route latency."""
        return max(self.samples_ms) if self.samples_ms else float("inf")

    @property
    def slow_threshold_ms(self) -> float:
        """Threshold for an obviously abnormal retry/route-resolution sample."""
        baseline = self.baseline_median_ms
        if baseline is None or not math.isfinite(baseline):
            return 250.0
        return max(250.0, baseline * 4.0)

    @property
    def slow_samples(self) -> int:
        """Count transactions that are far outside the normal route latency."""
        if not self.samples_ms:
            return 0
        threshold = self.slow_threshold_ms
        return sum(value > threshold for value in self.samples_ms)

    @property
    def successes(self) -> int:
        """Successful samples."""
        return len(self.samples_ms) - self.failures

    @property
    def score(self) -> float:
        """Secondary scalar score; route selection is lexicographic."""
        hops = (
            0
            if self.candidate.repeaters is None
            else len(self.candidate.repeaters)
        )
        return (
            self.median_ms
            + 0.10 * self.worst_ms
            + 750.0 * self.slow_samples
            + 2000.0 * self.failures
            + 2000.0 * self.wake_failures
            + 5.0 * hops
        )

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe response data."""
        return {
            "route": self.candidate.label,
            "repeaters": (
                None
                if self.candidate.repeaters is None
                else list(self.candidate.repeaters)
            ),
            "route_speed": self.candidate.speed,
            "route_speed_kbps": RouteOptimizer._route_speed_kbps(
                self.candidate.speed
            ),
            "rf_score": round(self.candidate.rf_score, 1),
            "rf_evidence": list(self.candidate.rf_evidence),
            "warmup_samples_ms": [
                round(value, 1) for value in self.warmup_samples_ms
            ],
            "samples_ms": [round(value, 1) for value in self.samples_ms],
            "successes": self.successes,
            "failures": self.failures,
            "wake_failures": self.wake_failures,
            "baseline_median_ms": (
                None
                if self.baseline_median_ms is None
                else round(self.baseline_median_ms, 1)
            ),
            "slow_samples": self.slow_samples,
            "slow_threshold_ms": round(self.slow_threshold_ms, 1),
            "median_ms": round(self.median_ms, 1),
            "worst_ms": round(self.worst_ms, 1),
            "score": round(self.score, 1),
            "planned_rounds": self.planned_rounds,
            "completed_rounds": len(self.samples_ms),
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
        }


class RouteOptimizer:
    """One-shot optimizer which reuses Home Assistant's Z-Wave JS connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        zwave_entry_id: str,
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.zwave_entry_id = zwave_entry_id
        self._run_lock = asyncio.Lock()
        self._return_route_readback_supported: bool | None = None
        self._status_listeners: set[Callable[[], None]] = set()
        self._status_started_monotonic: float | None = None
        self._status: dict[str, Any] = {
            "state": "idle",
            "operation": None,
            "current_node_id": None,
            "current_node_name": None,
            "node_index": None,
            "node_total": None,
            "pass_index": None,
            "pass_total": None,
            "candidate_index": None,
            "candidate_total": None,
            "current_route": None,
            "completed_count": 0,
            "elapsed_seconds": 0.0,
            "latest_result": None,
        }

    @property
    def status(self) -> dict[str, Any]:
        """Return a snapshot of the live optimizer status."""
        data = dict(self._status)
        if self._status_started_monotonic is not None:
            data["elapsed_seconds"] = round(
                time.monotonic() - self._status_started_monotonic, 1
            )
        return data

    def add_status_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked when live optimizer status changes."""
        self._status_listeners.add(listener)

        def remove_listener() -> None:
            self._status_listeners.discard(listener)

        return remove_listener

    def _update_status(self, **changes: Any) -> None:
        """Update live status and notify entities without doing I/O."""
        self._status.update(changes)
        if self._status_started_monotonic is not None:
            self._status["elapsed_seconds"] = round(
                time.monotonic() - self._status_started_monotonic, 1
            )
        for listener in tuple(self._status_listeners):
            try:
                listener()
            except Exception:  # pragma: no cover - defensive UI notification guard.
                _LOGGER.exception("Z-Wave route optimizer status listener failed")

    def _start_status(self, operation: str, *, node_total: int | None = None) -> None:
        """Start a live-status run."""
        self._status_started_monotonic = time.monotonic()
        self._status = {
            "state": "running",
            "operation": operation,
            "current_node_id": None,
            "current_node_name": None,
            "node_index": None,
            "node_total": node_total,
            "pass_index": None,
            "pass_total": None,
            "candidate_index": None,
            "candidate_total": None,
            "current_route": None,
            "completed_count": 0,
            "elapsed_seconds": 0.0,
            "latest_result": None,
        }
        self._update_status()

    def _finish_status(self, *, latest_result: dict[str, Any] | None = None) -> None:
        """Mark the live-status run idle while preserving the last useful result."""
        elapsed = 0.0
        if self._status_started_monotonic is not None:
            elapsed = round(time.monotonic() - self._status_started_monotonic, 1)
        if latest_result is None:
            latest_result = self._status.get("latest_result")
        self._status_started_monotonic = None
        self._status.update(
            {
                "state": "idle",
                "current_node_id": None,
                "current_node_name": None,
                "node_index": None,
                "node_total": None,
                "pass_index": None,
                "pass_total": None,
                "candidate_index": None,
                "candidate_total": None,
                "current_route": None,
                "elapsed_seconds": elapsed,
                "latest_result": latest_result,
            }
        )
        self._update_status()

    def _get_client_driver_controller(self):
        """Return the currently loaded Z-Wave JS objects."""
        source = self.hass.config_entries.async_get_entry(self.zwave_entry_id)
        if source is None:
            raise HomeAssistantError("The configured Z-Wave integration no longer exists.")
        if source.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("The configured Z-Wave integration is not loaded.")

        runtime_data = getattr(source, "runtime_data", None)
        client = getattr(runtime_data, "client", None)
        if client is None or not client.connected or client.driver is None:
            raise HomeAssistantError("Z-Wave JS is not connected or its driver is not ready.")

        driver = client.driver
        return client, driver, driver.controller

    @staticmethod
    def _enum_name(value: Any) -> str:
        """Normalize an enum-like value to an uppercase name."""
        name = getattr(value, "name", None)
        if name:
            return str(name).upper()
        return str(value).split(".")[-1].upper()

    async def _ensure_network_safe_to_test(self, *, check_ota: bool) -> None:
        """Refuse to optimize while disruptive controller work is active."""
        _, _, controller = self._get_client_driver_controller()

        if controller.is_rebuilding_routes:
            raise HomeAssistantError(
                "Z-Wave routes are already being rebuilt; try again after that finishes."
            )

        inclusion_name = self._enum_name(controller.inclusion_state)
        if inclusion_name not in {"IDLE", "SMART_START", "SMARTSTART"}:
            raise HomeAssistantError(
                f"Z-Wave inclusion/exclusion is active ({inclusion_name}); "
                "route optimization was not started."
            )

        if check_ota:
            try:
                if await controller.async_is_any_ota_firmware_update_in_progress():
                    raise HomeAssistantError(
                        "A Z-Wave OTA firmware update is in progress; "
                        "route optimization was not started."
                    )
            except HomeAssistantError:
                raise
            except Exception as err:  # Older servers may not support this probe.
                _LOGGER.debug("Could not query OTA update state: %s", err)

    @staticmethod
    def _is_classic(node: Any) -> bool:
        """Return True for classic-mesh Z-Wave nodes."""
        raw = getattr(node, "protocol", None)
        if raw is None:
            raw = node.data.get("protocol")

        # Protocol was absent in older server schemas; those networks predate
        # Z-Wave Long Range support, so missing metadata is treated as classic.
        if raw is None:
            return True
        if isinstance(raw, int):
            return raw == 0

        name = getattr(raw, "name", None)
        normalized = str(name if name is not None else raw).upper()
        if "LONG" in normalized:
            return False
        return "ZWAVE" in normalized or "Z-WAVE" in normalized

    @staticmethod
    def _eligible_target(node: Any) -> bool:
        """Return whether a node can safely be actively benchmarked."""
        return RouteOptimizer._ineligibility_reason(node) is None

    @staticmethod
    def _ineligibility_reason(node: Any) -> str | None:
        """Return a human-readable reason a target is skipped."""
        if node.is_controller_node:
            return "controller node"
        if not RouteOptimizer._is_classic(node):
            return "Z-Wave Long Range"
        if RouteOptimizer._enum_name(node.status) == "DEAD":
            return "dead"
        if not node.ready:
            return "not ready / unavailable"
        if not (node.is_listening or node.is_frequent_listening):
            return "sleeping battery node / wake manually"
        return None

    @staticmethod
    def _can_repeat(node: Any) -> bool:
        """Return whether a node is a safe intermediate repeater."""
        return bool(
            RouteOptimizer._is_classic(node)
            and node.ready
            and node.is_routing
            and node.is_listening
            and RouteOptimizer._enum_name(node.status) != "DEAD"
        )

    @staticmethod
    def _supports_beaming(node: Any) -> bool:
        """Return whether a node can beam to FLiRS devices."""
        value = getattr(node, "supports_beaming", None)
        if value is None:
            value = node.data.get("supportsBeaming")
        return bool(value)

    @staticmethod
    def _is_flirs(node: Any) -> bool:
        """Return whether a node is frequent-listening but not always-listening."""
        return bool(node.is_frequent_listening and not node.is_listening)


    @staticmethod
    def _is_application_route_kind(value: Any) -> bool:
        """Return whether a serialized RouteKind is application-defined."""
        if isinstance(value, int):
            return value == ROUTE_KIND_APPLICATION
        if isinstance(value, str):
            normalized = (
                value.replace("_", "")
                .replace("-", "")
                .replace(" ", "")
                .upper()
            )
            return normalized in {"APPLICATION", "ROUTEKIND.APPLICATION"}
        return False

    @staticmethod
    def _candidate_from_route(
        route: dict[str, Any] | None,
        *,
        label_prefix: str = "CURRENT",
        require_application_kind: bool = True,
    ) -> Candidate | None:
        """Convert a route without confusing learned LWR/NLWR with a pin."""
        if not route:
            return None
        if require_application_kind and not RouteOptimizer._is_application_route_kind(
            route.get("routeKind")
        ):
            return None
        repeaters = route.get("repeaters")
        speed = route.get("routeSpeed")
        if not isinstance(repeaters, list) or not isinstance(speed, int):
            return None
        reps = tuple(int(value) for value in repeaters)
        return Candidate(
            repeaters=reps,
            speed=speed,
            label=RouteOptimizer._route_label(reps, speed, prefix=label_prefix),
        )

    async def _get_priority_state(self, client: Any, node_id: int) -> PriorityState:
        """Snapshot the application-priority state and current effective route."""
        try:
            data = await client.async_send_command(
                {
                    "command": "controller.get_priority_route",
                    "destinationNodeId": node_id,
                }
            )
        except Exception as err:
            raise HomeAssistantError(
                "Could not read Z-Wave priority routes. The connected Z-Wave JS "
                "Server may be too old for the priority-route API, or the controller "
                f"rejected the request for node {node_id}: {err}"
            ) from err

        route = data.get("route") if isinstance(data, dict) else None
        if route is not None and not isinstance(route, dict):
            raise HomeAssistantError(
                f"Z-Wave JS returned an unexpected priority-route response for node {node_id}."
            )

        return PriorityState(
            application=self._candidate_from_route(
                route,
                label_prefix="CURRENT",
                require_application_kind=True,
            ),
            effective_route=route,
            route_kind=route.get("routeKind") if route else None,
        )

    @staticmethod
    def _format_route_dict(route: dict[str, Any]) -> dict[str, Any]:
        """Normalize a Z-Wave route object for action response data."""
        speed = route.get("routeSpeed")
        try:
            speed_int = int(speed) if speed is not None else None
        except (TypeError, ValueError):
            speed_int = None
        repeaters = route.get("repeaters", [])
        if not isinstance(repeaters, list):
            repeaters = []
        return {
            "repeaters": [int(value) for value in repeaters],
            "route_speed": speed_int,
            "route_speed_kbps": RouteOptimizer._route_speed_kbps(speed_int),
        }

    async def _get_cached_return_route_state(
        self,
        client: Any,
        node_id: int,
    ) -> dict[str, Any]:
        """Read Z-Wave JS's cached SUC return-route knowledge.

        This is intentionally diagnostic only. The actual route stored in an
        end node cannot be queried, so cached information may be stale.
        """
        state: dict[str, Any] = {
            "source": "zwave_js_cache",
            "actual_node_state_queryable": False,
            "readback_supported": self._return_route_readback_supported,
            "priority_suc_return_route": None,
            "custom_suc_return_routes": [],
        }

        if self._return_route_readback_supported is False:
            state["readback_status"] = "unsupported_by_zwave_js_server"
            state["readback_note"] = (
                "Return-route getter commands were previously rejected as unsupported; "
                "the optimizer will not issue them again during this Home Assistant run."
            )
            return state

        def unknown_command(err: Exception) -> bool:
            text = str(err).lower().replace(" ", "_")
            return "unknown_command" in text or "unknowncommand" in text

        try:
            data = await client.async_send_command(
                {
                    "command": "controller.get_priority_suc_return_route",
                    "nodeId": node_id,
                }
            )
            route = data
            if isinstance(data, dict):
                if "result" in data:
                    route = data.get("result")
                elif "route" in data:
                    route = data.get("route")
            if isinstance(route, dict) and "repeaters" in route:
                state["priority_suc_return_route"] = self._format_route_dict(route)
        except Exception as err:
            if unknown_command(err):
                self._return_route_readback_supported = False
                state["readback_supported"] = False
                state["readback_status"] = "unsupported_by_zwave_js_server"
                state["readback_note"] = (
                    "Z-Wave JS Server rejected the return-route getter as unknown_command. "
                    "Further getter attempts are suppressed for this Home Assistant run."
                )
                return state
            state["priority_suc_return_route_error"] = str(err)
        else:
            self._return_route_readback_supported = True
            state["readback_supported"] = True

        try:
            data = await client.async_send_command(
                {
                    "command": "controller.get_custom_suc_return_route",
                    "nodeId": node_id,
                }
            )
            routes = data
            if isinstance(data, dict):
                if "result" in data:
                    routes = data.get("result")
                elif "routes" in data:
                    routes = data.get("routes")
            if isinstance(routes, list):
                state["custom_suc_return_routes"] = [
                    self._format_route_dict(route)
                    for route in routes
                    if isinstance(route, dict)
                ]
        except Exception as err:
            if unknown_command(err):
                self._return_route_readback_supported = False
                state["readback_supported"] = False
                state["readback_status"] = "unsupported_by_zwave_js_server"
                state["readback_note"] = (
                    "Z-Wave JS Server rejected a return-route getter as unknown_command. "
                    "Further getter attempts are suppressed for this Home Assistant run."
                )
                state.pop("priority_suc_return_route_error", None)
                return state
            state["custom_suc_return_routes_error"] = str(err)

        return state

    @staticmethod
    def _return_route_topology_validated(
        repeaters: tuple[int, ...],
        graph: dict[int, set[int]],
        controller_id: int,
        target_id: int,
    ) -> bool:
        """Check each node->controller hop against current neighbor information."""
        reverse_path = (target_id, *repeaters, controller_id)
        return all(
            dst in graph.get(src, set())
            for src, dst in zip(reverse_path, reverse_path[1:])
        )

    @staticmethod
    def _suggest_return_route(
        candidate: Candidate,
        graph: dict[int, set[int]],
        controller_id: int,
        target_id: int,
    ) -> dict[str, Any] | None:
        """Suggest a node->controller route by reversing the forward route.

        This is never applied automatically. The reverse path is checked
        against the controller's current neighbor information, but RF links
        can still behave asymmetrically.
        """
        if candidate.is_auto or candidate.repeaters is None or candidate.speed is None:
            return None

        repeaters = tuple(reversed(candidate.repeaters))
        topology_validated = RouteOptimizer._return_route_topology_validated(
            repeaters, graph, controller_id, target_id
        )

        return {
            "repeaters": list(repeaters),
            "route_speed": candidate.speed,
            "route_speed_kbps": RouteOptimizer._route_speed_kbps(candidate.speed),
            "basis": "reverse_of_winning_forward_route",
            "topology_validated": topology_validated,
            "warning": (
                "Recommendation only. Neighbor-table validation does not prove "
                "equal RF performance in both directions, and the actual return "
                "route cannot be read back from the node."
            ),
        }


    @staticmethod
    def _controller_method_succeeded(data: Any) -> bool:
        """Interpret common Z-Wave JS Server controller-method responses."""
        if data is True:
            return True
        if data is False or data is None:
            return False
        if isinstance(data, dict):
            if data.get("success") is False:
                return False
            result = data.get("result")
            if isinstance(result, bool):
                return result
            if data.get("success") is True:
                return True
        return False

    async def _assign_priority_suc_return_route(
        self,
        client: Any,
        node_id: int,
        repeaters: tuple[int, ...],
        speed: int,
    ) -> None:
        """Write a priority route from an end node back to the SUC/controller."""
        if speed not in ROUTE_SPEED_TO_BPS:
            raise HomeAssistantError(
                f"Invalid Z-Wave route speed value {speed} for node {node_id}."
            )
        if len(repeaters) > 4:
            raise HomeAssistantError(
                f"Priority SUC return route for node {node_id} has more than 4 repeaters."
            )

        data = await client.async_send_command(
            {
                "command": "controller.assign_priority_suc_return_route",
                "nodeId": node_id,
                "repeaters": list(repeaters),
                "routeSpeed": speed,
            }
        )
        if not self._controller_method_succeeded(data):
            raise HomeAssistantError(
                f"Z-Wave JS rejected the priority SUC return route for node {node_id}."
            )

    async def _set_priority_route(
        self,
        client: Any,
        node_id: int,
        repeaters: tuple[int, ...],
        speed: int,
    ) -> None:
        """Set an application priority route."""
        if speed not in ROUTE_SPEED_TO_BPS:
            raise HomeAssistantError(
                f"Invalid Z-Wave route speed value {speed} for node {node_id}; "
                "expected 1 (9.6k), 2 (40k), or 3 (100k)."
            )
        data = await client.async_send_command(
            {
                "command": "controller.set_priority_route",
                "destinationNodeId": node_id,
                "repeaters": list(repeaters),
                "routeSpeed": speed,
            }
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise HomeAssistantError(
                f"Z-Wave JS rejected priority route for node {node_id}."
            )

    async def _remove_priority_route(self, client: Any, node_id: int) -> None:
        """Remove an application priority route."""
        data = await client.async_send_command(
            {
                "command": "controller.remove_priority_route",
                "destinationNodeId": node_id,
            }
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise HomeAssistantError(
                f"Z-Wave JS could not remove priority route for node {node_id}."
            )

    async def _apply_candidate(self, client: Any, node_id: int, candidate: Candidate) -> None:
        """Apply one candidate."""
        if candidate.is_auto:
            await self._remove_priority_route(client, node_id)
        else:
            assert candidate.speed is not None
            assert candidate.repeaters is not None
            await self._set_priority_route(
                client, node_id, candidate.repeaters, candidate.speed
            )

    async def _restore_priority_state(
        self, client: Any, node_id: int, previous: PriorityState
    ) -> None:
        """Restore only the application-defined priority route state.

        LWR and NLWR are learned routes and must never be converted into a
        persistent application priority route during rollback.
        """
        if previous.application is None:
            await self._remove_priority_route(client, node_id)
            return
        await self._set_priority_route(
            client,
            node_id,
            previous.application.repeaters or (),
            int(previous.application.speed),
        )

    @staticmethod
    def _route_speed_kbps(speed: int | None) -> float | None:
        """Convert Serial API route-speed enum 1/2/3 to kbit/s."""
        if speed is None:
            return None
        bps = ROUTE_SPEED_TO_BPS.get(int(speed))
        return None if bps is None else bps / 1000.0

    @staticmethod
    def _route_label(
        repeaters: tuple[int, ...] | None,
        speed: int | None,
        *,
        prefix: str | None = None,
    ) -> str:
        """Human readable route label."""
        if repeaters is None:
            label = "AUTO"
        else:
            route = "direct" if not repeaters else " → ".join(map(str, repeaters))
            kbps = RouteOptimizer._route_speed_kbps(speed)
            speed_label = f"{kbps:g}k" if kbps is not None else f"speed={speed}"
            label = f"{route} @ {speed_label}"
        return f"{prefix}: {label}" if prefix else label

    @staticmethod
    def _rate_to_bps(value: Any) -> int | None:
        """Normalize Z-Wave capability metadata to bits per second."""
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = -1

        if numeric in VALID_BPS:
            return numeric
        if numeric in ROUTE_SPEED_TO_BPS:
            return ROUTE_SPEED_TO_BPS[numeric]

        name = getattr(value, "name", "")
        normalized = str(name).upper()
        if "9K6" in normalized:
            return 9600
        if "40K" in normalized:
            return 40000
        if "100K" in normalized and "LONG" not in normalized:
            return 100000
        return None

    @staticmethod
    def _common_speeds(
        path_node_ids: Iterable[int], nodes: dict[int, Any]
    ) -> list[int]:
        """Return Serial API route-speed enum values valid for the entire path."""
        common_bps: set[int] | None = None
        for node_id in path_node_ids:
            node = nodes[node_id]
            rates_bps = {
                normalized
                for rate in (node.supported_data_rates or [])
                if (normalized := RouteOptimizer._rate_to_bps(rate)) is not None
            }
            if not rates_bps and node.max_data_rate:
                maximum = RouteOptimizer._rate_to_bps(node.max_data_rate)
                if maximum is not None:
                    rates_bps = {rate for rate in VALID_BPS if rate <= maximum}
            if rates_bps:
                common_bps = (
                    rates_bps
                    if common_bps is None
                    else common_bps & rates_bps
                )

        if not common_bps:
            # routeSpeed=2 is 40 kbit/s
            return [2]

        return [
            BPS_TO_ROUTE_SPEED[bps]
            for bps in sorted(common_bps, reverse=True)
            if bps in BPS_TO_ROUTE_SPEED
        ]

    @staticmethod
    def _statistics(node: Any) -> dict[str, Any]:
        """Return serialized Z-Wave JS statistics when available."""
        data = getattr(node, "data", None)
        if not isinstance(data, dict):
            return {}
        statistics_data = data.get("statistics")
        return statistics_data if isinstance(statistics_data, dict) else {}

    @staticmethod
    def _valid_rssi(value: Any) -> float | None:
        """Normalize a real RSSI value, excluding Z-Wave sentinel/error values."""
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if -130.0 <= numeric <= -20.0:
            return numeric
        return None

    @classmethod
    def _controller_noise_floor(cls, nodes: dict[int, Any], controller_id: int) -> float | None:
        """Return a conservative controller-local background RSSI hint."""
        controller = nodes.get(controller_id)
        if controller is None:
            return None
        background = cls._statistics(controller).get("backgroundRSSI")
        if not isinstance(background, dict):
            return None
        values: list[float] = []
        for channel in background.values():
            if not isinstance(channel, dict):
                continue
            value = cls._valid_rssi(channel.get("average"))
            if value is None:
                value = cls._valid_rssi(channel.get("current"))
            if value is not None:
                values.append(value)
        return max(values) if values else None

    @classmethod
    def _rf_path_hint(cls, repeaters: tuple[int, ...], target_id: int, nodes: dict[int, Any], controller_id: int) -> tuple[float, tuple[str, ...]]:
        """Rank a path using passive RF/history hints without hard-filtering it."""
        score = 0.0
        evidence: list[str] = []
        target = nodes.get(target_id)
        if target is None:
            return score, ()
        target_stats = cls._statistics(target)
        noise_floor = cls._controller_noise_floor(nodes, controller_id)
        first_hop_id = repeaters[0] if repeaters else target_id
        first_hop = nodes.get(first_hop_id)
        if first_hop is not None:
            first_rssi = cls._valid_rssi(cls._statistics(first_hop).get("rssi"))
            if first_rssi is not None:
                score += max(0.0, min(45.0, first_rssi + 110.0))
                evidence.append(f"controller_link_rssi={first_rssi:g}dBm")
                if noise_floor is not None:
                    snr = first_rssi - noise_floor
                    score += max(0.0, min(20.0, snr)) * 0.5
                    evidence.append(f"controller_snr_hint={snr:g}dB")
        for route_name, exact_bonus in (("lwr", 90.0), ("nlwr", 70.0)):
            route = target_stats.get(route_name)
            if not isinstance(route, dict):
                continue
            raw_repeaters = route.get("repeaters")
            if not isinstance(raw_repeaters, list):
                continue
            try:
                learned = tuple(int(value) for value in raw_repeaters)
            except (TypeError, ValueError):
                continue
            if learned == repeaters:
                score += exact_bonus
                evidence.append(f"matches_{route_name}")
            elif repeaters and any(value in learned for value in repeaters):
                overlap = len(set(repeaters) & set(learned))
                score += 12.0 * overlap
                evidence.append(f"overlaps_{route_name}={overlap}")
            if learned == repeaters:
                link_rssi: list[float] = []
                route_rssi = cls._valid_rssi(route.get("rssi"))
                if route_rssi is not None:
                    link_rssi.append(route_rssi)
                raw_repeater_rssi = route.get("repeaterRSSI")
                if isinstance(raw_repeater_rssi, list):
                    for value in raw_repeater_rssi:
                        normalized = cls._valid_rssi(value)
                        if normalized is not None:
                            link_rssi.append(normalized)
                if link_rssi:
                    weakest = min(link_rssi)
                    score += max(0.0, min(35.0, weakest + 110.0))
                    evidence.append(f"{route_name}_weakest_rssi={weakest:g}dBm")
        return score, tuple(evidence)

    @staticmethod
    def _benchmark_is_clean(result: BenchResult) -> bool:
        return result.failures == 0 and result.wake_failures == 0 and result.slow_samples == 0 and math.isfinite(result.median_ms) and math.isfinite(result.worst_ms)

    @classmethod
    def _excellent_direct(cls, result: BenchResult) -> bool:
        return result.candidate.repeaters == () and cls._benchmark_is_clean(result) and result.median_ms <= ADAPTIVE_DIRECT_MEDIAN_MS and result.worst_ms <= ADAPTIVE_DIRECT_WORST_MS

    @classmethod
    def _excellent_one_hop(cls, result: BenchResult) -> bool:
        return result.candidate.repeaters is not None and len(result.candidate.repeaters) == 1 and cls._benchmark_is_clean(result) and result.median_ms <= ADAPTIVE_ONE_HOP_MEDIAN_MS and result.worst_ms <= ADAPTIVE_ONE_HOP_WORST_MS

    @staticmethod
    def _find_paths(
        controller_id: int,
        target_id: int,
        graph: dict[int, set[int]],
        nodes: dict[int, Any],
        max_repeaters: int,
        max_paths: int,
    ) -> list[tuple[int, ...]]:
        """Find bounded source routes with independent budgets per hop depth."""
        found: list[tuple[int, ...]] = []
        target = nodes[target_id]
        max_pending = max(256, max_paths * 20)

        for repeater_depth in range(max_repeaters + 1):
            depth_found: list[tuple[int, ...]] = []
            pending: deque[tuple[int, ...]] = deque([(controller_id,)])

            while pending and len(depth_found) < max_paths:
                path = pending.popleft()
                current = path[-1]
                used_repeaters = len(path) - 1

                if used_repeaters == repeater_depth:
                    if target_id not in graph.get(current, set()):
                        continue
                    if (
                        RouteOptimizer._is_flirs(target)
                        and current != controller_id
                        and not RouteOptimizer._supports_beaming(nodes[current])
                    ):
                        continue
                    depth_found.append(tuple(path[1:]))
                    continue

                for nxt in sorted(graph.get(current, ())):
                    if nxt == target_id or nxt in path:
                        continue
                    node = nodes.get(nxt)
                    if node is None or not RouteOptimizer._can_repeat(node):
                        continue
                    if len(pending) >= max_pending:
                        break
                    pending.append((*path, nxt))

            found.extend(depth_found)

        return sorted(set(found), key=lambda route: (len(route), route))

    async def _build_graph(
        self,
        client: Any,
        controller: Any,
        *,
        refresh_neighbors: bool,
    ) -> tuple[dict[int, set[int]], list[str]]:
        """Read or optionally rediscover the topology."""
        warnings: list[str] = []
        nodes = controller.nodes

        if refresh_neighbors:
            # The Serial API cannot actively rediscover neighbors for the
            # controller itself. Refresh only always-listening classic repeaters.
            # The controller's existing routing-info neighbors are still read below.
            refresh_ids = [
                node_id
                for node_id, node in sorted(nodes.items())
                if node_id != controller.own_node_id and self._can_repeat(node)
            ]
            for node_id in refresh_ids:
                await self._ensure_network_safe_to_test(check_ota=False)
                try:
                    data = await client.async_send_command(
                        {
                            "command": "controller.discover_node_neighbors",
                            "nodeId": node_id,
                        }
                    )
                    if not isinstance(data, dict) or data.get("success") is not True:
                        warnings.append(
                            f"Neighbor rediscovery reported failure for node {node_id}."
                        )
                except Exception as err:
                    warnings.append(
                        f"Neighbor rediscovery failed for node {node_id}: {err}"
                    )
                # Active rediscovery is intentionally opt-in and intentionally
                # paced. The discovery call itself can be expensive on RF.
                await asyncio.sleep(0.5)

        graph: dict[int, set[int]] = {}
        for node_id, node in sorted(nodes.items()):
            # Reading routing info is controller-local; include all ready
            # classic nodes so reverse-path plausibility can be evaluated too.
            if (
                not self._is_classic(node)
                or not node.ready
                or self._enum_name(node.status) == "DEAD"
            ):
                continue
            try:
                neighbors = await controller.async_get_node_neighbors(node)
                graph[node_id] = {int(value) for value in neighbors}
            except Exception as err:
                graph[node_id] = set()
                warnings.append(
                    f"Could not read neighbors for node {node_id}: {err}"
                )

        return graph, warnings

    def _candidate_list(
        self, node_id: int, controller_id: int, graph: dict[int, set[int]], nodes: dict[int, Any], previous: PriorityState, *, max_repeaters: int, max_candidates: int, include_auto: bool, adaptive_testing: bool,
    ) -> list[Candidate]:
        """Build a bounded candidate list with optional passive RF prioritization."""
        candidates: list[Candidate] = []
        current = previous.application
        if current:
            if adaptive_testing and current.repeaters is not None:
                rf_score, rf_evidence = self._rf_path_hint(current.repeaters, node_id, nodes, controller_id)
                current = Candidate(current.repeaters, current.speed, current.label, rf_score, rf_evidence)
            candidates.append(current)
        if include_auto:
            candidates.append(Candidate(None, None, "AUTO"))
        paths = self._find_paths(controller_id, node_id, graph, nodes, max_repeaters=max_repeaters, max_paths=max(max_candidates * 4, max_candidates))
        by_depth: dict[int, list[tuple[tuple[int, ...], list[int], float, tuple[str, ...]]]] = {}
        for repeaters in paths:
            speeds = self._common_speeds((controller_id, *repeaters, node_id), nodes)
            if not speeds:
                continue
            rf_score, rf_evidence = self._rf_path_hint(repeaters, node_id, nodes, controller_id) if adaptive_testing else (0.0, ())
            by_depth.setdefault(len(repeaters), []).append((repeaters, speeds, rf_score, rf_evidence))
        if adaptive_testing:
            for specs in by_depth.values():
                specs.sort(key=lambda item: (-item[2], item[0]))
        depth_queues: dict[int, deque[Candidate]] = {}
        for depth, specs in sorted(by_depth.items()):
            queue: deque[Candidate] = deque()
            max_speed_count = max(len(speeds) for _, speeds, _, _ in specs)
            for speed_index in range(max_speed_count):
                for repeaters, speeds, rf_score, rf_evidence in specs:
                    if speed_index >= len(speeds):
                        continue
                    speed = speeds[speed_index]
                    queue.append(Candidate(repeaters, speed, self._route_label(repeaters, speed), rf_score, rf_evidence))
            depth_queues[depth] = queue
        generated: list[Candidate] = []
        active_depths = deque(depth for depth in sorted(depth_queues) if depth_queues[depth])
        while active_depths and len(generated) < max_candidates:
            depth = active_depths.popleft(); queue = depth_queues[depth]; generated.append(queue.popleft())
            if queue: active_depths.append(depth)
        if adaptive_testing:
            generated = [candidate for _, candidate in sorted(enumerate(generated), key=lambda item: (len(item[1].repeaters or ()), item[0]))]
        candidates.extend(generated)
        unique: list[Candidate] = []; seen: set[tuple[Any, Any]] = set()
        for candidate in candidates:
            key=(candidate.repeaters,candidate.speed)
            if key in seen: continue
            seen.add(key); unique.append(candidate)
        return unique

    async def _ping_sample(self, node: Any) -> tuple[bool, float]:
        """Measure one Z-Wave JS ping without adding a second timeout layer."""
        started = time.monotonic()
        try:
            responded = bool(await node.async_ping())
        except Exception:
            responded = False
        elapsed = (time.monotonic() - started) * 1000.0
        return responded, elapsed

    async def _benchmark(
        self,
        client: Any,
        node: Any,
        candidate: Candidate,
        *,
        rounds: int,
        warmup: int,
        settle_seconds: float,
        baseline_median_ms: float | None = None,
    ) -> BenchResult:
        """Benchmark one candidate's routing quality.

        FLiRS sleep/wake timing is intentionally excluded from the score.
        At least one unscored ping wakes a FLiRS device, then measured probes
        are sent closely together so they primarily measure the route.

        Early elimination is intentionally conservative: repeated transaction
        failures stop immediately, while latency-only elimination requires an
        already established device baseline plus several clearly pathological
        scored samples.
        """
        await self._ensure_network_safe_to_test(check_ota=False)
        await self._apply_candidate(client, node.node_id, candidate)
        await asyncio.sleep(settle_seconds)

        warmup_count = max(warmup, 1 if self._is_flirs(node) else 0)
        warmup_samples: list[float] = []
        wake_failures = 0
        for _ in range(warmup_count):
            responded, elapsed = await self._ping_sample(node)
            warmup_samples.append(elapsed)
            if self._is_flirs(node) and not responded:
                wake_failures += 1
            await asyncio.sleep(DEFAULT_SAMPLE_INTERVAL)

        samples: list[float] = []
        failures = 0
        stopped_early = False
        stop_reason: str | None = None
        slow_threshold = (
            max(250.0, baseline_median_ms * 4.0)
            if baseline_median_ms is not None
            else None
        )

        for index in range(rounds):
            responded, elapsed = await self._ping_sample(node)
            samples.append(elapsed)
            if not responded:
                failures += 1

            if failures >= 2:
                stopped_early = True
                stop_reason = "two transaction failures"
                break

            if slow_threshold is not None and len(samples) >= 3:
                slow_count = sum(value > slow_threshold for value in samples)
                current_median = statistics.median(samples)
                if slow_count >= 2 and current_median > slow_threshold:
                    stopped_early = True
                    stop_reason = (
                        "latency clearly worse than established device baseline "
                        f"({slow_count}/{len(samples)} samples > {slow_threshold:.0f} ms)"
                    )
                    break

            if index + 1 < rounds:
                await asyncio.sleep(DEFAULT_SAMPLE_INTERVAL)

        return BenchResult(
            candidate=candidate,
            samples_ms=samples,
            failures=failures,
            warmup_samples_ms=warmup_samples,
            wake_failures=wake_failures,
            baseline_median_ms=baseline_median_ms,
            stopped_early=stopped_early,
            stop_reason=stop_reason,
            planned_rounds=rounds,
        )

    @staticmethod
    def _derive_device_baseline(results: list[BenchResult]) -> float | None:
        """Return the best-known device latency baseline without self-inflation."""
        finite = [result for result in results if math.isfinite(result.median_ms)]
        if not finite:
            return None
        clean = [
            result
            for result in finite
            if result.failures == 0 and result.wake_failures == 0
        ]
        pool = clean or finite
        return min(result.median_ms for result in pool)

    @staticmethod
    def _apply_device_baseline(
        results: list[BenchResult], baseline_median_ms: float | None
    ) -> None:
        """Apply one common slow-sample baseline to every candidate result."""
        for result in results:
            result.baseline_median_ms = baseline_median_ms

    @staticmethod
    def _choose_winner(
        results: list[BenchResult],
        previous: PriorityState,
        min_improvement: float,
    ) -> BenchResult:
        """Choose the best route while avoiding unnecessary route churn."""
        raw_best = min(
            results,
            key=lambda item: (
                item.failures,
                item.wake_failures,
                item.slow_samples,
                item.median_ms,
                item.worst_ms,
                item.score,
            ),
        )
        tolerance = 1.0 + min_improvement / 100.0

        def near_equal(result: BenchResult) -> bool:
            return (
                result.failures == raw_best.failures
                and result.wake_failures == raw_best.wake_failures
                and result.slow_samples == raw_best.slow_samples
                and result.median_ms <= raw_best.median_ms * tolerance
                and result.worst_ms <= max(
                    raw_best.worst_ms * tolerance,
                    raw_best.worst_ms + 15.0,
                )
            )

        current = previous.application
        if current is not None:
            for result in results:
                if (
                    result.candidate.repeaters == current.repeaters
                    and result.candidate.speed == current.speed
                    and near_equal(result)
                ):
                    return result

        # If AUTO is really equivalent, prefer not to pin anything.
        for result in results:
            if result.candidate.is_auto and near_equal(result):
                return result

        return raw_best

    @staticmethod
    def _same_candidate(left: Candidate | None, right: Candidate | None) -> bool:
        """Return whether two route candidates describe the same route."""
        if left is None or right is None:
            return left is right
        return left.repeaters == right.repeaters and left.speed == right.speed

    async def _optimize_one(
        self,
        *,
        client: Any,
        controller: Any,
        graph: dict[int, set[int]],
        node: Any,
        apply: bool,
        apply_return_route: bool,
        allow_unvalidated_return_route: bool,
        rounds: int,
        warmup: int,
        max_repeaters: int,
        max_candidates: int,
        min_improvement: float,
        settle_seconds: float,
        include_auto: bool,
        adaptive_testing: bool,
        defer_apply: bool,
    ) -> tuple[dict[str, Any], Candidate | None, PriorityState]:
        """Optimize one node and always restore it before returning."""
        node_id = node.node_id
        node_name = node.data.get("name") or f"Node {node_id}"
        previous = await self._get_priority_state(client, node_id)
        candidates = self._candidate_list(
            node_id,
            controller.own_node_id,
            graph,
            controller.nodes,
            previous,
            max_repeaters=max_repeaters,
            max_candidates=max_candidates,
            include_auto=include_auto,
            adaptive_testing=adaptive_testing,
        )

        result_data: dict[str, Any] = {
            "node_id": node_id,
            "name": node_name,
            "applied": False,
            "applied_forward_route": False,
            "applied_return_route": False,
            "starting_priority_state": previous.as_dict(),
            "sampling": {
                "mode": (
                    "flirs_warm_then_route_probes"
                    if self._is_flirs(node)
                    else "continuous_route_probes"
                ),
                "scored_probe_interval_seconds": DEFAULT_SAMPLE_INTERVAL,
                "flirs_wakeup_samples_scored": False,
                "slow_sample_threshold_basis": "best_known_device_median_x4_min_250ms",
                "early_elimination": True,
                "adaptive_testing": adaptive_testing,
            },
            "candidate_strategy": {
                "adaptive_testing": adaptive_testing,
                "rf_ranking": "passive_zwave_js_statistics" if adaptive_testing else "disabled",
                "hard_rssi_filtering": False,
                "direct_short_circuit": {"median_ms": ADAPTIVE_DIRECT_MEDIAN_MS, "worst_ms": ADAPTIVE_DIRECT_WORST_MS},
                "one_hop_short_circuit": {"paths_before_decision": ADAPTIVE_ONE_HOP_PATHS, "median_ms": ADAPTIVE_ONE_HOP_MEDIAN_MS, "worst_ms": ADAPTIVE_ONE_HOP_WORST_MS},
                "planned_candidates": len(candidates),
                "rf_ranked_candidates": sum(bool(candidate.rf_evidence) for candidate in candidates),
                "tested_candidates": 0, "skipped_candidates": 0, "stop_reason": None, "untested": [],
            },
            "return_route_state": await self._get_cached_return_route_state(
                client, node_id
            ),
            "candidates": [],
            "confirmation": None,
        }

        if not candidates:
            result_data["status"] = "no_candidates"
            return result_data, None, previous

        results: list[BenchResult] = []
        candidate_records: list[BenchResult | dict[str, Any]] = []
        winner: BenchResult | None = None
        replacement_inconclusive = False
        adaptive_stop_reason: str | None = None
        baseline_candidate_count = (1 if previous.application is not None else 0) + (1 if include_auto else 0)
        generated_candidates = candidates[baseline_candidate_count:]
        one_hop_paths_planned = {c.repeaters for c in generated_candidates if c.repeaters is not None and len(c.repeaters) == 1}
        one_hop_paths_needed = min(ADAPTIVE_ONE_HOP_PATHS, len(one_hop_paths_planned))
        one_hop_paths_tested: set[tuple[int, ...]] = set()
        try:
            for candidate_index, candidate in enumerate(candidates, start=1):
                baseline = self._derive_device_baseline(results)
                self._update_status(phase="benchmark", current_node_id=node_id, current_node_name=node_name, candidate_index=candidate_index, candidate_total=len(candidates), current_route=candidate.label)
                try:
                    benchmark = await self._benchmark(client, node, candidate, rounds=rounds, warmup=warmup, settle_seconds=settle_seconds, baseline_median_ms=baseline)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    candidate_records.append({"route": candidate.label, "repeaters": None if candidate.repeaters is None else list(candidate.repeaters), "route_speed": candidate.speed, "route_speed_kbps": self._route_speed_kbps(candidate.speed), "rf_score": round(candidate.rf_score,1), "rf_evidence": list(candidate.rf_evidence), "error": str(err)})
                    continue
                results.append(benchmark); candidate_records.append(benchmark)
                if not adaptive_testing or candidate_index <= baseline_candidate_count:
                    continue
                if self._excellent_direct(benchmark):
                    adaptive_stop_reason = f"excellent direct route confirmed ({benchmark.median_ms:.1f} ms median, {benchmark.worst_ms:.1f} ms worst)"; break
                if candidate.repeaters is not None and len(candidate.repeaters) == 1:
                    one_hop_paths_tested.add(candidate.repeaters)
                    if one_hop_paths_needed > 0 and len(one_hop_paths_tested) >= one_hop_paths_needed:
                        clean = [r for r in results if r.candidate.repeaters is not None and len(r.candidate.repeaters)==1 and self._benchmark_is_clean(r)]
                        if clean:
                            best_one_hop=min(clean,key=lambda r:(r.median_ms,r.worst_ms,r.score))
                            if self._excellent_one_hop(best_one_hop):
                                adaptive_stop_reason=f"excellent one-hop route confirmed after {len(one_hop_paths_tested)} RF-ranked paths ({best_one_hop.candidate.label}: {best_one_hop.median_ms:.1f} ms median)"; break

            if results:
                baseline = self._derive_device_baseline(results)
                self._apply_device_baseline(results, baseline)
                winner = self._choose_winner(results, previous, min_improvement)

                # Existing pins get a fresh head-to-head confirmation before a
                # different route is recommended. This protects a known-good pin
                # from one unlucky outlier in the broad scan.
                incumbent = previous.application
                if incumbent is not None and not self._same_candidate(
                    winner.candidate, incumbent
                ):
                    confirmation_candidates = [incumbent, winner.candidate]
                    confirmation_results: list[BenchResult] = []
                    confirmation_errors: list[dict[str, Any]] = []
                    for confirmation_index, candidate in enumerate(
                        confirmation_candidates, start=1
                    ):
                        self._update_status(
                            phase="confirmation",
                            candidate_index=confirmation_index,
                            candidate_total=len(confirmation_candidates),
                            current_route=candidate.label,
                        )
                        try:
                            confirmation = await self._benchmark(
                                client,
                                node,
                                candidate,
                                rounds=rounds,
                                warmup=warmup,
                                settle_seconds=settle_seconds,
                                baseline_median_ms=baseline,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            confirmation_errors.append(
                                {"route": candidate.label, "error": str(err)}
                            )
                            continue
                        confirmation_results.append(confirmation)

                    if len(confirmation_results) == 2:
                        confirmation_baseline = self._derive_device_baseline(
                            confirmation_results
                        )
                        self._apply_device_baseline(
                            confirmation_results, confirmation_baseline
                        )
                        winner = self._choose_winner(
                            confirmation_results, previous, min_improvement
                        )
                        result_data["confirmation"] = {
                            "performed": True,
                            "reason": "challenger_would_replace_existing_application_priority_route",
                            "results": [
                                item.as_dict() for item in confirmation_results
                            ],
                            "winner": winner.as_dict(),
                        }
                    else:
                        # Replacement is intentionally fail-safe. If the fresh
                        # incumbent-vs-challenger comparison cannot complete, keep
                        # the incumbent instead of removing/replacing a known pin.
                        incumbent_confirmation = next(
                            (
                                result
                                for result in confirmation_results
                                if self._same_candidate(result.candidate, incumbent)
                            ),
                            None,
                        )
                        incumbent_initial = next(
                            (
                                result
                                for result in results
                                if self._same_candidate(result.candidate, incumbent)
                            ),
                            None,
                        )
                        safe_incumbent = incumbent_confirmation or incumbent_initial
                        if safe_incumbent is not None:
                            winner = safe_incumbent
                        else:
                            # We do not have enough fresh evidence to construct a
                            # trustworthy replacement recommendation. Refuse to
                            # replace rather than silently falling back to challenger.
                            winner = None
                            replacement_inconclusive = True
                        result_data["confirmation"] = {
                            "performed": True,
                            "reason": "challenger_would_replace_existing_application_priority_route",
                            "status": (
                                "inconclusive_kept_incumbent"
                                if safe_incumbent is not None
                                else "inconclusive_no_replacement_recommendation"
                            ),
                            "errors": confirmation_errors,
                        }
        finally:
            try:
                # A cancelled HA service call must not strand the node on the
                # last experimental route.
                await asyncio.shield(
                    self._restore_priority_state(client, node_id, previous)
                )
            except Exception as err:
                _LOGGER.exception(
                    "Failed restoring starting priority route for node %s", node_id
                )
                raise RouteRestoreError(
                    f"Failed to restore the starting priority route for node {node_id}: {err}"
                ) from err

        result_data["candidate_strategy"]["tested_candidates"] = len(candidate_records)
        result_data["candidate_strategy"]["skipped_candidates"] = max(0, len(candidates)-len(candidate_records))
        result_data["candidate_strategy"]["stop_reason"] = adaptive_stop_reason
        if len(candidate_records) < len(candidates):
            result_data["candidate_strategy"]["untested"] = [{"route": c.label, "rf_score": round(c.rf_score,1), "rf_evidence": list(c.rf_evidence)} for c in candidates[len(candidate_records):]]

        # Re-render benchmark records only after the common device baseline is
        # known, otherwise early candidates would retain self-relative slow counts.
        final_baseline = self._derive_device_baseline(results)
        self._apply_device_baseline(results, final_baseline)
        result_data["candidates"] = [
            record.as_dict() if isinstance(record, BenchResult) else record
            for record in candidate_records
        ]
        result_data["device_baseline_median_ms"] = (
            None if final_baseline is None else round(final_baseline, 1)
        )

        if winner is None:
            result_data["status"] = (
                "replacement_confirmation_inconclusive"
                if replacement_inconclusive
                else "all_candidates_failed"
            )
            return result_data, None, previous

        # If confirmation selected an initial result object, ensure its diagnostics
        # reflect the final common baseline too.
        winner.baseline_median_ms = final_baseline
        result_data["best"] = winner.as_dict()
        suggestion = self._suggest_return_route(
            winner.candidate,
            graph,
            controller.own_node_id,
            node_id,
        )
        result_data["suggested_priority_suc_return_route"] = suggestion
        result_data["return_route_apply_requested"] = apply_return_route
        result_data["status"] = "ok"

        # Validate all requested permanent writes before making the first one.
        return_repeaters: tuple[int, ...] | None = None
        return_speed: int | None = None
        if apply_return_route:
            if suggestion is None:
                raise HomeAssistantError(
                    f"Node {node_id} winner is AUTO, so there is no concrete return route to apply."
                )
            return_repeaters = tuple(int(value) for value in suggestion["repeaters"])
            return_speed = int(suggestion["route_speed"])
            for repeater_id in return_repeaters:
                repeater = controller.nodes.get(repeater_id)
                if repeater is None or not self._can_repeat(repeater):
                    raise HomeAssistantError(
                        f"Repeater {repeater_id} is no longer a usable routing node."
                    )
            topology_validated = self._return_route_topology_validated(
                return_repeaters, graph, controller.own_node_id, node_id
            )
            if not topology_validated and not allow_unvalidated_return_route:
                raise HomeAssistantError(
                    "The suggested return route is not validated by the current "
                    "neighbor graph. Enable 'Allow topology-unvalidated return route' "
                    "to force it."
                )
            result_data["suggested_priority_suc_return_route"][
                "topology_validated"
            ] = topology_validated

            # Applying only the return half is safe when the winning forward route
            # is already the incumbent. Otherwise it would intentionally create a
            # mismatched forward/return pair.
            if not apply and not self._same_candidate(
                winner.candidate, previous.application
            ):
                raise HomeAssistantError(
                    "Applying the suggested return route without the winning forward "
                    "route would create a mismatched pair. Enable 'Apply best forward "
                    "route' as well, or keep the current pinned forward route as winner."
                )

        if (apply or apply_return_route) and not defer_apply:
            forward_changed = False
            try:
                if apply:
                    await self._apply_candidate(client, node_id, winner.candidate)
                    forward_changed = True
                    result_data["applied_forward_route"] = True

                if apply_return_route:
                    assert return_repeaters is not None and return_speed is not None
                    await self._assign_priority_suc_return_route(
                        client, node_id, return_repeaters, return_speed
                    )
                    result_data["applied_return_route"] = True
                    result_data["return_route_state_after"] = (
                        await self._get_cached_return_route_state(client, node_id)
                    )
            except asyncio.CancelledError:
                if forward_changed:
                    await asyncio.shield(
                        self._restore_priority_state(client, node_id, previous)
                    )
                raise
            except Exception as err:
                if forward_changed:
                    try:
                        await asyncio.shield(
                            self._restore_priority_state(client, node_id, previous)
                        )
                    except Exception as rollback_err:
                        raise RouteRestoreError(
                            f"Applying routes for node {node_id} failed and forward-route "
                            f"rollback also failed: {rollback_err}. Original error: {err}"
                        ) from rollback_err
                uncertainty = (
                    " Return-route state may be uncertain because Z-Wave JS cannot "
                    "reliably read that route back from the node."
                    if apply_return_route
                    else ""
                )
                raise HomeAssistantError(
                    f"Applying optimized route settings for node {node_id} failed; "
                    f"the starting forward application-priority state was restored."
                    f"{uncertainty} Original error: {err}"
                ) from err

            result_data["applied"] = bool(
                result_data["applied_forward_route"]
                or result_data["applied_return_route"]
            )

        return result_data, winner.candidate, previous

    async def _rollback_priority_states(
        self,
        client: Any,
        node_ids: list[int],
        states: dict[int, PriorityState],
    ) -> list[str]:
        """Best-effort rollback for a failed/cancelled whole-network commit."""
        errors: list[str] = []
        for node_id in reversed(node_ids):
            state = states.get(node_id)
            if state is None:
                errors.append(f"Node {node_id}: starting state missing")
                continue
            try:
                await self._restore_priority_state(client, node_id, state)
            except Exception as err:
                _LOGGER.exception(
                    "Failed rolling back application priority route for node %s",
                    node_id,
                )
                errors.append(f"Node {node_id}: {err}")
        return errors

    async def optimize_node(
        self,
        node: Any,
        *,
        apply: bool,
        apply_return_route: bool,
        allow_unvalidated_return_route: bool,
        rounds: int,
        warmup: int,
        max_repeaters: int,
        max_candidates: int,
        min_improvement: float,
        settle_seconds: float,
        include_auto: bool,
        adaptive_testing: bool,
        refresh_neighbors: bool,
    ) -> dict[str, Any]:
        """Optimize a single node."""
        if self._run_lock.locked():
            raise HomeAssistantError("A Z-Wave route optimization is already running.")

        async with self._run_lock:
            self._start_status("single_node", node_total=1)
            try:
                await self._ensure_network_safe_to_test(check_ota=True)
                client, _, controller = self._get_client_driver_controller()

                if node.client is not client:
                    raise HomeAssistantError(
                        "The selected device belongs to a different Z-Wave network."
                    )
                reason = self._ineligibility_reason(node)
                if reason is not None:
                    raise HomeAssistantError(
                        f"The selected node is not eligible: {reason}."
                    )

                node_name = node.data.get("name") or f"Node {node.node_id}"
                self._update_status(
                    phase="topology",
                    current_node_id=node.node_id,
                    current_node_name=node_name,
                    node_index=1,
                    node_total=1,
                )
                graph, warnings = await self._build_graph(
                    client, controller, refresh_neighbors=refresh_neighbors
                )
                result, _, _ = await self._optimize_one(
                    client=client,
                    controller=controller,
                    graph=graph,
                    node=node,
                    apply=apply,
                    apply_return_route=apply_return_route,
                    allow_unvalidated_return_route=allow_unvalidated_return_route,
                    rounds=rounds,
                    warmup=warmup,
                    max_repeaters=max_repeaters,
                    max_candidates=max_candidates,
                    min_improvement=min_improvement,
                    settle_seconds=settle_seconds,
                    include_auto=include_auto,
                    adaptive_testing=adaptive_testing,
                    defer_apply=False,
                )
                latest = self._compact_latest_result(result)
                self._update_status(
                    completed_count=1,
                    latest_result=latest,
                    phase="complete",
                    current_route=None,
                )
                return {
                    "mode": "single_node",
                    "dry_run": not (apply or apply_return_route),
                    "warnings": warnings,
                    "result": result,
                }
            except Exception as err:
                self._update_status(
                    phase="error",
                    latest_result={
                        "node_id": getattr(node, "node_id", None),
                        "name": getattr(node, "data", {}).get("name"),
                        "status": "error",
                        "error": str(err),
                    },
                )
                raise
            finally:
                self._finish_status()

    @staticmethod
    def _compact_latest_result(result: dict[str, Any]) -> dict[str, Any]:
        """Return a small status-safe summary of one completed node."""
        best = result.get("best")
        compact: dict[str, Any] = {
            "node_id": result.get("node_id"),
            "name": result.get("name"),
            "status": result.get("status"),
        }
        if isinstance(best, dict):
            compact["best_route"] = best.get("route")
            compact["median_ms"] = best.get("median_ms")
            compact["failures"] = best.get("failures")
            compact["slow_samples"] = best.get("slow_samples")
        strategy = result.get("candidate_strategy")
        if isinstance(strategy, dict):
            compact["tested_candidates"] = strategy.get("tested_candidates")
            compact["skipped_candidates"] = strategy.get("skipped_candidates")
            if strategy.get("stop_reason"):
                compact["adaptive_stop_reason"] = strategy.get("stop_reason")
        if result.get("error"):
            compact["error"] = result.get("error")
        return compact

    async def optimize_network(
        self,
        *,
        apply: bool,
        apply_return_route: bool,
        allow_unvalidated_return_route: bool,
        passes: int,
        rounds: int,
        warmup: int,
        max_repeaters: int,
        max_candidates: int,
        min_improvement: float,
        settle_seconds: float,
        include_auto: bool,
        adaptive_testing: bool,
        refresh_neighbors: bool,
    ) -> dict[str, Any]:
        """Benchmark the whole eligible mesh without committing route changes."""
        # v0.7.2 deliberately keeps whole-network writes behind a hard guard.
        if apply or apply_return_route:
            raise HomeAssistantError(
                "Whole-network Apply is intentionally disabled in v0.7.2. "
                "Run Optimize Z-Wave network with both apply toggles off; use "
                "single-node optimization for deliberate route writes."
            )
        if self._run_lock.locked():
            raise HomeAssistantError("A Z-Wave route optimization is already running.")

        async with self._run_lock:
            self._start_status("whole_network")
            try:
                await self._ensure_network_safe_to_test(check_ota=True)
                client, _, controller = self._get_client_driver_controller()
                self._update_status(phase="topology")
                graph, warnings = await self._build_graph(
                    client, controller, refresh_neighbors=refresh_neighbors
                )

                targets = [
                    node
                    for _, node in sorted(controller.nodes.items())
                    if self._eligible_target(node)
                ]
                skipped_nodes = []
                for _, node in sorted(controller.nodes.items()):
                    if node.is_controller_node or node in targets:
                        continue
                    skipped_nodes.append(
                        {
                            "node_id": node.node_id,
                            "name": node.data.get("name") or f"Node {node.node_id}",
                            "reason": self._ineligibility_reason(node) or "not eligible",
                        }
                    )

                self._update_status(
                    node_total=len(targets),
                    pass_total=passes,
                    completed_count=0,
                )
                pass_results: list[dict[str, Any]] = []
                final_results: list[dict[str, Any]] = []
                completed_node_runs = 0

                for pass_index in range(1, passes + 1):
                    node_results: list[dict[str, Any]] = []
                    self._update_status(
                        phase="benchmark",
                        pass_index=pass_index,
                        pass_total=passes,
                        current_node_id=None,
                        current_node_name=None,
                        node_index=None,
                        candidate_index=None,
                        candidate_total=None,
                        current_route=None,
                    )
                    _LOGGER.info(
                        "Z-Wave route optimizer: starting whole-network pass %s/%s",
                        pass_index,
                        passes,
                    )

                    for index, node in enumerate(targets, start=1):
                        await self._ensure_network_safe_to_test(check_ota=False)
                        node_name = node.data.get("name") or f"Node {node.node_id}"
                        self._update_status(
                            phase="benchmark",
                            current_node_id=node.node_id,
                            current_node_name=node_name,
                            node_index=index,
                            node_total=len(targets),
                            pass_index=pass_index,
                            pass_total=passes,
                            candidate_index=None,
                            candidate_total=None,
                            current_route=None,
                        )
                        _LOGGER.info(
                            "Z-Wave route optimizer: pass %s/%s benchmarking node %s (%s/%s)",
                            pass_index,
                            passes,
                            node.node_id,
                            index,
                            len(targets),
                        )
                        try:
                            result, _, _ = await self._optimize_one(
                                client=client,
                                controller=controller,
                                graph=graph,
                                node=node,
                                apply=False,
                                apply_return_route=False,
                                allow_unvalidated_return_route=allow_unvalidated_return_route,
                                rounds=rounds,
                                warmup=warmup,
                                max_repeaters=max_repeaters,
                                max_candidates=max_candidates,
                                min_improvement=min_improvement,
                                settle_seconds=settle_seconds,
                                include_auto=include_auto,
                                adaptive_testing=adaptive_testing,
                                defer_apply=True,
                            )
                        except (asyncio.CancelledError, RouteRestoreError):
                            raise
                        except Exception as err:
                            result = {
                                "node_id": node.node_id,
                                "name": node_name,
                                "status": "error",
                                "error": str(err),
                                "applied": False,
                                "applied_forward_route": False,
                                "applied_return_route": False,
                            }

                        node_results.append(result)
                        completed_node_runs += 1
                        self._update_status(
                            completed_count=completed_node_runs,
                            latest_result=self._compact_latest_result(result),
                            current_route=None,
                        )

                    pass_results.append(
                        {
                            "pass_index": pass_index,
                            "completed_nodes": len(node_results),
                            "results": [
                                self._compact_latest_result(result)
                                for result in node_results
                            ],
                        }
                    )
                    final_results = node_results

                return {
                    "mode": "whole_network",
                    "dry_run": True,
                    "whole_network_apply_enabled": False,
                    "passes_requested": passes,
                    "passes_completed": len(pass_results),
                    "eligible_nodes": len(targets),
                    "completed_nodes": len(final_results),
                    "completed_node_runs": completed_node_runs,
                    "adaptive_testing": adaptive_testing,
                    "skipped_node_ids": [item["node_id"] for item in skipped_nodes],
                    "skipped_nodes": skipped_nodes,
                    "warnings": warnings,
                    "rollback_errors": [],
                    "applied_node_ids": [],
                    "passes": pass_results,
                    "results": final_results,
                }
            except Exception as err:
                self._update_status(
                    phase="error",
                    latest_result={"status": "error", "error": str(err)},
                )
                raise
            finally:
                self._finish_status()
