# Adversarial Review

This review was performed after the initial implementation, followed by fixes, then
a second adversarial pass and another set of fixes.

## Pass 1 — state preservation and transactional safety

### 1. Learned routes could have been mistaken for pinned routes

**Problem:** `controller.get_priority_route` can report an effective route whose
`routeKind` is LWR, NLWR, or Application. Treating every returned route as an
application-defined priority route would risk converting a learned LWR/NLWR into a
persistent pin during restoration.

**Fix:** The optimizer now recognizes `RouteKind.Application` explicitly. It
snapshots and restores only application-defined priority-route state. LWR/NLWR are
kept as diagnostic information and are never converted into a pin.

### 2. Whole-network Apply was initially non-atomic

**Problem:** If routes were permanently applied node by node and a later apply
failed, the mesh could be left half changed.

**Fix:** Whole-network mode benchmarks every node while restoring its starting
application-priority state. Permanent changes are deferred until the entire scan
finishes. Final Apply is transactional: if any commit fails or the action is
cancelled, attempted nodes are rolled back in reverse order.

### 3. Existing pinned-route baseline could be contaminated by AUTO testing

**Problem:** Removing the pin to test AUTO can change the controller's learned route
state before the original pinned route is measured.

**Fix:** An existing application priority route is benchmarked first, before AUTO or
generated candidates.

## Pass 2 — ranking, cancellation, compatibility, and failure ambiguity

### 1. A very fast route with a failed sample could beat a slower reliable route

**Problem:** A scalar latency score with a failure penalty is still capable of
ranking a flaky route above a reliable route in extreme cases.

**Fix:** Winner selection is lexicographic: failure count first, then p95 latency,
median latency, then the secondary score. Hysteresis is only allowed between routes
with the same failure count.

### 2. Cancellation could interrupt per-node restoration

**Problem:** Cancelling a Home Assistant action while a node was on an experimental
route could interrupt the `finally` block's restore await.

**Fix:** Restoration is now protected with `asyncio.shield`.

### 3. Protocol and data-rate representation could vary by library/schema version

**Problem:** Assuming raw integer protocol/data-rate representations creates
unnecessary coupling to one model serialization.

**Fix:** Classic-vs-LR and route-speed normalization now accept both raw integer and
enum-like values.

### 4. Neighbor refresh reached through an internal controller client reference

**Problem:** `controller.client` is more implementation-dependent than the live
client Home Assistant already exposes.

**Fix:** The integration now passes HA's known Z-Wave JS client explicitly to
topology refresh code.

### 5. Optional neighbor rediscovery could generate an unnecessarily sharp traffic burst

**Problem:** A dense mesh plus immediate rediscovery calls could put avoidable load
on the RF network.

**Fix:** Active rediscovery remains opt-in and is paced by 0.5 seconds between
routing nodes.

### 6. A failed restore was treated like an ordinary per-node error

**Problem:** Continuing a whole-network scan after a starting route could not be
restored means the network state is no longer known.

**Fix:** Restoration failure is now fatal. Whole-network optimization stops
immediately rather than continuing experimentation.

### 7. A route change might take effect even if the command response is lost

**Problem:** If `setPriorityRoute` changes the controller state but the response is
lost, the call looks failed even though the route may have changed. A rollback list
containing only successful responses would miss that node.

**Fix:** Whole-network commits add a node to the *attempted* set before awaiting the
apply call, and rollback all attempted nodes on failure. Single-node final Apply is
transactional as well.

## Validation performed

- Python syntax compilation for every integration module.
- JSON parsing for manifest and translation/string files.
- YAML parsing for `services.yaml`.
- Eight isolated logic tests:
  - RouteKind/Application discrimination
  - classic vs Long Range detection
  - path-wide route-speed intersection
  - bounded topology path generation
  - reliability-first winner selection
  - hysteresis
  - AUTO preference when near-equivalent
  - reverse-order rollback

## Remaining limitations

These are not hidden by the implementation:

1. **Dry-run cannot be perfectly RF-state-neutral.** Temporarily setting/removing
   application routes and generating traffic can change Z-Wave JS/controller learned
   LWR/NLWR state. There is no portable API here to snapshot and restore every learned
   routing heuristic.
2. **This optimizes controller → node application priority routes.** It does not
   rewrite SUC/priority return routes stored for node → controller traffic.
3. **Candidate quality depends on topology data.** With stale neighbor information,
   a good path can be omitted. `Rediscover repeater neighbors first` exists for this
   reason, but is intentionally manual because rediscovery is traffic-heavy.
