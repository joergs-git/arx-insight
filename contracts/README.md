# What arx-free and ARX Insight share - and how it stays the same on both sides

Two products, two repositories, one owner, different working sessions. Whatever both have to agree on - a format, a
rule, a number, a list - is a **contract**. Four rules keep the two from drifting apart:

1. **One owner per topic.** Every shared topic belongs to ONE project; a change starts there, in the contract, with a
   new version - never in code "in passing". The other project follows. (Table below.)
2. **Ask, do not rebuild.** arx-free does not re-implement what ARX Insight decides (plan, targets, rest, progression,
   beginner ramp) - it asks (`/api/report`, later `/api/brief`) and shows. A **port** is the exception for what must
   work at the machine, live and without Insight running; every port is listed here and tested against shared vectors.
3. **Numbers are data.** Thresholds, factors and codes that both sides use live in the contract file; each
   implementation asserts in a test that its own constants ARE the contract's (`tests/unit/test_effort.py` there,
   `tests/test_contracts.py` here).
4. **Differences are found by a machine, not by luck.** `contracts/MANIFEST.json` lists every contract file with its
   SHA-256. `python -m arx_tools.contracts` there, `tools/contracts.py` here - and the same check inside each test
   suite - verifies that (a) the folder is what the manifest says, and (b) when the sibling project is checked out next
   to this one, the things both carry are identical there: the exercise catalogue (codes, names, groups), the numbers
   of the fatigue rule as ARX Insight's source states them, and every contract file the sibling has adopted (identical
   copy, same hash). On a machine without the sibling (kiosk PC, fresh clone) that part is skipped (`ARX_SKIP_SIBLING=1`
   silences arx-free's check on purpose while the owner works on a change that spans both sides). An older file of a
   contract THIS project owns that the sibling still carries after it was retired here (modes-1 after modes-2) is
   history, not drift - the owner decides what the current version contains.

Found by reading the other side on 2026-09-21 - the reason this page exists: our bridge asked ARX Insight with `user=`
while its route wants `user_id=` (it could never have worked; our stand-in in the tests was more forgiving than the
real route), and the original's Inroad percentages are not on the scale of ARX Insight's `inroad_v3`.

## Register

| Topic | Owner (a change starts here) | Form | The other side | Kept in step by |
|---|---|---|---|---|
| History of the original software for arx-free | ARX Insight (exporter, and since v2 the route `GET /api/export?since=` on its local listener) | `arx-export-3.md` (gzip'd NDJSON, verbatim config / events / samples, imperial; the same lines by file or by route, `since` = only the sets that began after a `started_at`; the athlete line carries `language` / `display_units` and - v3, 2026-09-24 - `email` when Insight knows them - "Insight wins for the person, arx-free for the machine", owner 2026-09-23; v2 stays listed until v3 is adopted, v1 retired here 2026-09-24) | arx-free importer + `sync.py` (asks at start and when a session begins, imports what is new, shows the person's fields read-only; takes `email` into `athletes.email`) | importer tests, footer counts, format name in the header (`arx-export-1` names the unchanged line format) |
| What ARX Insight reads from arx-free's database: tables / columns / meanings (`sets.source` NULL = arx-free's own recording, `'arx-original'` = a copy), the curve format, the three settings original / arx-free / both without duplicates, and **who is who: the e-mail address first, the import link second, names never** | **ARX Insight** (the reader states what it relies on; the tables stay arx-free's) | `arx-free-sets-1.md` (2026-09-24) | arx-free keeps the columns and meanings stable (a migration that touches one needs a new version here first) and asks the athlete for the e-mail at session start when none is stored | hash in both manifests (adoption open); Insight's `tests/test_sources.py` against a database in that layout |
| Did the set produce fatigue? (`inroad_v3`, deep 20 / moderate 10 / borderline 2, four repetitions) | **ARX Insight** (`arx_detail.effort_v3`) | `effort-v3.md` + `effort-v3-vectors.json` (generated with Insight's own function) | arx-free port `arx_app/effort.py` | shared vectors, constants asserted, sibling check reads Insight's numbers |
| The words a person reads or hears for the fatigue rule: "Ermüdung im Satz" / "fatigue in the set", levels tief / mittel / leicht, "Ermüdungsziel" / "fatigue target" | **ARX Insight** (report, coach) | `vocabulary-1.md` | arx-free's live display and voice | hash in both manifests (adopted 2026-09-21) |
| A set's mode (movement x ending x phase): endings 3 reps / 1 time (Countdown) / 0 inroad (the original's Inroad Mode - it shows a zone, a high-water mark of the momentary force, and a person ends the set; the original never ends a set by itself) / 4 fatigue (arx-free's fatigue-target protocol - ends by itself; never from the original), Output = impulse, the machine's own inroad scale, field names, how a recommended hold runs on each product (`static/time` with a fatigue target; arx-free: `static/fatigue`) | **ARX Insight** (analysis, planning) | `modes-3.md` (v1 and v2 retired here after arx-free adopted their successors - 2026-09-22 / 2026-09-23; arx-free removes its `modes-2.md` next) | arx-free's set records, live view, compat views (write 4, never 0, for a fatigue-ended set) | hash in both manifests (v3 adopted 2026-09-22) |
| Did the hold produce fatigue? (`hold-1`: the held force = 3-s mean against the sustained force = the lowest force inside a full 3-s window, at its best; a drop counts after a second; letting go ends the hold; live ending at the target) | **ARX Insight** (`arx_detail.hold_v1`) | `hold-1.md` + `hold-1-vectors.json` (9 cases incl. the owner's real test hold on the original) | arx-free ends a hold live with the same rule (ending 4 for holds); Insight judges holds from the curve with it | shared vectors, constants asserted (adoption open) |
| Exercise catalogue: codes, names, groups | today two files (`config/exercises.json`, Insight's `exercises.json`) - planned as contract `exercises-1`, owner arx-free | - | each side adds its own fields (arx-free: rest position, clips, setup; Insight: targets, limiters, joints) | sibling check compares code / name / group |
| Setup and cues per exercise, German + English (`exercise-coaching-1.json`) | **arx-free** (shown and spoken at the machine) - owner agreed 2026-09-21 | contract file, identical copy in Insight (adopted 2026-09-21) | Insight's plan rows | hash in both manifests |
| Marks a set carries: planned effort (on-ramp), reason of an early stop (planned, extends `arx-export`) | arx-free (producer) | fields in the set record / export | Insight must not count such sets as missed targets | contract version in the file header |
| Facts before a session (planned `insight-brief-1`) | **ARX Insight** | `GET /api/brief?user_id=` - ready-made sentences, numbers in kg | arx-free's "before you start" pop-up (today: tolerant extraction from `/api/report`) | schema test on both sides |
| The Insight trainer defines the sets (planned, "in due course") | **ARX Insight** | plan -> protocol, repetitions, travel times, pauses, fatigue target per exercise | arx-free applies after the athlete's confirmation | contract + version handshake |
| Joint routine for several athletes (`group-session-1`, planned M7) | ARX Insight | planning logic and setup attributes live in Insight | arx-free shows and lets edit | - |
| Compat views `"ExerciseSet"` / `"User"` (was planned as M6) | - | superseded 2026-09-24 by `arx-free-sets-1`: Insight reads arx-free's own tables directly and hands the recordings to its decoders in the original's shapes - no views to keep in step | - | - |
| Evidence ("is that scientifically right?") | **ARX Insight** (`science.json`) | one verified list | `docs/coaching-knowledge.md` refers to its topics, keeps no second list | review |

Observed on the original by the owner, not yet in a contract (goes into `modes-4` with the next change - an adopted
contract is never edited in place): the original computes its Inroad **live as the momentary force against the maximum
reached so far in the set** - any repetition, no averaging (2026-09-22). Insight's `inroad_machine` reads the same
scale from the recording as best repetition peak -> last repetition peak (the momentary value dips inside every
repetition; the peaks are what a person judges the zone by).

**Only in ARX Insight, never rebuilt here:** what to train when, targets and progression, rest between sessions,
what changes after missed targets, how long a beginner ramps, the AI coach.
**Only in arx-free, never rebuilt there:** everything that moves the machine and its safety rules, the live coach at
the machine (it USES the shared fatigue rule), recording a set, the touch UI.

Planned, when both sides speak contracts: a **version handshake** - ARX Insight names the contract versions it
implements in its answers, arx-free shows a warning in Admin when they differ.

## Working across the two repositories

* The owner's working tree of the sibling project is never touched. A change there is made on its own branch in a
  separate worktree (like the exporter, `../arx-insight-export`), pushed, and merged by the owner.
* Reading the sibling's source to check a contract is fine and wanted; importing its code into this project's tests is
  not (the vectors were generated once, with a one-off script outside both repositories).
* Open cross-repo work: `tasks/cross-repo.md`.

Units in every contract: **imperial (lb, inch)**, exactly like the original data - except where a contract says
otherwise (ARX Insight's report speaks kilograms; the screen converts). Display conversion is never part of a contract.
