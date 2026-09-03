# Z-Wave Route Optimizer for Home Assistant

Version: **0.8.1**

A manually invoked Home Assistant custom integration for benchmarking and optionally pinning **classic Z-Wave application priority routes** using Home Assistant's existing Z-Wave JS connection.

It does **not** schedule optimizations, open a second Z-Wave JS connection, or optimize in the background.

## Install

Copy `custom_components/zwave_route_optimizer/` into `/config/custom_components/zwave_route_optimizer/`, restart Home Assistant, then add **Z-Wave Route Optimizer** from **Settings → Devices & services**.

## Actions

In **Developer Tools → Actions**:

- **Z-Wave Route Optimizer: Optimize one Z-Wave node**
- **Z-Wave Route Optimizer: Optimize Z-Wave network**
- **Z-Wave Route Optimizer: Apply last network optimization**

The integration also creates:

- a diagnostic **Status** sensor with live optimizer/apply progress;
- an **Apply last optimization** button, available only while a staged plan has write-ready routes.

## Whole-network workflow

Whole-network discovery and commitment are intentionally separate.

1. Run **Optimize Z-Wave network**. It is always a dry run and restores every experimental application-priority route.
2. The completed run produces `route_stability` and an `apply_plan` and stages that exact plan in memory.
3. Review the returned plan. Only `ready_to_set` operations are eligible for automatic bulk Apply.
4. Press **Apply last optimization** or run **Apply last network optimization**.
5. The integration revalidates the staged plan before the first write, then writes and verifies each route transactionally.

A new optimization replaces the previous staged plan. A Home Assistant restart discards it. A staged plan expires after **30 minutes**. A successful or attempted write transaction consumes the plan, so it cannot be replayed accidentally.

### Bulk-write readiness policy

v0.8.1 keeps the existing exact-stable policy and adds a conservative three-pass physical-consensus path for cases where AUTO and an explicit candidate represent the same repeater path.

An explicit route can become `ready_to_set` in either of two ways:

- **Exact stability:** at least two complete passes, the same explicit candidate won every pass, every winning pass had zero failures and zero slow samples, and the route differs from the starting application-priority route.
- **Physical repeater consensus:** at least three complete passes, the physical repeater path is known and identical in every pass, every winning pass is clean, and the same exact explicit candidate on that path wins a strict majority of passes. For three passes this means at least **2/3 explicit wins**.

Other outcomes are intentionally not written:

- already-matching stable explicit routes → `no_change`;
- exact-stable AUTO on an already-unpinned node → `leave_auto`;
- an all-pass stable **direct** physical path on an unpinned node → `leave_auto` rather than creating an unnecessary direct pin;
- path-stable repeater routes without an explicit majority → `hold`;
- AUTO that would require clearing a current pin → `hold`;
- incomplete, dynamic, or unstable outcomes → `hold`.

Three passes are recommended before using whole-network Apply because physical-consensus promotion is intentionally unavailable with only two passes.

### Route-change review data

`apply_plan.route_changes` provides a compact visualization-friendly record for every evaluated node. Each entry includes:

- the starting application/learned route;
- the all-pass physical consensus path and speed agreement when known;
- the strongest exact explicit candidate and its win count;
- compact per-pass winner, physical path, latency, failure, and slow-sample data;
- the final `ready_to_set`, `leave_auto`, `no_change`, or `hold` decision and proposed write operation.

This data is intended to support a future Home Assistant route-change visualization without changing the transaction format used by Apply.

## Apply preflight and transaction safety

**Apply last network optimization does not benchmark again.** It commits exactly the staged plan that was produced by discovery.

Before writing anything, the integration:

- confirms the Z-Wave integration/controller identity still matches the staged plan;
- checks that disruptive controller work (route rebuild, inclusion/exclusion, OTA) is not active;
- rebuilds the current cached neighbor graph and revalidates every staged path;
- checks target/repeater eligibility, FLiRS beaming requirements, and end-to-end route speed support;
- re-reads every affected node's current application-priority route and requires it to match the snapshot captured during discovery.

If any material preflight check fails, **zero routes are written** and the stale plan is invalidated.

Once preflight succeeds, each node is re-read immediately before its write to detect an external route change after preflight. Each write is then followed by application-priority-route readback verification. If a write or verification fails, every node whose write may have started is restored to its preflight snapshot in reverse order. Cancellation also shields rollback so a cancelled Home Assistant action cannot intentionally strand a partially applied plan.

The response distinguishes successful rollback from `rollback_incomplete` and reports every rollback failure explicitly.

## Return-route limitation

Whole-network Apply modifies **forward application-priority routes only**. Bulk priority SUC return-route writes remain disabled because the actual route stored in an end node cannot be read back reliably enough to provide equivalent transactional rollback guarantees.

The single-node action still exposes its existing deliberate forward and return-route write controls.

## Benchmarking behavior

- Classic Z-Wave only; Z-Wave Long Range is skipped.
- Ordinary sleeping battery nodes are skipped; FLiRS targets remain supported.
- FLiRS probes use an unscored wake-up phase followed by grouped scored probes.
- The final repeater into a FLiRS target must support beaming.
- Route speeds use Serial API enums: `1=9.6k`, `2=40k`, `3=100k`.
- Up to four repeaters are supported; the default maximum is two.
- Existing application priority routes are distinguished from learned LWR/NLWR routes.
- Every experimental forward route is restored before moving on; cancellation shields restoration.
- Adaptive candidate testing is enabled by default and uses passive RF/history only for candidate ordering, never as a hard filter.
- Learned LWR/NLWR history gets full ranking weight only after a measured clean/fast AUTO baseline; a poor AUTO result removes the learned-route bonus.
- A clean direct route can stop expansion early; otherwise RF-ranked one-hop routes are tried before deeper paths.
- A clean incumbent can stop after three challenger paths when it still wins under the configured hysteresis.
- Slow/pathological candidates can be eliminated early using a best-known device baseline.
- A challenger that would replace an existing application pin receives a fresh incumbent-vs-challenger confirmation round.
- Whole-network runs support 1–10 passes and return compact per-pass summaries plus full detail for the final pass.

## Live status

The event-driven Status sensor tracks optimizer and Apply progress. During discovery it includes pass, node, candidate, route, elapsed time, and latest result. After discovery it reports `plan_ready`/`plan_no_changes` plus the staged plan ID/counts. During Apply it moves through phases including:

- `preflight_topology`
- `preflight`
- `prewrite_check`
- `applying`
- `verifying`
- `rollback` when required
- `completed`, `rolled_back`, `rollback_incomplete`, or `plan_invalid`

## Dry-run caveat

Dry-run restores the starting **application priority-route configuration**, but benchmark traffic can still influence learned LWR/NLWR/controller routing state. There is no portable API to snapshot and restore every learned routing heuristic.