4. **Physical-controller behavior has not been exercised here.** The code was checked
   against current Home Assistant / Z-Wave JS APIs and tested with isolated mocks, but
   the first run on a real network should still be a dry-run.
5. **Large networks can produce a large action response and substantial Z-Wave
   traffic.** Candidate and sample limits are bounded, but whole-network mode is
   intentionally a manual maintenance operation rather than something to run often.


## Pass 3 — findings from the first real lock run

### 1. Route speed was represented in the wrong unit

**Observed:** The existing lock route came back as `routeSpeed: 2`, while generated
routes were labeled/sent as `40000` and `100000`.

**Problem:** The Serial API priority-route speed field is an enum:
`1 = 9.6 kbit/s`, `2 = 40 kbit/s`, `3 = 100 kbit/s`.

**Fix:** Node capability metadata is normalized to bit/s for path intersection, then
converted to 1/2/3 before `setPriorityRoute`. Results also expose
`route_speed_kbps` so the raw enum is unambiguous.

### 2. Active neighbor rediscovery incorrectly included the controller

**Observed:** ZW0322: discovering neighbors for the controller itself is not possible.

**Fix:** Active rediscovery excludes the controller. Its existing routing information
is still used to build candidate paths.

### 3. Back-to-back FLiRS pings showed warm-device bias

**Observed:** The first measured lock ping was consistently ~70–76 ms while later
back-to-back pings were often ~45–55 ms.

**Fix:** FLiRS targets use 1.10-second spaced samples after warmup and between
measurements. Always-listening nodes retain the short interval.


## Pass 4 — FLiRS route-vs-wakeup separation and return-route safety

### 1. Spaced FLiRS samples measured wake-up behavior instead of route quality

**Observed:** With 1.1-second spacing, almost every lock route alternated between
~45–55 ms and ~1.4–1.5 second transactions.

**Fix:** FLiRS candidates receive at least one unscored wake-up ping, followed by
tightly grouped scored probes. Warm-up timings remain visible but do not affect the
route score.

### 2. Five-sample p95 was effectively the maximum

**Fix:** p95 is no longer used for selection. Results expose median, worst latency,
and a count of severe slow samples. Selection ranks failures, slow samples, median,
then worst latency.

### 3. Final FLiRS repeaters were not required to support beaming

**Fix:** Candidate generation rejects any FLiRS path whose final repeater does not
report beaming capability.

### 4. Priority SUC return routes cannot be safely dry-run

**Finding:** Z-Wave JS only has cached knowledge of priority SUC return routes because
the route cannot be queried from the node. Assigning a priority SUC route replaces a
full return-route set.

**Fix:** v0.6 reports cached return-route state and a non-applied mirrored suggestion,
but never modifies return routes.


## Pass 5 — wake reliability and reverse-topology validation

### 1. Unscored FLiRS warm-up could hide a route that fails to wake the device

**Fix:** Warm-up latency remains excluded, but a failed FLiRS wake-up ping is tracked
as `wake_failures` and ranked as a reliability problem before latency.

### 2. Blindly reversing a forward route could suggest a topology-invalid return path

**Fix:** The controller's cached neighbor information is now read for all ready
classic nodes. A mirrored return-route suggestion includes `topology_validated` and
is only described as plausible when each reverse hop exists in the current graph.
It remains informational and is never applied.


## Pass 7 — real return-route assignment verification

### Finding

The first real priority SUC return-route assignment succeeded, but verification
remained null. Review against the current public Z-Wave JS API found that the
integration was using internal-looking `*_cached` getter command names rather than
the documented controller methods.

### Fix

Return-route inspection now calls:

- `controller.get_priority_suc_return_route`
- `controller.get_custom_suc_return_route`

Response parsing accepts direct, `result`, and named route/list wrappers.

The write path was unchanged because
`controller.assign_priority_suc_return_route` was already correct.

## v0.7.0 — dry-run findings and hardening

### Pass 8 — scoring, candidate diversity, and incumbent protection

#### 1. Slow candidates inflated their own slow-sample threshold

**Observed:** A candidate around 1.5 seconds median with a ~5 second worst sample could report zero slow samples because its threshold was derived from its own median.

**Fix:** All candidate slow-sample counts are evaluated against one device-level best-known baseline: `max(250 ms, best-known median × 4)`. Candidate dictionaries are rendered only after the final device baseline is known, so early candidates are not left with self-relative thresholds.

#### 2. Shallow paths could consume the generated-candidate budget

**Observed:** Multiple one-hop repeaters at two speeds could consume all 12 generated slots before any two-hop route was benchmarked.

