"""v0.8.1 physical-consensus apply policy for Z-Wave Route Optimizer."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .optimizer_v080 import RouteOptimizer as V080RouteOptimizer


class RouteOptimizer(V080RouteOptimizer):
    """v0.8.1 optimizer with conservative physical-path consensus promotion."""

    @staticmethod
    def _summary_explicit_candidate(
        summary: dict[str, Any],
    ) -> tuple[tuple[int, ...], int] | None:
        """Return an explicit compact winner signature, excluding AUTO."""
        if summary.get("best_is_auto"):
            return None
        repeaters = summary.get("best_repeaters")
        speed = summary.get("best_route_speed")
        if not isinstance(repeaters, list) or speed is None:
            return None
        try:
            return tuple(int(value) for value in repeaters), int(speed)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _starting_route_preview(full: dict[str, Any]) -> dict[str, Any]:
        """Return a visualization-friendly snapshot of the starting route state."""
        starting = full.get("starting_priority_state")
        if not isinstance(starting, dict):
            return {
                "kind": "unknown",
                "repeaters": None,
                "route_speed": None,
                "route_speed_kbps": None,
                "route_kind": None,
            }

        application = starting.get("application_priority_route")
        if isinstance(application, dict):
            repeaters = application.get("repeaters")
            speed = application.get("route_speed")
            return {
                "kind": "application",
                "repeaters": list(repeaters) if isinstance(repeaters, list) else None,
                "route_speed": speed,
                "route_speed_kbps": application.get("route_speed_kbps"),
                "route_kind": starting.get("route_kind"),
            }

        effective = starting.get("effective_route")
        if not isinstance(effective, dict):
            return {
                "kind": "auto",
                "repeaters": None,
                "route_speed": None,
                "route_speed_kbps": None,
                "route_kind": starting.get("route_kind"),
            }
        repeaters = effective.get("repeaters")
        speed = effective.get("routeSpeed")
        try:
            speed_int = int(speed) if speed is not None else None
        except (TypeError, ValueError):
            speed_int = None
        return {
            "kind": "learned",
            "repeaters": list(repeaters) if isinstance(repeaters, list) else None,
            "route_speed": speed_int,
            "route_speed_kbps": RouteOptimizer._route_speed_kbps(speed_int),
            "route_kind": effective.get("routeKind", starting.get("route_kind")),
        }

    @classmethod
    def _build_apply_plan(cls, response: dict[str, Any]) -> dict[str, Any]:
        """Build the staged forward-write set using exact and physical consensus.

        Exact-stable behavior from v0.8.0 is preserved. v0.8.1 additionally
        recognizes a three-or-more-pass physical-path consensus. Stable direct
        AUTO/explicit mixtures are left unpinned. A repeated repeater path can
        become write-ready only when one exact explicit route wins a strict
        majority of passes and every pass winner on that physical path is clean.
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
            try:
                pass_index = int(pass_data.get("pass_index", 0))
            except (TypeError, ValueError):
                pass_index = 0
            for raw in pass_data.get("results", []):
                if not isinstance(raw, dict):
                    continue
                node_id = raw.get("node_id")
                if not isinstance(node_id, int):
                    continue
                item = dict(raw)
                item["pass_index"] = pass_index
                summaries.setdefault(node_id, []).append(item)

        for values in summaries.values():
            values.sort(key=lambda item: int(item.get("pass_index", 0)))

        detailed = {
            item.get("node_id"): item
            for item in final_results
            if isinstance(item, dict) and isinstance(item.get("node_id"), int)
        }

        entries: list[dict[str, Any]] = []
        route_changes: list[dict[str, Any]] = []
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
            final_repeaters: list[int] | None = None
            if isinstance(raw_repeaters, list):
                try:
                    final_repeaters = [int(value) for value in raw_repeaters]
                except (TypeError, ValueError):
                    final_repeaters = None
            try:
                final_speed = (
                    int(best.get("route_speed"))
                    if best.get("route_speed") is not None
                    else None
                )
            except (TypeError, ValueError):
                final_speed = None

            starting = full.get("starting_priority_state")
            application_route = (
                starting.get("application_priority_route")
                if isinstance(starting, dict)
                else None
            )

            physical = [cls._summary_physical_path(item) for item in node_summaries]
            physical_paths = [path for path, _ in physical]
            known_physical = len(physical) == requested and all(
                path is not None for path in physical_paths
            )
            physical_path_consensus: tuple[int, ...] | None = None
            if known_physical and len(set(physical_paths)) == 1:
                physical_path_consensus = physical_paths[0]

            speed_counts: Counter[int] = Counter(
                speed
                for path, speed in physical
                if path is not None and speed is not None
            )
            consensus_speed: int | None = None
            consensus_speed_count = 0
            if speed_counts:
                consensus_speed, consensus_speed_count = max(
                    speed_counts.items(), key=lambda item: (item[1], item[0])
                )

            explicit_counts: Counter[tuple[tuple[int, ...], int]] = Counter()
            for item in node_summaries:
                candidate = cls._summary_explicit_candidate(item)
                if candidate is None:
                    continue
                if physical_path_consensus is not None and candidate[0] != physical_path_consensus:
                    continue
                explicit_counts[candidate] += 1

            explicit_candidate: tuple[tuple[int, ...], int] | None = None
            explicit_wins = 0
            if explicit_counts:
                explicit_candidate, explicit_wins = max(
                    explicit_counts.items(),
                    key=lambda item: (item[1], item[0][1], item[0][0]),
                )
            explicit_majority_required = requested // 2 + 1

            action = "hold"
            reason = "route did not meet the automatic bulk-write safety policy"
            write_ready = False
            proposed_repeaters = final_repeaters
            proposed_speed = final_speed
            winner_label = best.get("route")

            if not enough_passes:
                reason = "at least two complete passes are required for bulk-write readiness"
            elif classification == "exact_stable":
                if not winners_clean:
                    reason = "the winning route was not clean in every pass"
                elif best_is_auto:
                    if application_route is None:
                        action = "leave_auto"
                        reason = "AUTO is exact-stable and the node is already unpinned"
                    else:
                        reason = (
                            "AUTO is exact-stable but clearing an existing pin remains "
                            "manual/review-only"
                        )
                elif final_repeaters is None or final_speed is None:
                    reason = "explicit winner is missing a serializable route"
                elif cls._same_route_dict(application_route, final_repeaters, final_speed):
                    action = "no_change"
                    reason = "exact-stable winner already matches the application priority route"
                else:
                    action = "ready_to_set"
                    reason = (
                        "exact-stable explicit winner was clean in every pass and differs "
                        "from the current application priority route"
                    )
                    write_ready = True
            elif classification == "path_stable":
                if requested < 3:
                    reason = (
                        "path-stable outcomes require at least three passes before "
                        "physical consensus can affect Apply"
                    )
                elif physical_path_consensus is None:
                    reason = "physical path was not known and identical in every pass"
                elif not winners_clean:
                    reason = "the consensus physical path was not clean in every pass"
                elif len(physical_path_consensus) == 0:
                    if application_route is None:
                        action = "leave_auto"
                        reason = (
                            "direct physical path was stable in every pass; AUTO already "
                            "reaches it, so creating a pin would add unnecessary state"
                        )
                    elif consensus_speed is not None and cls._same_route_dict(
                        application_route, [], consensus_speed
                    ):
                        action = "no_change"
                        reason = (
                            "direct physical path was stable in every pass and the existing "
                            "application priority route already matches it"
                        )
                    else:
                        reason = (
                            "direct physical path is stable but an existing application pin "
                            "prevents automatic unpinning"
                        )
                elif explicit_candidate is None:
                    reason = "stable repeater path had no explicit winning candidate"
                elif explicit_wins < explicit_majority_required:
                    reason = (
                        f"stable repeater path had only {explicit_wins}/{requested} explicit "
                        f"wins; at least {explicit_majority_required}/{requested} are required"
                    )
                else:
                    proposed_repeaters = list(explicit_candidate[0])
                    proposed_speed = explicit_candidate[1]
                    winner_label = cls._route_label(
                        explicit_candidate[0], explicit_candidate[1]
                    )
                    if cls._same_route_dict(
                        application_route, proposed_repeaters, proposed_speed
                    ):
                        action = "no_change"
                        reason = (
                            "three-pass physical consensus and explicit majority agree with "
                            "the existing application priority route"
                        )
                    else:
                        action = "ready_to_set"
                        reason = (
                            f"physical repeater path was stable {requested}/{requested} passes; "
                            f"{winner_label} won {explicit_wins}/{requested} passes with clean "
                            "winners, so the consensus route is write-ready"
                        )
                        write_ready = True
            else:
                reason = f"stability classification is {classification!s}; automatic Apply holds it"

            counts[action] += 1
            route_speed_kbps = cls._route_speed_kbps(proposed_speed)
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
                "winner": winner_label,
                "repeaters": proposed_repeaters,
                "route_speed": proposed_speed,
                "route_speed_kbps": route_speed_kbps,
                "explicit_wins": explicit_wins,
                "explicit_wins_required": explicit_majority_required,
            }
            entries.append(entry)

            pass_preview: list[dict[str, Any]] = []
            for item, (path, physical_speed) in zip(node_summaries, physical):
                explicit = cls._summary_explicit_candidate(item)
                pass_preview.append(
                    {
                        "pass_index": item.get("pass_index"),
                        "winner": item.get("best_route"),
                        "winner_is_auto": bool(item.get("best_is_auto")),
                        "winner_repeaters": item.get("best_repeaters"),
                        "winner_route_speed": item.get("best_route_speed"),
                        "physical_repeaters": None if path is None else list(path),
                        "physical_route_speed": physical_speed,
                        "median_ms": item.get("median_ms"),
                        "failures": item.get("failures"),
                        "slow_samples": item.get("slow_samples"),
                    }
                )

            route_changes.append(
                {
                    "node_id": node_id,
                    "name": stable.get("name"),
                    "action": action,
                    "reason": reason,
                    "current": cls._starting_route_preview(full),
                    "consensus": {
                        "repeaters": (
                            None
                            if physical_path_consensus is None
                            else list(physical_path_consensus)
                        ),
                        "route_speed": consensus_speed,
                        "route_speed_kbps": cls._route_speed_kbps(consensus_speed),
                        "physical_path_agreement": stable.get(
                            "physical_path_agreement"
                        ),
                        "physical_speed_agreement": (
                            None
                            if consensus_speed is None
                            else f"{consensus_speed_count}/{requested}"
                        ),
                        "explicit_candidate": (
                            None
                            if explicit_candidate is None
                            else {
                                "route": cls._route_label(
                                    explicit_candidate[0], explicit_candidate[1]
                                ),
                                "repeaters": list(explicit_candidate[0]),
                                "route_speed": explicit_candidate[1],
                                "route_speed_kbps": cls._route_speed_kbps(
                                    explicit_candidate[1]
                                ),
                                "wins": explicit_wins,
                                "wins_required": explicit_majority_required,
                            }
                        ),
                    },
                    "passes": pass_preview,
                    "proposed": {
                        "operation": (
                            "set_application_priority_route" if write_ready else None
                        ),
                        "repeaters": proposed_repeaters if write_ready else None,
                        "route_speed": proposed_speed if write_ready else None,
                        "route_speed_kbps": route_speed_kbps if write_ready else None,
                    },
                }
            )

            if write_ready:
                write_operations.append(
                    {
                        "node_id": node_id,
                        "name": stable.get("name"),
                        "operation": "set_application_priority_route",
                        "repeaters": proposed_repeaters,
                        "route_speed": proposed_speed,
                        "route_speed_kbps": route_speed_kbps,
                    }
                )

        return {
            "mode": "preview_only",
            "writes_enabled": False,
            "minimum_passes_required": 2,
            "minimum_passes_for_physical_consensus": 3,
            "policy": (
                "exact-stable clean explicit winners remain eligible; with at least "
                "three passes, a clean all-pass physical repeater consensus may also "
                "be pinned when one explicit candidate wins a strict majority; stable "
                "direct paths are left on AUTO; ambiguous outcomes are held"
            ),
            "counts": counts,
            "write_ready_node_ids": [item["node_id"] for item in write_operations],
            "write_operations": write_operations,
            "nodes": entries,
            "route_changes": route_changes,
        }
