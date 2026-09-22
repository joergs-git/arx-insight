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
| History of the original software for arx-free | ARX Insight (exporter) | `arx-export-1.md` (gzip'd NDJSON, verbatim config / events / samples, imperial) | arx-free importer | importer tests, footer counts, format name in the header |
| Did the set produce fatigue? (`inroad_v3`, deep 20 / moderate 10 / borderline 2, four repetitions) | **ARX Insight** (`arx_detail.effort_v3`) | `effort-v3.md` + `effort-v3-vectors.json` (generated with Insight's own function) | arx-free port `arx_app/effort.py` | shared vectors, constants asserted, sibling check reads Insight's numbers |
| The words a person reads or hears for the fatigue rule: "Ermüdung im Satz" / "fatigue in the set", levels tief / mittel / leicht, "Ermüdungsziel" / "fatigue target" | **ARX Insight** (report, coach) | `vocabulary-1.md` | arx-free's live display and voice | hash in both manifests (adopted 2026-09-21) |
| A set's mode (movement x ending x phase): endings 3 reps / 1 time (Countdown) / 0 inroad (the original's Inroad Mode - its software ends the set automatically when the force no longer reaches the zone) / 4 fatigue (arx-free's fatigue-target protocol; never from the original), Output = impulse, the machine's own inroad scale, field names | **ARX Insight** (analysis, planning) | `modes-2.md` (v1 superseded 2026-09-21 and retired after adoption) | arx-free's set records, live view, compat views (write 4, never 0, for a fatigue-ended set) | hash in both manifests (v2 adopted 2026-09-22) |
| Exercise catalogue: codes, names, groups | today two files (`config/exercises.json`, Insight's `exercises.json`) - planned as contract `exercises-1`, owner arx-free | - | each side adds its own fields (arx-free: rest position, clips, setup; Insight: targets, limiters, joints) | sibling check compares code / name / group |
| Setup and cues per exercise, German + English (`exercise-coaching-1.json`) | **arx-free** (shown and spoken at the machine) - owner agreed 2026-09-21 | contract file, identical copy in Insight (adopted 2026-09-21) | Insight's plan rows | hash in both manifests |
| Marks a set carries: planned effort (on-ramp), reason of an early stop (planned, extends `arx-export`) | arx-free (producer) | fields in the set record / export | Insight must not count such sets as missed targets | contract version in the file header |
| Facts before a session (planned `insight-brief-1`) | **ARX Insight** | `GET /api/brief?user_id=` - ready-made sentences, numbers in kg | arx-free's "before you start" pop-up (today: tolerant extraction from `/api/report`) | schema test on both sides |
| The Insight trainer defines the sets (planned, "in due course") | **ARX Insight** | plan -> protocol, repetitions, travel times, pauses, fatigue target per exercise | arx-free applies after the athlete's confirmation | contract + version handshake |
| Joint routine for several athletes (`group-session-1`, planned M7) | ARX Insight | planning logic and setup attributes live in Insight | arx-free shows and lets edit | - |
| Compat views `"ExerciseSet"` / `"User"` (planned M6) | arx-free | SQLite views with the original's column names | Insight's SQL and decoders run unchanged | Insight's own tests against an arx-free database |
| Evidence ("is that scientifically right?") | **ARX Insight** (`science.json`) | one verified list | `docs/coaching-knowledge.md` refers to its topics, keeps no second list | review |

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