**Fix:** Path discovery now has an independent bounded search per repeater depth, and candidate selection round-robins across depths. Within one depth, the fastest speed for each distinct path is considered before additional speeds for the same path.

#### 3. A single outlier could dislodge a known-good application pin

**Observed:** The known-good Laundry Room Door route through node 35 @ 40k lost to AUTO after one ~1.5 second outlier.

**Fix:** Whenever the broad scan selects a challenger over an existing application priority route, the optimizer runs a fresh head-to-head confirmation round between incumbent and challenger. Inconclusive confirmation is fail-safe: keep the incumbent when there is usable incumbent evidence, otherwise make no replacement recommendation.

### Pass 9 — runtime behavior, return routes, and UI safety

#### 1. Path discovery itself could still starve deep routes on a very dense mesh

**Problem:** Merely asking BFS for more paths still allowed a sufficiently dense one-hop layer to fill the global path cap before deeper paths were discovered.

**Fix:** The path search cap is now per hop depth, not global. Direct, one-hop, two-hop, etc. are discovered independently before the candidate budget is applied.

#### 2. Hopeless candidates still consumed all configured samples

**Fix:** Benchmarking stops early after two scored transaction failures. Once a device baseline exists, it also stops after at least three probes when multiple samples and the current median are clearly beyond the baseline slow threshold.

#### 3. Return-route getters repeatedly generated `unknown_command`

**Fix:** The first getter rejection matching `unknown_command` marks return-route readback unsupported for the current Home Assistant runtime. Further getter commands are suppressed and responses report the capability as unsupported cleanly.

#### 4. Return-route writes were a separate action

**Fix:** The separate action is removed. The existing Optimize Node / Optimize Network actions now expose forward-route and suggested-return-route apply toggles. Dry runs always analyze and report the return suggestion. Single-node return-route writes validate repeater health and reverse topology immediately before assignment.

#### 5. A return route could be written for a forward winner that was not actually applied

**Fix:** If the winning forward route differs from the current application pin, return-only application is rejected. The caller must also apply the winning forward route.

#### 6. Whole-network Apply was not ready for the new return-route semantics

**Fix:** Whole-network writes are hard-disabled in v0.7.0. Either Apply toggle causes an immediate refusal before network experimentation. The transactional forward-route rollback helper remains in the codebase for a later deliberate re-enable pass.

#### 7. Long-running network scans had no observable progress

**Fix:** Added an event-driven diagnostic Status sensor with operation, current node/name, node X/Y, candidate X/Y, route, completion count, elapsed time, phase, and compact latest-result attributes.

#### 8. Skipped-node output was ID-only

**Fix:** Whole-network output now returns `skipped_nodes` entries with `node_id`, `name`, and explicit reason, while retaining `skipped_node_ids` for compatibility.

### v0.7.0 isolated regression validation

- Python syntax compilation for all integration modules.
- JSON parsing for manifest, strings, and English translation files.
- YAML parsing for `services.yaml`.
- Device-baseline slow-sample test using the pathological ~1537 ms / 5092 ms case.
- Candidate-budget test proving direct, one-hop, and two-hop candidates are all represented under the default-style 12-slot budget.
- FLiRS final-beaming constraint regression test.
- Unsupported return-route getter test proving only one `unknown_command` probe is issued before suppression.
- Early-elimination test proving two failed transactions stop a five-round candidate after two probes.
- Mocked incumbent-confirmation regression reproducing a noisy pinned-route scan and verifying the clean confirmation retains the incumbent.
- Whole-network Apply guard test proving writes are refused before Home Assistant/Z-Wave state is accessed.
- Skip-reason tests for sleeping battery, Long Range, and dead nodes.

### Remaining v0.7.0 limitation intentionally preserved

Priority SUC return routes still cannot be read back reliably from the end node, so a successful return-route write cannot be given the same rollback guarantees as a forward application-priority route. This is one reason whole-network Apply remains disabled.


## v0.7.1 — multi-pass whole-network dry runs

### Pass 10 — repeatability without multiplying response size

#### 1. Multi-pass scans must not change write safety

**Constraint:** Whole-network Apply remains hard-disabled. Each pass invokes the same per-node dry-run path with `defer_apply=True`, so application-priority state is restored after each experimental node scan. The neighbor graph is built once before all passes.

#### 2. Full per-pass candidate output would become excessively large

**Problem:** A 23-node run with ~12 candidates per node is already large. Returning full candidate details for every pass would multiply the Home Assistant action response and make copy/paste analysis unwieldy.

