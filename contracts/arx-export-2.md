# Contract `arx-export-2` - history of the original software for arx-free

Version 2 (2026-09-23, ARX Insight v0.22.0 / arx-free v0.9.0) - supersedes `arx-export-1` (2026-09-20). What is new:
the **route** `GET /api/export?since=` on ARX Insight's local listener, so that the original's newest sets land in
arx-free by themselves (owner's wish of 2026-09-22 night), and the header field `since`. The **lines are unchanged**:
the format name in the header stays `arx-export-1` because it names the line format, a file of version 1 is still
valid, an importer written for version 1 reads a version-2 answer as it is. arx-free drafted this as a "revision 1.1"
of the version-1 file on 2026-09-23; the owner of the contract (ARX Insight) issues it as version 2 - an adopted
contract file is never edited in place. `arx-export-1.md` stays in both folders until arx-free has adopted this file.
Revised the same day, before adoption (ARX Insight v0.23.0): the `athlete` line carries `language` and `display_units`
when ARX Insight knows them - the owner's "yes" to arx-free's proposal for ONE user basis (2026-09-23).

Direction: ARX Insight -> arx-free, one way, repeatable. The exporter lives in ARX Insight because it already owns the
safe copy-then-open access to the original Firebird database; arx-free never needs a Firebird client. Units:
**imperial (lb, inch, seconds)**, exactly as stored by the original. Nothing is converted, rounded or "improved" on
the way: configuration, events and samples travel **verbatim**.

## Two ways, the same lines

1. **The file** `arx-export-<yyyymmdd-hhmmss>.ndjson.gz` written by `tools/export_for_arx_free.py` (optionally with
   `--since`), imported by hand (`python -m arx_app.importer <file>`, Admin "Import history").
2. **The route** `GET /api/export?since=<started_at>` on a running ARX Insight's **local listener** (loopback only,
   `X-ARX-Token: local` like every local route; never on the phone listener - the answer holds names and training
   data). `Content-Type: application/gzip`; the body is byte for byte what the tool writes: one `header`, EVERY
   `athlete`, the `set` lines ordered by `source_set_id`, one `footer` with the counts of THIS answer.
   * `since` = a `started_at` text of the export itself (naive local time with seconds, e.g. `2026-09-13T18:04:11`):
     only sets that began **strictly after** it; without `since`: all sets. Fractions of a second are cut the way the
     export cuts them, so "after 18:04:11" means "at 18:04:12 or later" in the database. A `since` that is no date
     and time (or carries a time zone) is answered with `400 {"error": "bad_since"}`.
   * An ARX Insight older than v0.22.0 answers 404: arx-free says so in Admin and asks again at its next start.
   * arx-free asks at its start and whenever a session begins (`arx_app/sync.py`, a thread of its own - no tap
     waits), keeps the answer as `data/imports/insight-latest.ndjson.gz`, imports what is new with the idempotent
     importer and never sends anything back. `since` for the next call = the newest `started_at` of the sets that came
     from this source.
   * ARX Insight writes the answer to a temporary file in its data folder, streams it and removes it whatever happens
     (a start removes what a crash left behind). Response header `X-ARX-Export: athletes=N; sets=N; skipped=N`.

## Lines

gzip, UTF-8, one JSON object per line, in this order:

1. one `header`
2. all `athlete` lines
3. all `set` lines, **ordered by `source_set_id`** (a set may refer to an earlier one)
4. one `footer`

The file contains names and training data of real people: it stays on the owner's machines, is never committed and
never leaves the local network.

### header

```json
{"kind": "header", "format": "arx-export-1", "source": "arx-original", "created_at": "2026-09-20T18:00:00",
 "exporter": "arx-insight 0.22.0", "since": "2026-09-13T18:04:11|null"}
```

`source` names the origin of all `source_*` ids in the file; arx-free stores it next to them. `since` (new in v2) is
the filter this answer was made with, `null` for a complete export.

### athlete

```json
{"kind": "athlete", "source_user_id": "17", "first_name": "...", "last_name": "...", "gender": "m|f|null",
 "birth_date": "1980-05-17|null", "created_at": "2024-03-01T10:00:00|null",
 "language": "de", "display_units": "metric"}
```

Only these fields leave the original database. No e-mail, no password or token columns, no cloud ids, no waiver.
The sentinel date `0001-01-01` of the original is exported as `null`. Text is decoded explicitly (charset NONE).

