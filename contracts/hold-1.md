# Contract `hold-1` - did the hold produce fatigue? One rule for ARX Insight and arx-free

Version 1 (2026-09-22). **Owner of the rule: ARX Insight** (`arx_detail.hold_v1`). arx-free ends a static hold live
with the same rule (its fatigue-target protocol, ending `4`, for holds) and needs the same judgement at the machine
without Insight running - so it carries a port, tested against the same vectors (`contracts/hold-1-vectors.json`,
generated with Insight's own function; the last case is the owner's own Static test on the original, 2026-09-22).
A change of the rule is a change of this contract first: new version, new vectors, then both projects.

Why its own rule: effort-v3 needs repetitions (both phases); a hold has none. The original's Inroad zone is no
judgement for a hold either - it is a high-water mark of the momentary force (a one-second push to 52 lb lifted it to
47-52 lb while about 30 lb were held) and the original evaluates nothing for a hold (owner's test, contract modes-3).

## Input

The force curve of the hold: time in seconds from the first sample and the force in any unit (ratios; Insight feeds
kilograms, arx-free pounds). Irregular sampling is fine - everything below is read on the piecewise-linear curve
through the samples with time-weighted means (trapezoid integral over the window divided by its duration, border
points interpolated - as in effort-v3).

## Constants

| name | value | meaning |
|---|---|---|
| `HOLD_SETTLE_S` | 2.0 s | the force is still building up - not judged |
| `HOLD_WINDOW_S` | 3.0 s | the window of the sustained force and of the held force |
| `HOLD_PERSIST_S` | 1.0 s | a depth counts (and the live rule ends the hold) when it lasted this long |
| `HOLD_STEP_S` | 0.1 s | the grid the rule is evaluated on - independent of the sampling rate |
| `HOLD_RELEASE_SHARE` | 0.5 | raw force below this share of the reference for a full `HOLD_PERSIST_S` = let go |
| `HOLD_JUDGE_MIN_S` | 6.0 s | settle + window + persist: a shorter hold gets no judgement |
| `INROAD_DEEP` / `INROAD_MODERATE` / `BORDERLINE` | 20 / 10 / 2 | the classes, as in effort-v3 |

## The rule

Grid: `g_i = HOLD_SETTLE_S + HOLD_WINDOW_S + i * HOLD_STEP_S` for `i = 0, 1, ...` while `g_i <= t_last` (rounded to
6 decimals). `n_p = HOLD_PERSIST_S / HOLD_STEP_S` = 10 steps; "a full span" = `n_p + 1` consecutive grid points, i.e.
the curve over `[g - HOLD_PERSIST_S, g]`. At every grid point `g`:

* **held force** `m(g)` = time-weighted mean of the curve over `[g - HOLD_WINDOW_S, g]`;
* **sustained force** = the minimum of the curve over the same window; **reference** `R(g)` = the largest sustained
  force seen up to `g` (running maximum). A spike shorter than the window cannot raise it; the build-up at the start
  cannot lower it later.
* **let go**: the first grid point `g_rel` (from `i = n_p` on) whose span lies ENTIRELY below
  `HOLD_RELEASE_SHARE * R(g)` (maximum of the curve over the span). The hold is over there: a re-grab, a push or a
  rest on the handle after it is not the hold. The **judged end** `g_end` = the last grid point before `g_rel` (or the
  last grid point at all when nothing was let go) whose span lies entirely at or above `HOLD_RELEASE_SHARE * R(g)`.
  No such point: no judgement (`too_short`).
* **depth** `d(g) = (1 - m(g) / R(g)) * 100` for `g <= g_end`; **fatigue** = the largest depth held for a full span:
  `max over i >= n_p of min(d[i - n_p .. i])`, never below 0; **`inroad_hold = round(fatigue)`** (Python rounding).
* Class: **deep** `>= 20`, **moderate** `>= 10`, else **submax**; `borderline` = within `BORDERLINE` of a line.
* Reported with it: `reference = R(g_end)` and `end_mean = m(g_end)` (one decimal, input unit), `judged_s = g_end`,
  `released_s = g_rel` (None when nothing was let go). A recording shorter than `HOLD_JUDGE_MIN_S`: `too_short`;
  no force at all: `no_force`.

## Live ending (arx-free)

The same quantities as the hold runs (grid, means, running reference), and the hold ends at the FIRST of:
(a) **target reached** - `d(g) > target` at every point of a full span (`live_end_s[target]` in the vectors is that
grid point, for the goal targets 10 and 20; None = never); (b) **let go** - the release span above; (c) the planned
length `hold_s` (a Countdown upper limit). A hold ended by (a) reads at least the target retrospectively
(`inroad_hold >= target`); a hold ended by (b) reads the fatigue reached before the release - honest, and often small
(the owner's test: 8 %, he let go at 50 s while the original showed a zone of 47-52 lb).

## Reading it (both products)

Target and "reached" as in effort-v3 (`inroad_hold >= target - BORDERLINE`); a hold below the target is a real load
with half the stimulus, never "no stimulus" (vocabulary-1). Insight carries the figure in the set's fatigue slot like a
dynamic set's `inroad_v3` and reports the block `hold {reference, end_mean, inroad_hold, effort, borderline, judged_s,
released_s, live_end_s}`; holds are compared only with holds at the same position (modes-3).
