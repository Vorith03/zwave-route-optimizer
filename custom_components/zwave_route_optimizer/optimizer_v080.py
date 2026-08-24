"""v0.8.0 staged whole-network apply transaction for Z-Wave Route Optimizer."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import PENDING_PLAN_TTL_SECONDS
from .optimizer import Candidate, PriorityState
from .optimizer_v074 import RouteOptimizer as V074RouteOptimizer


class _PlanInvalidError(HomeAssistantError):
    """Raised when a staged plan no longer matches the live network."""


class RouteOptimizer(V074RouteOptimizer):
    """v0.8.0 optimizer with staged, validated, transactional forward Apply."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending_apply_plan: dict[str, Any] | None = None
        self._plan_expiry_handle: asyncio.TimerHandle | None = None

    @property
    def can_apply_pending_plan(self) -> bool:
        """Return whether a staged plan currently has write-ready operations."""
        plan = self._pending_apply_plan
        return bool(
            plan
            and plan.get("write_operations")
            and not self._plan_expired(plan)
            and not self._run_lock.locked()
        )

    @property
    def pending_plan_summary(self) -> dict[str, Any] | None:
        """Return a small JSON-safe summary of the currently staged plan."""
        plan = self._pending_apply_plan
        if plan is None:
            return None
        return {
            "plan_id": plan.get("plan_id"),
            "created_at": plan.get("created_at"),
            "expires_at": plan.get("expires_at"),
            "fingerprint": plan.get("fingerprint"),
            "passes": plan.get("passes"),
            "counts": deepcopy(plan.get("counts", {})),
            "write_ready_node_ids": [
                item.get("node_id") for item in plan.get("write_operations", [])
            ],
            "apply_available": self.can_apply_pending_plan,
        }

    @staticmethod
    def _plan_expired(plan: dict[str, Any]) -> bool:
        expires = plan.get("_expires_monotonic")
        return isinstance(expires, (int, float)) and time.monotonic() >= float(expires)

    @staticmethod
    def _application_route_from_state(state: PriorityState) -> dict[str, Any] | None:
        candidate = state.application
        if candidate is None:
            return None
        return {
            "repeaters": list(candidate.repeaters or ()),
            "route_speed": int(candidate.speed) if candidate.speed is not None else None,
        }

    @staticmethod
    def _normalize_serialized_application_route(
        route: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if route is None:
            return None
        if not isinstance(route, dict):
            raise _PlanInvalidError("A staged application-priority snapshot is malformed.")
        repeaters = route.get("repeaters")
        if not isinstance(repeaters, list):
            raise _PlanInvalidError("A staged application-priority route has invalid repeaters.")
        speed = route.get("route_speed")
        try:
            return {
                "repeaters": [int(value) for value in repeaters],
                "route_speed": int(speed),
            }
        except (TypeError, ValueError) as err:
            raise _PlanInvalidError(
                "A staged application-priority route has an invalid speed."
            ) from err

    def _controller_identity(self, controller: Any) -> dict[str, Any]:
        """Return a stable-enough identity for rejecting plans from another network."""
        data = getattr(controller, "data", None)
        if not isinstance(data, dict):
            data = {}
        home_id = getattr(controller, "home_id", None)
        if home_id is None:
            home_id = data.get("homeId", data.get("home_id"))
        own_node_id = getattr(controller, "own_node_id", None)
        try:
            own_node_id = int(own_node_id) if own_node_id is not None else None
        except (TypeError, ValueError):
            own_node_id = str(own_node_id)
        if home_id is not None and not isinstance(home_id, (str, int, float, bool)):
            home_id = str(home_id)
        return {
            "zwave_entry_id": self.zwave_entry_id,
            "home_id": home_id,
            "controller_node_id": own_node_id,
        }

    @staticmethod
    def _forward_route_topology_validated(
        repeaters: tuple[int, ...],
        graph: dict[int, set[int]],
        controller_id: int,
        target_id: int,
    ) -> bool:
        path = (controller_id, *repeaters, target_id)
        return all(
            destination in graph.get(source, set())
            for source, destination in zip(path, path[1:])
        )

    def _cancel_plan_expiry_timer(self) -> None:
        if self._plan_expiry_handle is not None:
            self._plan_expiry_handle.cancel()
            self._plan_expiry_handle = None

    def _schedule_plan_expiry(self, plan: dict[str, Any]) -> None:
        self._cancel_plan_expiry_timer()
        remaining = max(0.0, float(plan["_expires_monotonic"]) - time.monotonic())
        self._plan_expiry_handle = self.hass.loop.call_later(
            remaining, self._expire_pending_plan, str(plan["plan_id"])
        )

    def _clear_pending_plan(self) -> None:
        self._pending_apply_plan = None
        self._cancel_plan_expiry_timer()

    def _expire_pending_plan(self, plan_id: str) -> None:
        """Expire the staged plan and immediately refresh button/status state."""
        plan = self._pending_apply_plan
        if plan is None or plan.get("plan_id") != plan_id:
            return
        self._pending_apply_plan = None
        self._plan_expiry_handle = None
        result = {
            "status": "plan_expired",
            "plan_id": plan_id,
            "error": "Staged plan expired; rerun optimization.",
        }
        self._update_status(
            state="plan_expired",
            phase="plan_expired",
            plan_id=plan_id,
            ready_to_apply_count=0,
            latest_result=result,
        )

    def _stage_apply_plan(self, response: dict[str, Any]) -> None:
        """Convert the v0.7.4 readiness preview into one immutable in-memory plan."""
        preview = response.get("apply_plan")
        if not isinstance(preview, dict):
            self._clear_pending_plan()
            return

        _, _, controller = self._get_client_driver_controller()
        detailed = {
            item.get("node_id"): item
            for item in response.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("node_id"), int)
        }
        operations: list[dict[str, Any]] = []
        for raw in preview.get("write_operations", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("node_id"), int):
                continue
            operation = deepcopy(raw)
            node_id = int(operation["node_id"])
            full = detailed.get(node_id, {})
            starting = full.get("starting_priority_state") if isinstance(full, dict) else None
            expected = (
                starting.get("application_priority_route")
                if isinstance(starting, dict)
                else None
            )
            best = full.get("best") if isinstance(full, dict) else None
            if isinstance(best, dict):
                operation["winner"] = best.get("route")
            operation["expected_starting_application_priority_route"] = (
                None if expected is None else {
                    "repeaters": [int(value) for value in expected.get("repeaters", [])],
                    "route_speed": int(expected.get("route_speed")),
                }
            )
            operations.append(operation)

        created = datetime.now(timezone.utc)
        expires = created + timedelta(seconds=PENDING_PLAN_TTL_SECONDS)
        identity = self._controller_identity(controller)
        fingerprint_payload = {
            "controller": identity,
            "passes": response.get("passes_requested"),
            "operations": operations,
            "counts": preview.get("counts", {}),
            "created_at": created.isoformat(),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        plan_id = fingerprint[:16]

        plan = {
            "plan_id": plan_id,
            "fingerprint": fingerprint,
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
            "_expires_monotonic": time.monotonic() + PENDING_PLAN_TTL_SECONDS,
            "controller": identity,
            "passes": int(response.get("passes_requested", 1)),
            "counts": deepcopy(preview.get("counts", {})),
            "write_operations": operations,
        }
        self._pending_apply_plan = plan
        self._schedule_plan_expiry(plan)

        staged_preview = deepcopy(preview)
        staged_preview.update(
            {
                "mode": "staged",
                "writes_enabled": bool(operations),
                "apply_action": "zwave_route_optimizer.apply_last_network_optimization",
                "apply_action_available": bool(operations),
                "plan_id": plan_id,
                "fingerprint": fingerprint,
                "created_at": plan["created_at"],
                "expires_at": plan["expires_at"],
                "write_operations": deepcopy(operations),
            }
        )
        response["apply_plan"] = staged_preview
        state = "plan_ready" if operations else "plan_no_changes"
        self._update_status(
            state=state,
            operation="whole_network",
            phase=state,
            plan_id=plan_id,
            plan_created_at=plan["created_at"],
            plan_expires_at=plan["expires_at"],
            plan_counts=deepcopy(plan["counts"]),
            ready_to_apply_count=len(operations),
            latest_result={
                "status": state,
                "plan_id": plan_id,
                "ready_to_apply_count": len(operations),
                "counts": deepcopy(plan["counts"]),
            },
        )

    async def optimize_network(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Always dry-run discovery, then stage the resulting write-ready plan."""
        self._clear_pending_plan()
        kwargs["apply"] = False
        kwargs["apply_return_route"] = False
        kwargs["allow_unvalidated_return_route"] = False
        response = await super().optimize_network(*args, **kwargs)
        self._stage_apply_plan(response)
        response["whole_network_apply_enabled"] = True
        response["apply_mode"] = "separate_staged_action"
        return response

    async def _preflight_plan(
        self,
        plan: dict[str, Any],
        client: Any,
        controller: Any,
    ) -> tuple[dict[int, PriorityState], list[str]]:
        """Validate every staged operation before the first write occurs."""
        if self._plan_expired(plan):
            raise _PlanInvalidError("The staged optimization plan has expired; rerun optimization.")
        if self._controller_identity(controller) != plan.get("controller"):
            raise _PlanInvalidError(
                "The Z-Wave controller identity changed since optimization; no routes were modified."
            )

        self._update_status(phase="preflight_topology")
        graph, warnings = await self._build_graph(
            client, controller, refresh_neighbors=False
        )
        operations = plan.get("write_operations", [])
        snapshots: dict[int, PriorityState] = {}

        for index, operation in enumerate(operations, start=1):
            if self._plan_expired(plan):
                raise _PlanInvalidError(
                    "The staged optimization plan expired during preflight; no routes were modified."
                )
            node_id = int(operation["node_id"])
            node = controller.nodes.get(node_id)
            node_name = operation.get("name") or f"Node {node_id}"
            self._update_status(
                phase="preflight",
                current_node_id=node_id,
                current_node_name=node_name,
                node_index=index,
                node_total=len(operations),
                completed_count=index - 1,
                current_route=operation.get("route") or operation.get("winner"),
            )
            if node is None:
                raise _PlanInvalidError(
                    f"Node {node_id} no longer exists; no routes were modified."
                )
            reason = self._ineligibility_reason(node)
            if reason is not None:
                raise _PlanInvalidError(
                    f"Node {node_id} is no longer eligible ({reason}); no routes were modified."
                )

            repeaters = tuple(int(value) for value in operation.get("repeaters", []))
            speed = int(operation["route_speed"])
            if not self._forward_route_topology_validated(
                repeaters, graph, controller.own_node_id, node_id
            ):
                raise _PlanInvalidError(
                    f"The staged route for node {node_id} is no longer topology-valid; "
                    "no routes were modified."
                )
            for repeater_id in repeaters:
                repeater = controller.nodes.get(repeater_id)
                if repeater is None or not self._can_repeat(repeater):
                    raise _PlanInvalidError(
                        f"Repeater {repeater_id} for node {node_id} is no longer usable; "
                        "no routes were modified."
                    )
            if self._is_flirs(node) and repeaters:
                final_repeater = controller.nodes.get(repeaters[-1])
                if final_repeater is None or not self._supports_beaming(final_repeater):
                    raise _PlanInvalidError(
                        f"The final repeater for FLiRS node {node_id} no longer supports beaming; "
                        "no routes were modified."
                    )
            common_speeds = self._common_speeds(
                (controller.own_node_id, *repeaters, node_id), controller.nodes
            )
            if speed not in common_speeds:
                raise _PlanInvalidError(
                    f"The staged route speed for node {node_id} is no longer supported end-to-end; "
                    "no routes were modified."
                )

            state = await self._get_priority_state(client, node_id)
            snapshots[node_id] = state
            actual = self._application_route_from_state(state)
            expected = self._normalize_serialized_application_route(
                operation.get("expected_starting_application_priority_route")
            )
            if actual != expected:
                raise _PlanInvalidError(
                    f"Node {node_id} application priority route changed since optimization; "
                    "no routes were modified."
                )
            self._update_status(completed_count=index)

        return snapshots, warnings

    async def _rollback_attempted(
        self,
        client: Any,
        snapshots: dict[int, PriorityState],
        attempted_node_ids: list[int],
    ) -> dict[str, Any]:
        """Restore every node whose write may have started, in reverse order."""
        restored: list[int] = []
        failed: list[dict[str, Any]] = []
        total = len(attempted_node_ids)
        for index, node_id in enumerate(reversed(attempted_node_ids), start=1):
            self._update_status(
                phase="rollback",
                current_node_id=node_id,
                current_node_name=None,
                node_index=index,
                node_total=total,
                completed_count=index - 1,
                current_route=None,
            )
            try:
                await self._restore_priority_state(client, node_id, snapshots[node_id])
                verify = await self._get_priority_state(client, node_id)
                if self._application_route_from_state(verify) != self._application_route_from_state(
                    snapshots[node_id]
                ):
                    raise HomeAssistantError("rollback verification did not match snapshot")
            except Exception as err:
                failed.append({"node_id": node_id, "error": str(err)})
            else:
                restored.append(node_id)
            self._update_status(completed_count=index)
        return {"successful_node_ids": restored, "failed": failed}

    async def apply_last_network_optimization(self) -> dict[str, Any]:
        """Preflight and transactionally apply the currently staged forward plan."""
        if self._run_lock.locked():
            raise HomeAssistantError("A Z-Wave route optimizer operation is already running.")
        plan = self._pending_apply_plan
        if plan is None:
            raise HomeAssistantError(
                "There is no staged network optimization to apply. Run Optimize Z-Wave network first."
            )
        if not plan.get("write_operations"):
            raise HomeAssistantError("The staged optimization has no write-ready forward routes.")
        if self._plan_expired(plan):
            plan_id = plan.get("plan_id")
            self._clear_pending_plan()
            self._update_status(
                state="plan_expired",
                phase="plan_expired",
                plan_id=plan_id,
                latest_result={
                    "status": "plan_expired",
                    "plan_id": plan_id,
                    "error": "Staged plan expired; rerun optimization.",
                },
            )
            raise HomeAssistantError("The staged optimization plan expired; rerun optimization.")

        operations = list(plan["write_operations"])
        plan_id = str(plan["plan_id"])
        self._cancel_plan_expiry_timer()
        async with self._run_lock:
            self._start_status("apply_last_network_optimization", node_total=len(operations))
            self._update_status(phase="preflight", plan_id=plan_id)
            attempted: list[int] = []
            applied: list[int] = []
            snapshots: dict[int, PriorityState] = {}
            try:
                await self._ensure_network_safe_to_test(check_ota=True)
                client, _, controller = self._get_client_driver_controller()
                try:
                    snapshots, warnings = await self._preflight_plan(plan, client, controller)
                    if self._plan_expired(plan):
                        raise _PlanInvalidError(
                            "The staged optimization plan expired during preflight; no routes were modified."
                        )
                except _PlanInvalidError as err:
                    self._clear_pending_plan()
                    result = {
                        "status": "plan_invalid",
                        "plan_id": plan_id,
                        "reason": str(err),
                        "applied_node_ids": [],
                    }
                    self._finish_status(latest_result=result)
                    self._update_status(
                        state="plan_invalid",
                        phase="plan_invalid",
                        plan_id=plan_id,
                        latest_result=result,
                    )
                    return result

                for index, operation in enumerate(operations, start=1):
                    node_id = int(operation["node_id"])
                    node_name = operation.get("name") or f"Node {node_id}"
                    repeaters = tuple(int(value) for value in operation.get("repeaters", []))
                    speed = int(operation["route_speed"])
                    label = self._route_label(repeaters, speed)
                    self._update_status(
                        phase="applying",
                        current_node_id=node_id,
                        current_node_name=node_name,
                        node_index=index,
                        node_total=len(operations),
                        completed_count=index - 1,
                        current_route=label,
                    )
                    self._update_status(phase="prewrite_check")
                    live_before = await self._get_priority_state(client, node_id)
                    if self._application_route_from_state(live_before) != self._application_route_from_state(
                        snapshots[node_id]
                    ):
                        raise HomeAssistantError(
                            f"Node {node_id} application priority route changed after preflight."
                        )
                    self._update_status(phase="applying")

                    attempted.append(node_id)
                    await self._set_priority_route(client, node_id, repeaters, speed)
                    self._update_status(phase="verifying")
                    verify = await self._get_priority_state(client, node_id)
                    intended = Candidate(repeaters, speed, label)
                    if not self._same_candidate(verify.application, intended):
                        raise HomeAssistantError(
                            f"Priority-route verification failed for node {node_id}."
                        )
                    applied.append(node_id)
                    self._update_status(completed_count=index)

                self._clear_pending_plan()
                result = {
                    "status": "applied",
                    "plan_id": plan_id,
                    "applied_node_ids": applied,
                    "applied_count": len(applied),
                    "warnings": warnings,
                    "return_routes_modified": False,
                    "plan_consumed": True,
                }
                self._finish_status(latest_result=result)
                self._update_status(
                    state="applied",
                    phase="completed",
                    plan_id=plan_id,
                    ready_to_apply_count=0,
                    latest_result=result,
                )
                return result
            except asyncio.CancelledError:
                if attempted and snapshots:
                    rollback = await asyncio.shield(
                        self._rollback_attempted(client, snapshots, attempted)
                    )
                    self._clear_pending_plan()
                    self._finish_status(
                        latest_result={
                            "status": "cancelled_rolled_back",
                            "plan_id": plan_id,
                            "rollback": rollback,
                        }
                    )
                else:
                    self._finish_status()
                    if self._pending_apply_plan is plan and not self._plan_expired(plan):
                        self._schedule_plan_expiry(plan)
                raise
            except Exception as err:
                if attempted and snapshots:
                    rollback = await asyncio.shield(
                        self._rollback_attempted(client, snapshots, attempted)
                    )
                    self._clear_pending_plan()
                    status = "rolled_back" if not rollback["failed"] else "rollback_incomplete"
                    result = {
                        "status": status,
                        "plan_id": plan_id,
                        "error": str(err),
                        "attempted_node_ids": attempted,
                        "applied_before_failure_node_ids": applied,
                        "rollback": rollback,
                        "plan_consumed": True,
                    }
                    self._finish_status(latest_result=result)
                    self._update_status(
                        state=status,
                        phase=status,
                        plan_id=plan_id,
                        ready_to_apply_count=0,
                        latest_result=result,
                    )
                    return result

                self._finish_status(
                    latest_result={
                        "status": "apply_not_started",
                        "plan_id": plan_id,
                        "error": str(err),
                    }
                )
                if self._pending_apply_plan is plan and not self._plan_expired(plan):
                    self._schedule_plan_expiry(plan)
                raise
