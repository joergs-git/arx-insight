# Contract `arx-free-sets-3` - what ARX Insight reads from arx-free's database

Version 3 (2026-09-24, ARX Insight v0.28.0) - supersedes `arx-free-sets-2` (2026-09-24). What is new (arx-free's
request 9, the owner's live test with a second person): **an athlete that exists only in arx-free is a person of ARX
Insight** - listed, reported, planned and coached like everybody. ARX Insight reads five more columns of `athletes`
(`first_name`, `last_name`, `gender`, `birth_date`, `created_at`), gives such a person a STABLE id derived from
arx-free's athlete id (section "Persons that exist only in arx-free"), and its user list `GET /api/users` carries
`source` and `arx_free_ids` so that arx-free can resolve ANY athlete to the Insight id it asks the report for. The
export gets a `person` line for them (contract `arx-export-6`). `arx-free-sets-2.md` stays in both folders until
arx-free has adopted this file.

Version 2 (2026-09-24, ARX Insight v0.27.0) added the column **`sets.coach_json`** (arx-free schema 5, `PRAGMA
user_version` 5) - the live coach's notes about a set arx-free recorded itself, NULL on imported copies. ARX Insight
reads it when it is there (a file without the column is still read - the column is optional for the layout check) and
computes per cue group what the sentence did to the force (report `coach_effects`, chapter 3).

Version 1 (2026-09-24, ARX Insight v0.24.0). Owner: **ARX Insight** - the reader states what it relies on. The tables
are arx-free's; this contract pins the columns and meanings ARX Insight depends on, so arx-free keeps them stable (or
this file gets a new version first, in ARX Insight, before a migration changes one of them).

Direction: arx-free -> ARX Insight, **read-only, one way**. Why: the sets arx-free records itself never reach the
original software's Firebird database. With the device setting `sources` ARX Insight reads them from arx-free's SQLite
file - the original's database, arx-free's file, or both - **every set from the software that recorded it, never
twice**. Nothing of it is ever written back, and arx-free's copies of the original's sets are never exported back to
arx-free (the export `arx-export` stays original-only; the copies would come around as duplicates).

## The three settings

| `sources` (config.json, this PC only) | sets come from | persons |
|---|---|---|
| `original` (default) | the original's Firebird database | the original's `User` table |
| `arx-free` | arx-free's file: its own recordings AND the copies it imported (the full history from one file; a copy keeps the original's set id) | the original's `User` table + the persons that exist only in arx-free (v3) |
| `both` | the Firebird database + arx-free's OWN recordings (`sets.source IS NULL`); the copies are skipped - they ARE the original's sets | as `arx-free` |

## How the file is read

