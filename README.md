# Z-Wave Route Optimizer for Home Assistant

Version: **0.7.2**

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

**Whole-network Apply is intentionally disabled in v0.7.2.**

The action still exposes the apply controls so the intended UX is visible, but turning either write toggle on causes the action to refuse before benchmarking or modifying the network. Whole-network mode is dry-run only for this release.

## v0.7.2 behavior

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
- **Adaptive candidate testing is enabled by default.** Passive Z-Wave JS node statistics (RSSI plus recently observed LWR/NLWR routes and repeater RSSI when available) are used only to prioritize topology-valid candidates; they never hard-filter a route.
- Adaptive execution tests current/AUTO baselines first, then shorter generated routes. A clean direct route at or below 35 ms median / 75 ms worst stops route expansion. If direct is not excellent, up to three distinct RF-ranked one-hop paths are tried before deeper routes; a clean one-hop route at or below 45 ms median / 100 ms worst can stop expansion.
- Disable **Adaptive candidate testing** to benchmark the complete generated candidate set exactly as before.
- Slow-sample detection uses a **device/best-known baseline**, not the candidate's own median: `max(250 ms, best-known device median × 4)`.
- Pathological candidates can be eliminated early after two transaction failures or several clearly baseline-exceeding latency samples.
- If a challenger would replace an existing application priority route, the optimizer performs a fresh **incumbent vs challenger confirmation round** before recommending replacement.
- If that confirmation cannot be completed, replacement is fail-safe: the incumbent is retained when possible, otherwise no replacement recommendation is produced.
- Whole-network skipped-node output includes node names and explicit reasons.
- Whole-network dry runs support 1–10 complete passes in a single action. The neighbor graph is built once, each pass benchmarks every eligible node, and compact per-pass winner summaries are retained for repeatability comparisons. Those summaries also report how many candidates adaptive testing actually measured/skipped. The top-level `results` field remains the full detailed result from the final pass.
- Reverse return-route suggestions are generated and reported in dry-run output.
- Priority SUC return-route assignment is available through the existing single-node optimization action.
- Unsupported return-route getter commands are detected after the first `unknown_command` response and suppressed for the rest of the Home Assistant runtime.

## Live status entity

v0.7.0 added a diagnostic **Status** sensor to the integration. During a run it exposes attributes including:

- running / idle state
- operation type
- current node ID and name
- pass X/Y for multi-pass network runs
- node X/Y
- candidate X/Y
- current route
- completed count
- elapsed time
- latest completed result

The sensor is event-driven; it updates as optimizer progress changes.

## Return-route limitation

Z-Wave cannot reliably read the actual priority SUC return route back from the end node. Some Z-Wave JS Server connections also reject the available getter command forms with `unknown_command`.

Therefore:

- reverse-topology validation is advisory, not proof of RF symmetry;
- a successful return-route assignment cannot always be independently verified;
- return-route writes are not transactionally reversible in the same way as forward application-priority routes.

The integration reports this limitation rather than repeatedly issuing unsupported getter calls.

## Dry-run caveat

Dry-run restores the starting **application priority-route configuration**, but benchmarking traffic can still influence learned LWR/NLWR/controller routing state. There is no portable API to snapshot and restore every learned routing heuristic.
