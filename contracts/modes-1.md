# Contract `modes-1` - what a set's mode is, and the two figures that come with it

Version 1 (2026-09-21). **Owner: ARX Insight** (analysis and planning); arx-free adopts the definitions for its set
records, its live view and its live coach. Units in data: imperial (lb, inch, seconds) as stored by the original.

## Mode = movement x ending (+ phase)

* **movement**: `static` when the set is a hold - the original marks it with the rep scheme `StaticModeData` or with
  `StartPosition == EndPosition` in the set configuration; else `dynamic`.
* **ending** = what stops the set, the original's `ExerciseSet.PROTOCOL`: `3 = reps` (a repetition count),
  `1 = time` (the clock - "Countdown Mode"), `0 = inroad` (the machine's Inroad Mode ends the set when the force can
  no longer reach the selected share of the set's maximum). Any other code is `unknown` and is shown as unknown -
  never silently treated as reps. (Time Trial Mode's code is not known yet; it will get its own label once seen.)
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
and its display use (30-40 % are usual settings there). It is shown NEXT TO the fatigue in the set (effort-v3) and
never under the same name (see `vocabulary-1`: "Maschinen-Inroad" / "machine inroad").

## Field names (payloads, exports, views)

`movement`, `ending`, `phase`, `pos_in` (StartPosition, inch), `output_kg_s` (Insight, kg·s) / `output_lb_s`
(arx-free, lb·s), `inroad_machine` (%). A set's mode is written `movement/ending[/phase]`, e.g. `static/inroad`,
`dynamic/time`, `dynamic/reps/negative`.