* The file is `data/arx-free.sqlite` of the arx-free installation (kiosk: `Documents\arx-free\data\`), SQLite in WAL
  mode. ARX Insight opens it read-only (`mode=ro`, a plain open as the fallback when the WAL's `-shm` is not there),
  copies it INTO MEMORY with SQLite's backup API - one consistent snapshot that includes the WAL, where the newest
  sets live - releases the file handle at once and never writes. arx-free keeps writing undisturbed. The snapshot is
  reused for 30 s while the file (and its `-wal`) look unchanged.
* Layout check: `PRAGMA user_version` (4 when this was written) and the columns below via `PRAGMA table_info`. A file
  without one of them is reported as "unknown layout" - never guessed, never a broken report (the original's data are
  shown with a note). Additional columns and tables are ignored.
* The path is found in the usual place or set in ARX Insight's settings (`arx_free_db`).

## What is read (tables and columns; anything else is ignored)

* `athletes`: `id`, `email`, `source`, `source_user_id`, `deleted_at`, and since v3 `first_name`, `last_name`, `gender`,
  `birth_date`, `created_at` (in arx-free's schema since its first version - an older file still passes the layout check)
* `sets`: `id`, `athlete_id`, `session_id`, `exercise_code`, `started_at`, `mode`, `protocol`, `protocol_value`,
  `config_json`, `elapsed_s`, `junk`, `intensity_lb`, `max_lb`, `max_c_lb`, `max_e_lb`, `source`, `source_set_id`,
  `updated_at`, `deleted_at`
* `set_curves`: `set_id`, `samples_gz`, `events_json`

## The coach's notes (v2): `sets.coach_json`

JSON text, written by arx-free's app server when it stores a set it recorded (schema 5):

```json
{"on_ramp": 0, "care": false, "jerky_starts": 0, "asked_all_right": false,
 "cues": [{"id": "e_resist", "group": "eccentric", "kind": "general", "rep": 3, "t": 21.5}],
 "fatigue": {"reps": 8, "inroad_v3": 12.4, "class": "moderate", "target_pct": 10, "reached": true,
             "pacing_deficit_pct": 3.1, "live_drop_pct": 11}}
```

* `cues[]`: every sentence the live coach said in the set - `id` and `group` from arx-free's `config/coach-cues.json`,
  `kind` = `safety` | `event` | `general` | `announce` | `on_ramp`, `rep` = the repetition the sentence fell in, `t` =
  seconds since the set's first moving phase (the coach's t0 = the first `BeginFirstHalf`). The verdict after the set
  is NOT in the list (the app server chooses it after the notes are closed).
* `on_ramp` (0 = a normal set, 1.. = the athlete's first sets of the exercise), `care` (Insight's check-in flagged the
  exercise), `jerky_starts`, `asked_all_right` (the force was gone for a moment) and `fatigue` (the coach's own live
  reading on the effort-v3 scale) are arx-free's; Insight judges the set with its own rule as before.
* What ARX Insight does with it: `arx_history.coach_effects` - for a `general` / `event` cue in repetition r the change
  of that repetition's phase mean against repetition r - 1, compared with the athlete's typical change at the same
  transition in sets of the same exercise without a cue there; the median difference per cue group from
  `COACH_EFFECT_MIN_N` (5) cues on. Descriptive - a first "does this sentence help" number, never a proof.

## Meanings ARX Insight relies on

* `sets.source` **NULL = a set arx-free recorded itself**; `'arx-original'` with `source_set_id` = a copy of the
  original's set with that id, imported through `arx-export`. The same words on `athletes` (`source`,
  `source_user_id` = the original's user id) mark an athlete arx-free's import created.
* A row with `deleted_at` is gone. `junk` = hidden from statistics (the original's `HIDEFROMSTATS`).
* `protocol` labels -> the original's `ExerciseSet.PROTOCOL` codes: `Reps` 3, `Countdown` 1, `Inroad` 0,
  `FatigueTarget` 4 (arx-free's own ending, contract `modes-3`). An unknown label is shown as "unknown", never
  silently as repetitions. `mode` `Static` = a hold (the original's `StaticModeData`), anything else a repetition
  sequence.
* `started_at` = naive local time of the set start with seconds (`2026-09-23T21:02:47`); `updated_at` = last change.
* `config_json` = the original's rep-scheme keys, verbatim (`StartPosition`, `EndPosition`, `StartToEndSpeed` /
  `EndToStartSpeed` {`InchesPerSecond`, `MaxInchesPerSecond`}, `AccelerationTime`, `DecelerationTime`,
  `PauseAfterEndPosition`, `PauseAfterStartPosition`, `PreExerciseTimer`), in inches and seconds.
* `set_curves.samples_gz` = gzip of JSON `{"t": [...], "force_lb": [...], "raw_lb": [...], "pos_in": [...]}` - columns
  of equal length, `t` in seconds since the set's `BeginSequence`, force in lb, position in inches (`raw_lb` unused).
  `events_json` = `[{"Time": <seconds since BeginSequence>, "Type": <the original's event name>, "AdditionalData":
  ...}]` - `BeginSequence`, `BeginRep`, `BeginFirstHalf`, `BeginPauseAfterFirstHalf`, `BeginSecondHalf`,
  `BeginPauseAfterSecondHalf`, `EndRep`, `EndSequence` / `SequenceEndedBeforeCompletion`.
* The scalars mean what the original's columns mean: `intensity_lb` = time-averaged force (`INTENSITY`), `max_lb`
  (`MAXLOAD`), `max_c_lb` (`CONCENTRICMAX`), `max_e_lb` (`ECCENTRICMAX`), `elapsed_s` (`ELAPSEDSECONDS`).
* ARX Insight turns a recording into the ORIGINAL'S shapes (samples with absolute `Time` = `started_at` + `t`,
  `Value`, `EncoderValue`; events with `Time`, `Type`) and judges it with the same code as a set of the original
  (effort-v3, hold-1, modes-3, the ROM / tempo comparability). A set's id in ARX Insight = the original's integer id for
  a copy, arx-free's uuid text for its own recording.

## Who is who - the e-mail address is the key

A person is identified across arx-free, ARX Insight and any future cloud (the data of many machines) by the **e-mail
address, the only unique world key** (owner 2026-09-24). Local ids are technical links. ARX Insight maps an arx-free
athlete to one of its persons in this order:

1. `athletes.email` equals the e-mail in a person's ARX Insight profile (case-insensitive, trimmed) - when two
   profiles carry the same address, **the original's user wins** (the smaller id; a person derived from arx-free never
   takes an athlete away from an original user);
2. else `athletes.source = 'arx-original'` and `source_user_id` = the original's user id (the link arx-free's import
   made from `arx-export`);
3. else (v3) the athlete IS a person of ARX Insight of its own, with the derived id below.

Names never identify anyone. Enrichment, one simple way per product: ARX Insight asks in the profile; **arx-free asks
the athlete for the e-mail when a session starts and none is stored** (request to arx-free, open since v1). Typed
once, it travels: ARX Insight's export puts it on the athlete line and (v3) on the `person` line (contract
`arx-export-6`, "Insight wins for the person"). The address stays on the owner's machines and never reaches the AI.

## Persons that exist only in arx-free (v3)

* **The id.** `uid = 1000000 + int(sha256(athlete_id)[:8], 16) % 1000000000` (`arx_sources.derived_uid`; ids of
  the original are small, these never collide with them; below Firebird's INTEGER and 2^53). Should two uuids of one
  file hash alike (odds about n^2 / 2e9), the athletes are walked in the order `created_at, id` and the later one
  probes upward - the earlier keeps the id it always had (`assign_ids`). Nothing is stored: the CLI, the app and a
  copy of the file agree, and every file keyed by a user id (profile, check-ins, plan ledger, boards, chats, phone
  access codes) works for such a person as for everybody. arx-free never computes the id itself - it asks (below).
* **What the person is made of.** Name from `first_name` + `last_name`; sex from `gender` (`m` / `f` / `male` /
  `female` / `männlich` / `weiblich`, else unknown); age from `birth_date` (ISO date text, else unknown); `created_at`
  as the "since" date. arx-free's athlete form asks for none of gender and birth date today - both are simply unknown
  then (the age band steers only the wording's frame). E-mail stays arx-free's `athletes.email`; the e-mail typed into
  the person's ARX Insight profile is the person's key like for everybody.
* **Only with arx-free's data.** Such persons exist in the settings `arx-free` and `both`; in `original` they are
  counted in the settings window and nothing else. Their sets are arx-free's own recordings (`source IS NULL`).
* **When the athlete later links to an original user** (the trainer types the e-mail into the original user's
  profile, the next export carries it, arx-free's importer writes `source_user_id`): rule 1 / 2 give the original's id
  from then on - the sets move there, the derived person vanishes from the list; whatever was stored under the derived
  id (profile, ledger, boards, an access code) stays in the files under the old key and is not merged.
* **`GET /api/users`** (local listener, `X-ARX-Token: local`; the trainer's phone role gets the rows without
  `arx_free_ids` and with the birth year only, as before): every row carries `source` (`original` | `arx-free`) and
  `arx_free_ids` (the arx-free athlete ids that belong to this person - a list, an original user may own several;
  `[]` when none). A derived person: `{"id": 1234567890, "name": "...", "gender": "", "birthdate": null, "created":
  "2026-09-24", "source": "arx-free", "arx_free_ids": ["<uuid>"]}`.
* **arx-free's part** (its `insight_bridge`): resolve the Insight id of ANY athlete - own or imported - by asking
  `/api/users?q=` for the row whose `arx_free_ids` contains the athlete's id, uncached (a person can vanish when the
  setting changes), and use that row's `id` as `user_id` for the report, the plan, the brief, the progress factors and
  the framed page; `source_user_id` of an imported athlete stays the fallback when Insight is older than v0.28.0. The
  NOT_KNOWN sentence is then only right while Insight does not run or does not read arx-free's file.
