# Contract `modes-3` - what a set's mode is, and the two figures that come with it

Version 3 (2026-09-22) - supersedes `modes-2`, which arx-free adopted on 2026-09-22: the description of the original's
Inroad Mode now follows what the owner saw on the original himself (2026-09-22, 07:24, Static + Inroad, screenshot):
the original NEVER ends a set by itself - the hold ran more than 20 s far below the zone until a person ended it at
1:22; the zone is a high-water mark of the momentary force (a one-second spike to 52 lb pushed it to 47-52 lb while
about 30 lb were held); and the original evaluates nothing for a hold. The "automatic ending" in modes-2 was the
owner's wish for the product, written down as the original's behaviour - arx-free's fatigue target (ending `4`)
delivers it. New in v3: how a recommended hold is run on each product, and that ONE hold rule (owner: ARX Insight,
contract `hold-1`) judges holds on both sides. Nothing else changed; `modes-2.md` stays in both folders until arx-free has adopted
this version. **Owner: ARX Insight** (analysis and planning); arx-free adopts the definitions for its set records, its
live view and its live coach. Units in data: imperial (lb, inch, seconds) as stored by the original.

## Mode = movement x ending (+ phase)

* **movement**: `static` when the set is a hold - the original marks it with the rep scheme `StaticModeData` or with
  `StartPosition == EndPosition` in the set configuration; else `dynamic`.
* **ending** = what stops the set, the original's `ExerciseSet.PROTOCOL`: `3 = reps` (a repetition count),
  `1 = time` (the clock - "Countdown Mode"), `0 = inroad` (the original's Inroad Mode: it shows a work zone - the
  high-water mark of the momentary force minus the set percentage - and a PERSON ends the set; the original never ends
  a set by itself and evaluates nothing for a hold), `4 = fatigue` (arx-free only: the set ends by itself when the
  fatigue in the set - effort-v3; for a hold the shared hold rule - reaches the target; the original has no such
  protocol, arx-free's records and its compatibility views write 4, never 0). Any other code is `unknown` and is shown
  as unknown - never silently treated as reps. (Time Trial Mode's code is not known yet; it will get its own label
  once seen.)
* **phase** (from the curve, dynamic sets only): `both` normally; `negative` when the concentric phases carried less
  than `SINGLE_PHASE_SHARE` = 0.15 of the eccentric mean force (negative-only reps), `positive` the other way round;
  `hold` for a static set. Single-phase sets get **no fatigue judgement** (effort-v3 needs both phases) and are
  compared by the loaded phase's force only.

## Comparability

A day is only compared with days of the same movement, the same ending and the same phase scheme; dynamic sets
additionally need the same range of motion, tempo and grip-aid state (Insight's existing rules); static holds need
the same position (`StartPosition` within +- `STATIC_POS_TOLERANCE_IN` = 1.0 in). A mode change restarts the
comparison basis, and both products say so.

## Output

`Output` of a set = its impulse: the time-averaged force (the original's `INTENSITY`, lb) times the set's elapsed
seconds - lb·s in data, shown in the athlete's force unit times seconds. It is the progress figure of timed sets:
two timed days compare only at the same duration (+- `OUTPUT_TIME_TOLERANCE` = 5 %); more Output in the same time
is progress ("beat your gray line").

## The machine's own inroad scale

`inroad_machine` = the decline from the best repetition's peak force to the last repetition's peak force, in
percent of the best peak (Insight's `inroad_legacy`, whole-rep peaks). That is the scale the original's Inroad Mode
and its display use (30-40 % are usual settings there; the zone is shown, a person stops - and for a hold the zone
follows a momentary spike, so it says nothing there). It is shown NEXT TO the fatigue in the set (effort-v3) and
never under the same name (see `vocabulary-1`: "Maschinen-Inroad" / "machine inroad"). It exists for dynamic sets
only: a calibration of the machine scale to the fatigue in the set is fitted on dynamic both-phase sets and is
never applied to a hold.

## A recommended hold (new in v3)

Insight recommends a hold as `static/time` with `hold_s` (the length) and `fatigue_target_pct` (the goal's fatigue
target; `null` = no number, by feel). On the original it is run as a Countdown of `hold_s`: the clock ends it, a
person may end it earlier - the Inroad zone is no stop signal for a hold. On arx-free it is run as `static/fatigue`:
the hold ends by itself when the shared hold rule reaches `fatigue_target_pct` (`hold_s` is then the upper limit; a
hint without a target runs as `static/time` there too). Both products judge the fatigue of a hold with ONE rule,
contract `hold-1` (owner ARX Insight: the held force against the sustained force, persistence, letting go ends the
hold, the live ending; vectors generated with Insight's function - arx-free ports it and ends a hold live with it).

## Field names (payloads, exports, views)

`movement`, `ending`, `phase`, `pos_in` (StartPosition, inch), `output_kg_s` (Insight, kg·s) / `output_lb_s`
(arx-free, lb·s), `inroad_machine` (%). A set's mode is written `movement/ending[/phase]`, e.g. `static/time`,
`dynamic/time`, `dynamic/fatigue` and `static/fatigue` (arx-free), `dynamic/reps/negative`. A hold hint carries
`settings.hold_s` and `settings.fatigue_target_pct`.
