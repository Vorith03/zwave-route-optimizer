"""v0.7.4 decision/readiness layer for the Z-Wave route optimizer."""

from __future__ import annotations

from typing import Any, Iterator

from homeassistant.exceptions import HomeAssistantError

from .optimizer import (
    Candidate,
    PriorityState,
    RouteOptimizer as BaseRouteOptimizer,
)
from .optimizer_v073 import RouteOptimizer as V073RouteOptimizer


class _AdaptiveCandidateListV074(list[Candidate]):
    """Candidate list with side-effect-free iteration and incumbent stopping.

    v0.7.3 re-ranked inside ``__iter__``. The base optimizer iterates candidates
    before benchmarking to build metadata, so that hook fired before AUTO had a
    measured result and permanently selected the fallback learned-route weight.
    v0.7.4 keeps iteration side-effect free; re-ranking is triggered only after
    the AUTO benchmark actually completes.
    """

    def __init__(
        self,
        values: list[Candidate],
        *,
        optimizer: "RouteOptimizer",
        previous: PriorityState,
        baseline_count: int,
        adaptive_testing: bool,
    ) -> None:
        super().__init__(values)
        self._optimizer = optimizer
        self._previous = previous
        self._baseline_count = baseline_count
        self._adaptive_testing = adaptive_testing

    def __iter__(self) -> Iterator[Candidate]:
        index = 0
        while index < len(self):
            if (
                self._adaptive_testing
                and index >= self._baseline_count
                and self._optimizer._v073_should_stop_for_incumbent(
                    self._previous
                )
            ):
                return
            yield self[index]
            index += 1