**What ARX Insight knows about the person** (owner's decision 2026-09-23, "Insight wins for the person, arx-free
wins for the machine"): `language` (`de` | `en`) = the person's own choice in ARX Insight's profile, else the device's
language when it is set; `display_units` (`metric` | `imperial`) = the device's units when they are set (ARX Insight
keeps units per device). Both are **optional and present only when known**. Never `photos_enabled` - ARX Insight does
not know it, it stays arx-free's. An importer ignores fields it does not know.

Import rule (one user basis): arx-free takes the two at creation AND on every later import and shows them read-only
in the athlete's profile from then on (`athletes.insight_profile_at`); an athlete without the fields keeps arx-free's
own values and stays editable there. Everything machine-side (coach switches, on-ramp, fatigue targets, positions,
remembered settings) stays arx-free's and is never overwritten. A value that is not one of the agreed words is ignored.
Editing the person (name, language, units) happens in ONE place: ARX Insight.

### set

```json
{"kind": "set", "source_set_id": "4711", "source_user_id": "17", "exercise_code": 10,
 "started_at": "2026-09-13T18:04:11",
 "protocol": 3, "protocol_parameter": 5,
 "rep_scheme": "LoopingRepSequence",
 "elapsed_s": 52.4, "intensity_lb": 181.2, "max_lb": 310.0, "max_c_lb": 240.5, "max_e_lb": 310.0,
 "hide_from_stats": false, "notes": "text|null", "rest_timer_s": 180, "rest_timer_used_s": 201,
 "comparison_source_set_id": "4650|null",
 "config": { ...REPSCHEMEDATA as stored... },
 "events": [ ...EVENTSTREAMDATA as stored, without the "WaitingTimeLeft" countdown ticks... ],
 "samples": [ ...SERIALIZEDDETAILEDDATA as stored... ]}
```

* `started_at` = `EXERCISEDATE`, naive local time of the set start, seconds. `protocol`: 3 = repetitions, 1 = countdown
  seconds, 0 = inroad percent (4 = fatigue is arx-free's own, never from the original - contract `modes-3`);
  `protocol_parameter` = its value. `rep_scheme`: `LoopingRepSequence` or `StaticModeData`.
* Scalars are the original's columns (`ELAPSEDSECONDS`, `INTENSITY`, `MAXLOAD`, `CONCENTRICMAX`, `ECCENTRICMAX`);
  arx-free shows them as they were and marks them `metrics_algo_version = 0` ("as stored by the original").
* `config` keys as in the original: `StartPosition`, `EndPosition`, `StartToEndSpeed` / `EndToStartSpeed`
  {`InchesPerSecond`, `MaxInchesPerSecond`}, `AccelerationTime`, `DecelerationTime`, `PauseAfterEndPosition`,
  `PauseAfterStartPosition`, `PreExerciseTimer`.
* `events`: objects with `Time` (ISO with offset) and `Type`; `samples`: objects with `Time`, `Value`, optionally
  `RawValue`, `TaredValue`, `EncoderValue`, `SpeedValue` (and the `Has...` flags). A sample without `Value` or `Time`
  is dropped by the importer, not by the exporter.
* Sets with `DELETED` true are **not exported**. `HIDEFROMSTATS` becomes `hide_from_stats` (arx-free: "junk").
* `comparison_source_set_id` = `COMPARISONSET_ID`: the set whose curve was shown in grey. In an answer made with
  `since` that set may be older than `since` and therefore not in the answer: the importer links it when it already
  holds it and otherwise leaves the link out (never a refusal).

### footer

```json
{"kind": "footer", "athletes": 12, "sets": 2381}
```

The counts of what is in THIS file or answer (with `since`: the sets after it). The importer refuses a file whose
counts do not match (a cut-off copy).

## Import rules (arx-free, `arx_app/importer.py`)

* **Idempotent**: athletes by `(source, source_user_id)`, sets by `(source, source_set_id)`; the set's id in arx-free
  is `uuid5(arx-free namespace, "<source>:<source_set_id>")`. Importing the same or a newer export again adds only what
  is new and changes nothing that exists (notes or junk flags edited in arx-free stay). An answer made with `since`
  is imported the same way - a set that is there already is simply there already.
* New athletes get the app's default display units. Athletes are never merged by name.
* Times inside a set become seconds since its `BeginSequence` event; the phase list (concentric = rising position)
  and the inroad figures are computed by arx-free's own engine from the verbatim samples.
* A set counts when it was not ended before completion; an unfinished set is kept and marked.
* **Remembered settings**: for every athlete and exercise without settings in arx-free, the latest counting
  repetition set provides start / end position, travel times (`|start - end| / speed + AccelerationTime`, rounded to
  0.5 s), holds, repetitions and the countdown (never below 5 s). The range is **not** marked as confirmed: the
  athlete confirms it once in arx-free before the first set (docs/safety.md).