**Fix:** `passes` contains compact per-node summaries for every pass (winner, median, failures, slow samples). The existing top-level `results` field remains the full detailed result for the final pass, preserving the familiar single-pass response shape while keeping multi-pass output bounded.

#### 3. Progress needed a pass dimension

**Fix:** The diagnostic Status sensor now carries `pass_index` and `pass_total` during whole-network runs. `node_index/node_total` remain scoped to the current pass, while `completed_count` increases across all node-runs.

#### 4. Node optimization must not accidentally accept the new option

**Fix:** `passes` is added only to `NETWORK_SCHEMA` and is supplied separately by the network service handler. The shared single-node option extraction remains unchanged.

### v0.7.1 regression validation

- Python syntax compilation for all integration modules.
- JSON parsing for `manifest.json`.
- YAML parsing for `services.yaml`.
- Structural check that `passes` is network-only with range 1–10 and default 1.
- Response-shape review confirming full detail is retained only for the final pass and compact summaries are retained for every pass.
- Status lifecycle review confirming pass fields initialize/reset cleanly and do not leak into subsequent single-node runs.
- Whole-network Apply guard remains before any network access or pass iteration.


## v0.7.2 — adaptive candidate benchmarking

### Pass 11 — passive RF prioritization and route-expansion short circuits

#### 1. Neighbor topology alone does not encode link quality

**Problem:** A node appearing in a neighbor table only proves that communication was observed strongly enough for the topology cache; it does not mean every such link deserves an expensive benchmark.

**Fix:** Adaptive mode reads passive Z-Wave JS node statistics when present. Recent node RSSI, controller-local background RSSI, and observed LWR/NLWR route/repeater RSSI are converted into ordering hints. These hints never hard-filter a topology-valid candidate. Missing, stale, sentinel, or unavailable RSSI therefore degrades gracefully to neutral ordering.

#### 2. Passive RSSI is directional and can be stale

**Risk:** Node statistics primarily describe recently observed traffic and can be asymmetric relative to the forward application-priority route being tested.

**Fix:** RF evidence is deliberately used only for ranking. Candidate results expose `rf_score` and `rf_evidence`, and the response explicitly reports `hard_rssi_filtering: false`. No route is declared good because of RSSI alone.

#### 3. Excellent direct links made deeper-route probing wasteful

**Observed:** In the real two-pass network run, many mains-powered devices produced clean direct medians around 22–25 ms while one-hop routes clustered around ~40 ms and two-hop routes around ~55–60 ms or worse.

**Fix:** Generated routes execute short-to-deep in adaptive mode. After current/AUTO baselines, a clean direct route with median <= 35 ms and worst <= 75 ms stops route expansion. This is a measured-result short circuit, not an RF-only decision.

#### 4. Difficult nodes still needed repeater exploration

**Fix:** If direct routing is not excellent, adaptive mode tests distinct one-hop paths in passive-RF priority order. After up to three distinct one-hop paths, a clean one-hop result with median <= 45 ms and worst <= 100 ms stops deeper expansion. Otherwise the remaining selected candidate set continues normally, including multi-hop routes.

#### 5. Adaptive behavior needed an exhaustive fallback

**Fix:** Both actions expose **Adaptive candidate testing**, enabled by default. Turning it off disables passive RF ordering and adaptive short-circuiting, restoring exhaustive benchmarking of the full generated candidate set.

#### 6. Adaptive savings needed to be visible in multi-pass summaries

**Fix:** Detailed results include a `candidate_strategy` object with planned/tested/skipped counts, thresholds, stop reason, and untested-route RF hints. Compact whole-network pass summaries include tested/skipped candidate counts and the adaptive stop reason.

### v0.7.2 regression validation

- Python syntax compilation for all integration modules.
- JSON parsing for manifest/string/translation files and YAML parsing for `services.yaml`.
- Passive RF ranking test proving an exact LWR match with stronger usable RSSI ranks ahead of a weak unrelated repeater.
- Sentinel RSSI handling excludes non-negative Z-Wave RSSI error values from ranking.
- Candidate-plan test confirms direct candidates execute before one-hop candidates while hop-diverse candidate budgeting remains intact.
- Mocked direct-route flow confirms AUTO + an excellent direct route stop before remaining generated candidates and still restore the starting priority state.
- Mocked one-hop flow confirms three distinct one-hop probes can stop before a two-hop route only when the measured one-hop result satisfies the conservative latency/reliability thresholds.
- Whole-network Apply remains hard-disabled before topology discovery or adaptive benchmarking begins.