class RouteOptimizer(V073RouteOptimizer):
    """v0.7.4 optimizer with measured AUTO re-ranking and apply planning."""

    def _candidate_list(
        self,
        node_id: int,
        controller_id: int,
        graph: dict[int, set[int]],
        nodes: dict[int, Any],
        previous: PriorityState,
        *,
        max_repeaters: int,
        max_candidates: int,
        include_auto: bool,
        adaptive_testing: bool,
    ) -> list[Candidate]:
        """Build candidates without v0.7.3's iterator-time re-ranking hook."""
        values = BaseRouteOptimizer._candidate_list(
            self,
            node_id,
            controller_id,
            graph,
            nodes,
            previous,
            max_repeaters=max_repeaters,
            max_candidates=max_candidates,
            include_auto=include_auto,
            adaptive_testing=adaptive_testing,
        )
        if not adaptive_testing:
            return values

        baseline_count = (1 if previous.application is not None else 0) + (
            1 if include_auto else 0
        )
        candidates = _AdaptiveCandidateListV074(
            values,
            optimizer=self,
            previous=previous,
            baseline_count=baseline_count,
            adaptive_testing=True,
        )

        context = self._v073_context
        if context is not None:
            context["candidate_list"] = candidates
            context["candidate_baseline_count"] = baseline_count
            context["candidate_target_id"] = node_id
            context["candidate_nodes"] = nodes
            context["candidate_controller_id"] = controller_id
            context["reranked_after_auto"] = False

        return candidates

    async def _benchmark(self, *args: Any, **kwargs: Any):
        """Benchmark and re-rank untouched generated routes after measured AUTO."""
        result = await super()._benchmark(*args, **kwargs)
        context = self._v073_context
        if (
            context is None
            or not result.candidate.is_auto
            or context.get("reranked_after_auto")
        ):
            return result

        candidates = context.get("candidate_list")
        baseline_count = context.get("candidate_baseline_count")
        target_id = context.get("candidate_target_id")
        nodes = context.get("candidate_nodes")
        controller_id = context.get("candidate_controller_id")
        if (
            not isinstance(candidates, list)
            or not isinstance(baseline_count, int)
            or not isinstance(target_id, int)
            or not isinstance(nodes, dict)
            or not isinstance(controller_id, int)
        ):
            return result

        weight = self._auto_history_weight(result)
        reranked = self._rerank_generated_candidates(
            list(candidates[baseline_count:]),
            target_id=target_id,
            nodes=nodes,
            controller_id=controller_id,
            learned_route_weight=weight,
        )
        candidates[baseline_count:] = reranked
        context["learned_route_weight"] = weight
        context["reranked_after_auto"] = True
        return result

    @staticmethod
    def _same_route_dict(
        route: dict[str, Any] | None,
        repeaters: list[int],
        speed: int | None,
    ) -> bool:
        """Return whether a serialized application route matches a winner."""
        if not isinstance(route, dict):
            return False
        raw_repeaters = route.get("repeaters")
        if not isinstance(raw_repeaters, list):
            return False
        try:
            existing_repeaters = [int(value) for value in raw_repeaters]
            existing_speed = int(route.get("route_speed"))
        except (TypeError, ValueError):
            return False
        return existing_repeaters == repeaters and existing_speed == speed

    @classmethod
    def _build_apply_plan(cls, response: dict[str, Any]) -> dict[str, Any]:
        """Build the exact forward-write set a future bulk Apply may consume.

        This is intentionally stricter than merely picking the final-pass winner.
        Only exact-stable winners that were clean in every requested pass can
        become write-ready. AUTO, path-stable, dynamic, or unstable outcomes are
        never converted into a new pin by this planning release.
        """
        requested = int(response.get("passes_requested", 1))
        pass_results = response.get("passes")
        stability = response.get("route_stability")
        final_results = response.get("results")
        if not isinstance(pass_results, list):
            pass_results = []
        if not isinstance(stability, dict):
            stability = {}
        if not isinstance(final_results, list):
            final_results = []

        summaries: dict[int, list[dict[str, Any]]] = {}
        for pass_data in pass_results:
            if not isinstance(pass_data, dict):
                continue
            for item in pass_data.get("results", []):
                if not isinstance(item, dict):
                    continue
                node_id = item.get("node_id")
                if isinstance(node_id, int):
                    summaries.setdefault(node_id, []).append(item)

        detailed = {
            item.get("node_id"): item
            for item in final_results
            if isinstance(item, dict) and isinstance(item.get("node_id"), int)
        }

        entries: list[dict[str, Any]] = []
        counts = {
            "ready_to_set": 0,
            "no_change": 0,
            "leave_auto": 0,
            "hold": 0,
        }
        write_operations: list[dict[str, Any]] = []

        for stable in stability.get("nodes", []):
            if not isinstance(stable, dict):
                continue
            node_id = stable.get("node_id")
            if not isinstance(node_id, int):
                continue
            node_summaries = summaries.get(node_id, [])
            full = detailed.get(node_id, {})
            classification = stable.get("classification")
            enough_passes = requested >= 2 and len(node_summaries) == requested
            winners_clean = enough_passes and all(
                item.get("status") == "ok"
                and item.get("failures") == 0
                and item.get("slow_samples") == 0
                for item in node_summaries
            )

            best = full.get("best") if isinstance(full, dict) else None
            if not isinstance(best, dict):
                best = {}
            raw_repeaters = best.get("repeaters")
            best_is_auto = raw_repeaters is None
            repeaters: list[int] | None
            if isinstance(raw_repeaters, list):
                try:
                    repeaters = [int(value) for value in raw_repeaters]
                except (TypeError, ValueError):
                    repeaters = None
            else:
                repeaters = None
            try:
                speed = (
                    int(best.get("route_speed"))
                    if best.get("route_speed") is not None
                    else None
                )
            except (TypeError, ValueError):
                speed = None

            starting = full.get("starting_priority_state")
            application_route = (
                starting.get("application_priority_route")
                if isinstance(starting, dict)
                else None
            )

            action = "hold"
            reason = "winner is not exact-stable across all requested passes"
            write_ready = False

            if not enough_passes:
                reason = "at least two complete passes are required for bulk-write readiness"
            elif classification != "exact_stable":
                reason = f"stability classification is {classification!s}, not exact_stable"
            elif not winners_clean:
                reason = "the winning route was not clean in every pass"
            elif best_is_auto:
                if application_route is None:
                    action = "leave_auto"
                    reason = "AUTO is exact-stable and the node is already unpinned"
                else:
                    reason = (
                        "AUTO is exact-stable but clearing an existing pin remains "
                        "manual/review-only in v0.7.4"
                    )
            elif repeaters is None or speed is None:
                reason = "explicit winner is missing a serializable route"
            elif cls._same_route_dict(application_route, repeaters, speed):
                action = "no_change"
                reason = "exact-stable winner already matches the application priority route"
            else:
                action = "ready_to_set"
                reason = (
                    "exact-stable explicit winner was clean in every pass and differs "
                    "from the current application priority route"
                )
                write_ready = True

            counts[action] += 1
            entry = {
                "node_id": node_id,
                "name": stable.get("name"),
                "classification": classification,
                "confidence": stable.get("confidence"),
                "candidate_agreement": stable.get("candidate_agreement"),
                "physical_path_agreement": stable.get("physical_path_agreement"),
                "action": action,
                "write_ready": write_ready,
                "reason": reason,
                "winner": best.get("route"),
                "repeaters": repeaters,
                "route_speed": speed,
                "route_speed_kbps": best.get("route_speed_kbps"),
            }
            entries.append(entry)
            if write_ready:
                write_operations.append(
                    {
                        "node_id": node_id,
                        "name": stable.get("name"),
                        "operation": "set_application_priority_route",
                        "repeaters": repeaters,
                        "route_speed": speed,
                        "route_speed_kbps": best.get("route_speed_kbps"),
                    }
                )

        return {
            "mode": "preview_only",
            "writes_enabled": False,
            "minimum_passes_required": 2,
            "policy": (
                "exact_stable + clean winner in every pass; AUTO is never converted "
                "to a new pin; ambiguous outcomes are held"
            ),
            "counts": counts,
            "write_ready_node_ids": [item["node_id"] for item in write_operations],
            "write_operations": write_operations,
            "nodes": entries,
        }

    async def optimize_network(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run network dry-run and attach v0.7.4 bulk-write readiness preview."""
        if kwargs.get("apply") or kwargs.get("apply_return_route"):
            raise HomeAssistantError(
                "Whole-network Apply is intentionally disabled in v0.7.4. "
                "Run with both apply toggles off and review apply_plan; v0.8.0 "
                "is intended to consume only the write-ready forward operations."
            )
        response = await super().optimize_network(*args, **kwargs)
        response["apply_plan"] = self._build_apply_plan(response)
        return response
