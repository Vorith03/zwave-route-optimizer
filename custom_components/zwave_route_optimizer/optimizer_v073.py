"""v0.7.3 adaptive/stability layer for the Z-Wave route optimizer."""

from __future__ import annotations

from typing import Any, Iterator

from homeassistant.exceptions import HomeAssistantError

from .optimizer import (
    ROUTE_KIND_APPLICATION,
    BenchResult,
    Candidate,
    PriorityState,
    RouteOptimizer as BaseRouteOptimizer,
)

ADAPTIVE_INCUMBENT_MEDIAN_MS = 60.0
ADAPTIVE_INCUMBENT_WORST_MS = 150.0
ADAPTIVE_INCUMBENT_CHALLENGER_PATHS = 3
ADAPTIVE_LEARNED_ROUTE_WEAK_WEIGHT = 0.20
ADAPTIVE_LEARNED_ROUTE_STRONG_WEIGHT = 1.0
ADAPTIVE_AUTO_GOOD_MEDIAN_MS = 45.0
ADAPTIVE_AUTO_GOOD_WORST_MS = 100.0


class _AdaptiveCandidateList(list[Candidate]):
    """A candidate list that can re-rank/stop between benchmark iterations."""

    def __init__(
        self,
        values: list[Candidate],
        *,
        optimizer: "RouteOptimizer",
        previous: PriorityState,
        target_id: int,
        nodes: dict[int, Any],
        controller_id: int,
        baseline_count: int,
        adaptive_testing: bool,
    ) -> None:
        super().__init__(values)
        self._optimizer = optimizer
        self._previous = previous
        self._target_id = target_id
        self._nodes = nodes
        self._controller_id = controller_id
        self._baseline_count = baseline_count
        self._adaptive_testing = adaptive_testing
        self._reranked = False

    def __iter__(self) -> Iterator[Candidate]:
        index = 0
        while index < len(self):
            if (
                self._adaptive_testing
                and not self._reranked
                and index == self._baseline_count
            ):
                self._optimizer._v073_rerank_remaining(
                    self,
                    baseline_count=self._baseline_count,
                    target_id=self._target_id,
                    nodes=self._nodes,
                    controller_id=self._controller_id,
                )
                self._reranked = True

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


