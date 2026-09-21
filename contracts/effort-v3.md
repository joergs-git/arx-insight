# Contract `effort-v3` - did the set produce fatigue? One rule for ARX Insight and arx-free

Version 1 (2026-09-21). **Owner of the rule: ARX Insight** (`arx_detail.effort_v3` and the phase means in
`arx_detail.set_detail`). arx-free needs the same judgement at the machine - live and right after the last repetition,
without Insight running - so it carries a port (`src/arx_app/effort.py`). Both implementations are tested against the
same vectors (`contracts/effort-v3-vectors.json`, generated with Insight's own function). A change of the rule is a
change of this contract first: new version, new vectors, then both projects.

Why a contract: a second, "similar" rule would let the machine say "well done" where the report says "that was a
strength test" - the owner's decision D (2026-09-21): shared rules, or the two products drift apart.

## Input: one value per repetition and phase kind

For every **complete** repetition (both moving phases recorded) two numbers: the **time-weighted mean force** of the
concentric phase and of the eccentric phase (trapezoid integral over the window divided by its duration; border points
interpolated). The window of a phase leaves out its first `PHASE_SETTLE_S` = 1.0 s - the start of a phase still carries
the force of the phase before - unless the phase is shorter than `SHORT_PHASE_S` = 2.0 s (then the whole phase). Holds
are not part of it. Rising position = concentric. The unit does not matter (ratios); Insight feeds kilograms rounded
to one decimal, arx-free pounds - the vectors are chosen so that this never flips a result.

## The rule

With `n` repetitions, `half = ceil(n / 2)`, `w = max(2, n // 4)`, `k = min(max(3, n // 4), n - half)`:

* `reference(series)` = the best mean of `w` consecutive values among the first `half` values,
  `end(series)` = the median of the last `k` values. Fewer than **4 repetitions** (or `k < 1`): no judgement.
  (The reference is restricted to the first half on purpose: "best window anywhere" reads about 10 % on shuffled
  repetitions - noise would look like effort; this one reads about 0 there, and a set whose force rises reads 0.)
* Each phase kind is normalised to its own reference, then combined repetition by repetition:
  `series[i] = 0.5 * (con[i] / reference(con) + ecc[i] / reference(ecc))`.
* `output_change_pct = (end(series) / reference(series) - 1) * 100` (one decimal), **`inroad_v3 = max(0, round(-change))`**.
* Class: **deep** `>= 20`, **moderate** `>= 10`, else **submax**; `unknown` without a judgement.
  `borderline` = within `BORDERLINE` = 2 points of one of the two lines.
* Explanatory figures: `fatigue_con_pct` / `fatigue_ecc_pct` (drop of each phase kind alone),
  `pacing_deficit_pct` = how much the first `w` repetitions stayed below the best stretch of the set (held back at the
  start), `best_rep`.

## Reading it (both products)

* The target comes from the athlete's goal: 10 for strength / conditioning, 20 for muscle (Insight's
  `EFFORT_TARGETS`). arx-free uses **10** until a goal is handed over (owner's decision A).
* **Reached** = `inroad_v3 >= target - BORDERLINE`. **Missed** = below that - and only a miss leads to "again, now -
  properly" (Insight's `repeat_now`).
* **Never judged as a miss** (arx-free marks these sets, Insight reads the mark through the export - see
  `tasks/cross-repo.md`): on-ramp sets of a new athlete (planned below full effort), sets that were ended early for
  pain / dizziness / headache, test repetitions.
* The original software's Inroad Mode uses another scale (momentary force against the set's maximum, typically
  30 - 40 %). Those numbers do not translate - arx-free shows and uses `inroad_v3` only.

## Live estimate (arx-free only)

The same rule on the repetitions finished so far. Below four repetitions there is no estimate - the live coach then
compares against the reference set and the first repetition instead (`docs/feature-live-coach.md`).
