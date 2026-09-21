# Contract `arx-export-1` - history of the original software for arx-free

Version 1 (2026-09-20). Direction: ARX Insight -> arx-free, one way, repeatable. The exporter lives in ARX Insight
because it already owns the safe copy-then-open access to the original Firebird database; arx-free never needs a
Firebird client. Units: **imperial (lb, inch, seconds)**, exactly as stored by the original. Nothing is converted,
rounded or "improved" on the way: configuration, events and samples travel **verbatim**.

## File

`arx-export-<yyyymmdd-hhmmss>.ndjson.gz` - gzip, UTF-8, one JSON object per line, in this order:

1. one `header`
2. all `athlete` lines
3. all `set` lines, **ordered by `source_set_id`** (a set may refer to an earlier one)
4. one `footer`

The file contains names and training data of real people: it stays on the owner's machines, is never committed and
never leaves the local network.

### header

```json
{"kind": "header", "format": "arx-export-1", "source": "arx-original", "created_at": "2026-09-20T18:00:00",
 "exporter": "arx-insight 0.6.0", "database": {"ods": "13.0", "schema_version": "..."}}
```

`source` names the origin of all `source_*` ids in the file; arx-free stores it next to them.

### athlete

```json
{"kind": "athlete", "source_user_id": "17", "first_name": "...", "last_name": "...", "gender": "m|f|null",
 "birth_date": "1980-05-17|null", "created_at": "2024-03-01T10:00:00|null"}
```

Only these fields leave the original database. No e-mail, no password or token columns, no cloud ids, no waiver.
The sentinel date `0001-01-01` of the original is exported as `null`. Text is decoded explicitly (charset NONE).

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

* `started_at` = `EXERCISEDATE`, naive local time of the set start. `protocol`: 3 = repetitions, 1 = countdown
  seconds, 0 = inroad percent; `protocol_parameter` = its value. `rep_scheme`: `LoopingRepSequence` or `StaticModeData`.
* Scalars are the original's columns (`ELAPSEDSECONDS`, `INTENSITY`, `MAXLOAD`, `CONCENTRICMAX`, `ECCENTRICMAX`);
  arx-free shows them as they were and marks them `metrics_algo_version = 0` ("as stored by the original").
* `config` keys as in the original: `StartPosition`, `EndPosition`, `StartToEndSpeed` / `EndToStartSpeed`
  {`InchesPerSecond`, `MaxInchesPerSecond`}, `AccelerationTime`, `DecelerationTime`, `PauseAfterEndPosition`,
  `PauseAfterStartPosition`, `PreExerciseTimer`.
* `events`: objects with `Time` (ISO with offset) and `Type`; `samples`: objects with `Time`, `Value`, optionally
  `RawValue`, `TaredValue`, `EncoderValue`, `SpeedValue` (and the `Has...` flags). A sample without `Value` or `Time`
  is dropped by the importer, not by the exporter.
* Sets with `DELETED` true are **not exported**. `HIDEFROMSTATS` becomes `hide_from_stats` (arx-free: "junk").
* `comparison_source_set_id` = `COMPARISONSET_ID`: the set whose curve was shown in grey.

### footer

```json
{"kind": "footer", "athletes": 12, "sets": 2381}
```

The importer refuses a file whose counts do not match (a cut-off copy).

## Import rules (arx-free, `arx_app/importer.py`)

* **Idempotent**: athletes by `(source, source_user_id)`, sets by `(source, source_set_id)`; the set's id in arx-free
  is `uuid5(arx-free namespace, "<source>:<source_set_id>")`. Importing the same or a newer export again adds only what
  is new and changes nothing that exists (notes or junk flags edited in arx-free stay).
* New athletes get the app's default display units. Athletes are never merged by name.
* Times inside a set become seconds since its `BeginSequence` event; the phase list (concentric = rising position)
  and the inroad figures are computed by arx-free's own engine from the verbatim samples.
* A set counts when it was not ended before completion; an unfinished set is kept and marked.
* **Remembered settings**: for every athlete and exercise without settings in arx-free, the latest counting
  repetition set provides start / end position, travel times (`|start - end| / speed + AccelerationTime`, rounded to
  0.5 s), holds, repetitions and the countdown (never below 5 s). The range is **not** marked as confirmed: the
  athlete confirms it once in arx-free before the first set (docs/safety.md).