class RouteOptimizer(BaseRouteOptimizer):
    """v0.7.3 optimizer with conditional RF history and pass consensus."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._v073_context: dict[str, Any] | None = None

    @classmethod
    def _rf_path_hint(
        cls,
        repeaters: tuple[int, ...],
        target_id: int,
        nodes: dict[int, Any],
        controller_id: int,
        *,
        learned_route_weight: float = ADAPTIVE_LEARNED_ROUTE_WEAK_WEIGHT,
    ) -> tuple[float, tuple[str, ...]]:
        """Rank a path while keeping learned-route evidence conditional."""
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
                score += exact_bonus * learned_route_weight
                evidence.append(
                    f"matches_{route_name}(weight={learned_route_weight:g})"
                )
            elif repeaters and any(value in learned for value in repeaters):
                overlap = len(set(repeaters) & set(learned))
                score += 12.0 * overlap * learned_route_weight
                evidence.append(
                    f"overlaps_{route_name}={overlap}(weight={learned_route_weight:g})"
                )

            if learned != repeaters:
                continue
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
                score += (
                    max(0.0, min(35.0, weakest + 110.0))
                    * learned_route_weight
                )
                evidence.append(f"{route_name}_weakest_rssi={weakest:g}dBm")

        return score, tuple(evidence)

    @classmethod
    def _auto_history_weight(cls, auto_result: BenchResult | None) -> float:
        """Return how much learned LWR/NLWR history should affect ordering."""
        if auto_result is None:
            return ADAPTIVE_LEARNED_ROUTE_WEAK_WEIGHT
        if (
            cls._benchmark_is_clean(auto_result)
            and auto_result.median_ms <= ADAPTIVE_AUTO_GOOD_MEDIAN_MS
            and auto_result.worst_ms <= ADAPTIVE_AUTO_GOOD_WORST_MS
        ):
            return ADAPTIVE_LEARNED_ROUTE_STRONG_WEIGHT
        return 0.0

    @classmethod
    def _rerank_generated_candidates(
        cls,
        candidates: list[Candidate],
        *,
        target_id: int,
        nodes: dict[int, Any],
        controller_id: int,
        learned_route_weight: float,
    ) -> list[Candidate]:
        """Refresh RF scores after baseline testing, preserving path diversity."""
        grouped: dict[int, dict[tuple[int, ...], list[Candidate]]] = {}
        for candidate in candidates:
            if candidate.repeaters is None:
                continue
            rf_score, rf_evidence = cls._rf_path_hint(
                candidate.repeaters,
                target_id,
                nodes,
                controller_id,
                learned_route_weight=learned_route_weight,
            )
            refreshed = Candidate(
                candidate.repeaters,
                candidate.speed,
                candidate.label,
                rf_score,
                rf_evidence,
            )
            grouped.setdefault(len(candidate.repeaters), {}).setdefault(
                candidate.repeaters, []
            ).append(refreshed)

        ordered: list[Candidate] = []
        for depth in sorted(grouped):
            path_variants = list(grouped[depth].values())
            path_variants.sort(
                key=lambda variants: (
                    -variants[0].rf_score,
                    variants[0].repeaters or (),
                )
            )
            max_variants = max(len(variants) for variants in path_variants)
            for variant_index in range(max_variants):
                for variants in path_variants:
                    if variant_index < len(variants):
                        ordered.append(variants[variant_index])
        return ordered

    def _v073_rerank_remaining(
        self,
        candidates: list[Candidate],
        *,
        baseline_count: int,
        target_id: int,
        nodes: dict[int, Any],
        controller_id: int,
    ) -> None:
        context = self._v073_context
        if context is None:
            return
        benchmarks: list[BenchResult] = context["benchmarks"]
        auto_result = next(
            (result for result in benchmarks if result.candidate.is_auto), None
        )
        weight = self._auto_history_weight(auto_result)
        context["learned_route_weight"] = weight
        reranked = self._rerank_generated_candidates(
            list(candidates[baseline_count:]),
            target_id=target_id,
            nodes=nodes,
            controller_id=controller_id,
            learned_route_weight=weight,
        )
        candidates[baseline_count:] = reranked

    @classmethod
    def _excellent_incumbent(cls, result: BenchResult) -> bool:
        return (
            cls._benchmark_is_clean(result)
            and result.median_ms <= ADAPTIVE_INCUMBENT_MEDIAN_MS
            and result.worst_ms <= ADAPTIVE_INCUMBENT_WORST_MS
        )

    def _v073_should_stop_for_incumbent(self, previous: PriorityState) -> bool:
        context = self._v073_context
        incumbent = previous.application
        if context is None or incumbent is None or context.get("stop_reason"):
            return False

        benchmarks: list[BenchResult] = context["benchmarks"]
        challenger_paths = {
            result.candidate.repeaters
            for result in benchmarks
            if not result.candidate.is_auto
            and not self._same_candidate(result.candidate, incumbent)
            and result.candidate.repeaters is not None
        }
        if len(challenger_paths) < ADAPTIVE_INCUMBENT_CHALLENGER_PATHS:
            return False

        incumbent_result = next(
            (
                result
                for result in benchmarks
                if self._same_candidate(result.candidate, incumbent)
            ),
            None,
        )
        if incumbent_result is None or not self._excellent_incumbent(incumbent_result):
            return False

        provisional = self._choose_winner(
            benchmarks, previous, float(context["min_improvement"])
        )
        if not self._same_candidate(provisional.candidate, incumbent):
            return False

        context["stop_reason"] = (
            "clean incumbent retained after "
            f"{len(challenger_paths)} RF-ranked challenger paths"
        )
        return True

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
        values = super()._candidate_list(
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
        return _AdaptiveCandidateList(
            values,
            optimizer=self,
            previous=previous,
            target_id=node_id,
            nodes=nodes,
            controller_id=controller_id,
            baseline_count=baseline_count,
            adaptive_testing=True,
        )

    async def _benchmark(self, *args: Any, **kwargs: Any) -> BenchResult:
        result = await super()._benchmark(*args, **kwargs)
        if self._v073_context is not None:
            self._v073_context["benchmarks"].append(result)
        return result

    async def _optimize_one(self, *args: Any, **kwargs: Any):
        """Run the base transaction-safe optimizer with v0.7.3 iteration hooks."""
        previous_context = self._v073_context
        context: dict[str, Any] = {
            "benchmarks": [],
            "min_improvement": kwargs.get("min_improvement", 15.0),
            "learned_route_weight": ADAPTIVE_LEARNED_ROUTE_WEAK_WEIGHT,
            "stop_reason": None,
        }
        self._v073_context = context
        try:
            result, winner, previous = await super()._optimize_one(*args, **kwargs)
            strategy = result.get("candidate_strategy")
            if isinstance(strategy, dict):
                strategy["learned_route_policy"] = (
                    "strong_only_when_auto_is_clean_and_fast"
                )
                strategy["learned_route_weight_initial"] = (
                    ADAPTIVE_LEARNED_ROUTE_WEAK_WEIGHT
                )
                strategy["learned_route_weight_after_baseline"] = context[
                    "learned_route_weight"
                ]
                strategy["incumbent_short_circuit"] = {
                    "challenger_paths_before_decision": (
                        ADAPTIVE_INCUMBENT_CHALLENGER_PATHS
                    ),
                    "incumbent_median_ms": ADAPTIVE_INCUMBENT_MEDIAN_MS,
                    "incumbent_worst_ms": ADAPTIVE_INCUMBENT_WORST_MS,
                }
                if context.get("stop_reason"):
                    strategy["stop_reason"] = context["stop_reason"]
            return result, winner, previous
        finally:
            self._v073_context = previous_context

    @staticmethod
    def _compact_latest_result(result: dict[str, Any]) -> dict[str, Any]:
        compact = BaseRouteOptimizer._compact_latest_result(result)
        best = result.get("best")
        if isinstance(best, dict):
            compact["best_repeaters"] = best.get("repeaters")
            compact["best_route_speed"] = best.get("route_speed")
            compact["best_route_speed_kbps"] = best.get("route_speed_kbps")
            compact["best_is_auto"] = best.get("repeaters") is None
        starting = result.get("starting_priority_state")
        if isinstance(starting, dict):
            effective = starting.get("effective_route")
            if isinstance(effective, dict):
                compact["starting_effective_repeaters"] = effective.get("repeaters")
                compact["starting_effective_route_speed"] = effective.get(
                    "routeSpeed"
                )
                compact["starting_effective_route_kind"] = effective.get("routeKind")
        return compact

    @staticmethod
    def _summary_physical_path(
        summary: dict[str, Any],
    ) -> tuple[tuple[int, ...] | None, int | None]:
        """Normalize explicit winners and trustworthy AUTO starting paths."""
        if summary.get("best_is_auto"):
            if summary.get("starting_effective_route_kind") == ROUTE_KIND_APPLICATION:
                return None, None
            repeaters = summary.get("starting_effective_repeaters")
            speed = summary.get("starting_effective_route_speed")
        else:
            repeaters = summary.get("best_repeaters")
            speed = summary.get("best_route_speed")
        if not isinstance(repeaters, list):
            return None, None
        try:
            path = tuple(int(value) for value in repeaters)
        except (TypeError, ValueError):
            return None, None
        try:
            speed_int = int(speed) if speed is not None else None
        except (TypeError, ValueError):
            speed_int = None
        return path, speed_int

    @classmethod
    def _build_route_stability(
        cls,
        pass_results: list[dict[str, Any]],
        expected_passes: int,
    ) -> dict[str, Any]:
        """Classify exact winner and physical-path repeatability across passes."""
        by_node: dict[int, list[dict[str, Any]]] = {}
        for pass_data in pass_results:
            pass_index = int(pass_data.get("pass_index", 0))
            for summary in pass_data.get("results", []):
                if not isinstance(summary, dict) or summary.get("status") != "ok":
                    continue
                node_id = summary.get("node_id")
                if not isinstance(node_id, int) or not summary.get("best_route"):
                    continue
                item = dict(summary)
                item["pass_index"] = pass_index
                by_node.setdefault(node_id, []).append(item)

        nodes: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for node_id in sorted(by_node):
            winners = sorted(by_node[node_id], key=lambda item: item["pass_index"])
            exact = [
                (
                    bool(item.get("best_is_auto")),
                    None
                    if item.get("best_is_auto")
                    else tuple(item.get("best_repeaters") or ()),
                    item.get("best_route_speed"),
                )
                for item in winners
            ]
            physical = [cls._summary_physical_path(item) for item in winners]
            paths = [path for path, _ in physical]
            known_paths = [path for path in paths if path is not None]
            exact_stable = len(set(exact)) == 1
            path_stable = (
                len(known_paths) == len(winners) and len(set(known_paths)) == 1
            )
            all_auto = all(bool(item.get("best_is_auto")) for item in winners)

            if len(winners) < expected_passes:
                classification, confidence = "incomplete", "insufficient"
            elif expected_passes <= 1:
                classification, confidence = "single_pass", "insufficient"
            elif exact_stable and (not all_auto or path_stable):
                classification, confidence = "exact_stable", "strong"
            elif path_stable:
                classification, confidence = "path_stable", "moderate"
            elif exact_stable and all_auto:
                classification, confidence = (
                    "auto_policy_stable_path_dynamic",
                    "low",
                )
            else:
                classification, confidence = "unstable", "low"
            counts[classification] = counts.get(classification, 0) + 1

            path_counts: dict[tuple[int, ...] | None, int] = {}
            for path in paths:
                path_counts[path] = path_counts.get(path, 0) + 1
            top_path, top_path_count = max(
                path_counts.items(), key=lambda item: item[1]
            )
            exact_counts: dict[tuple[Any, ...], int] = {}
            for signature in exact:
                exact_counts[signature] = exact_counts.get(signature, 0) + 1
            top_exact_count = max(exact_counts.values())

            nodes.append(
                {
                    "node_id": node_id,
                    "name": winners[0].get("name"),
                    "classification": classification,
                    "confidence": confidence,
                    "candidate_agreement": f"{top_exact_count}/{expected_passes}",
                    "physical_path_agreement": f"{top_path_count}/{expected_passes}",
                    "consensus_path": None if top_path is None else list(top_path),
                    "pass_winners": [
                        {
                            "pass_index": item["pass_index"],
                            "route": item.get("best_route"),
                            "median_ms": item.get("median_ms"),
                        }
                        for item in winners
                    ],
                }
            )

        return {
            "passes": expected_passes,
            "classification_counts": counts,
            "nodes": nodes,
        }

    async def optimize_network(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run network optimization and attach v0.7.3 stability analysis."""
        if kwargs.get("apply") or kwargs.get("apply_return_route"):
            raise HomeAssistantError(
                "Whole-network Apply is intentionally disabled in v0.7.3. "
                "Run Optimize Z-Wave network with both apply toggles off; use "
                "single-node optimization for deliberate route writes."
            )
        response = await super().optimize_network(*args, **kwargs)
        response["route_stability"] = self._build_route_stability(
            response.get("passes", []), int(response.get("passes_requested", 1))
        )
        return response
