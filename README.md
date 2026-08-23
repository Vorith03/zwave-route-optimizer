# Z-Wave Route Optimizer for Home Assistant

Version: **0.7.4**

A manually invoked Home Assistant custom integration for benchmarking and optionally pinning **classic Z-Wave application priority routes** using Home Assistant's existing Z-Wave JS connection.

It does **not** run automatically, schedule optimizations, open a second Z-Wave JS connection, or optimize in the background.

## Install

Copy `custom_components/zwave_route_optimizer/` into `/config/custom_components/zwave_route_optimizer/`, restart Home Assistant, then add **Z-Wave Route Optimizer** from **Settings → Devices & services**.

## Actions

In **Developer Tools → Actions**:

- **Z-Wave Route Optimizer: Optimize one Z-Wave node**
- **Z-Wave Route Optimizer: Optimize Z-Wave network**

Both default to dry-run behavior.

### Single-node writes

The single-node action contains two explicit write toggles:

- **Apply best forward route**
- **Apply suggested return route**

The return-route suggestion is the reversed winning forward route with reverse-topology validation. A topology-unvalidated return route can be explicitly allowed.

If the winning forward route differs from the currently pinned route, applying only the return route is blocked to avoid intentionally creating a mismatched forward/return pair.

### Whole-network writes

**Whole-network Apply is intentionally disabled in v0.7.4.**

This release adds an `apply_plan` preview to whole-network dry-run results. It is the proposed safety gate for v0.8.0 bulk forward Apply: only an explicit route that is `exact_stable`, clean in every requested pass, and different from the current application priority route is marked `ready_to_set`.

Stable AUTO on an already-unpinned node is `leave_auto`; an already-matching pin is `no_change`; path-stable, AUTO-dynamic, unstable, incomplete, and existing-pin-clearing cases remain `hold`.

## v0.7.4 behavior

- Classic Z-Wave only; Z-Wave Long Range is skipped.
- Ordinary sleeping battery nodes are skipped; FLiRS targets remain supported.
- FLiRS probes use an unscored wake-up phase followed by tightly grouped scored probes.
- The final repeater into a FLiRS target must support beaming.
- Route speeds use Serial API enums: `1=9.6k`, `2=40k`, `3=100k`.
- Up to four repeaters are supported; the default maximum is two.
- Existing application priority routes are distinguished from learned LWR/NLWR routes.
- Every experimental forward route is restored before moving on.
- Cancellation shields restoration of the starting application priority state.
- Candidate generation is hop-diverse: direct, 1-hop, 2-hop, etc. receive independent path discovery and round-robin candidate slots so shallow routes cannot starve multi-hop routes.
- **Adaptive candidate testing is enabled by default.** Passive Z-Wave JS node statistics are used only to prioritize topology-valid candidates; they never hard-filter a route.
- Learned LWR/NLWR history is conditional. v0.7.4 fixes the v0.7.3 lifecycle bug so generated candidates are re-ranked **after the measured AUTO benchmark completes**, rather than during a metadata-only candidate iteration. Clean/fast AUTO gets full learned-history weight; poor AUTO gets zero learned-history weight; missing AUTO retains only the weak prior.
- Adaptive execution tests current/AUTO baselines first, then shorter generated routes. A clean direct route at or below 35 ms median / 75 ms worst stops route expansion. Otherwise up to three RF-ranked one-hop paths are tried before deeper routes; a clean one-hop route at or below 45 ms median / 100 ms worst can stop expansion.
- A clean existing application pin at or below 60 ms median / 150 ms worst can stop after three RF-ranked challenger paths if the incumbent still wins under the configured minimum-improvement hysteresis.
- Disable **Adaptive candidate testing** to benchmark the complete generated candidate set exactly as before.
- Slow-sample detection uses a **device/best-known baseline**, not the candidate's own median: `max(250 ms, best-known median × 4)`.
- Pathological candidates can be eliminated early after two transaction failures or several clearly baseline-exceeding latency samples.
- If a challenger would replace an existing application priority route, the optimizer performs a fresh **incumbent vs challenger confirmation round** before recommending replacement.
- If that confirmation cannot be completed, replacement is fail-safe: the incumbent is retained when possible, otherwise no replacement recommendation is produced.
- Whole-network skipped-node output includes node names and explicit reasons.
- Whole-network dry runs support 1–10 complete passes in a single action. The neighbor graph is built once, each pass benchmarks every eligible node, and compact per-pass winner summaries are retained for repeatability comparisons. The top-level `results` field remains the full detailed result from the final pass.
- Multi-pass responses include `route_stability`, classifying each node as `exact_stable`, `path_stable`, `auto_policy_stable_path_dynamic`, `unstable`, `single_pass`, or `incomplete`.
- Multi-pass responses also include `apply_plan`, with `ready_to_set`, `no_change`, `leave_auto`, and `hold` decisions plus the exact proposed forward write operations. v0.7.4 never executes that plan.
- Reverse return-route suggestions are generated and reported in dry-run output.
- Priority SUC return-route assignment remains available through the existing single-node optimization action.
- Unsupported return-route getter commands are detected after the first `unknown_command` response and suppressed for the rest of the Home Assistant runtime.

## Live status entity

The diagnostic **Status** sensor exposes running/idle state, operation type, current node, pass X/Y, node X/Y, candidate X/Y, current route, completed count, elapsed time, and the latest completed result. It is event-driven.

## Return-route limitation

Z-Wave cannot reliably read the actual priority SUC return route back from the end node. Some Z-Wave JS Server connections also reject the available getter command forms with `unknown_command`.

Therefore reverse-topology validation is advisory, a successful return-route assignment cannot always be independently verified, and return-route writes are not transactionally reversible in the same way as forward application-priority routes. Bulk return-route Apply remains outside the v0.8.0 forward-write plan.

## Dry-run caveat

Dry-run restores the starting **application priority-route configuration**, but benchmarking traffic can still influence learned LWR/NLWR/controller routing state. There is no portable API to snapshot and restore every learned routing heuristic.
