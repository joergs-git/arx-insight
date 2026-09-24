# Contract `vocabulary-2` - the words the athlete reads for the fatigue rule

Version 2 (2026-09-24, ARX Insight v0.25.0) - supersedes `vocabulary-1` (2026-09-21). **Owner: ARX Insight** (the
report and the AI coach); arx-free adopts the same words in its live display and live coach. Code labels of
`effort-v3` (`deep / moderate / submax`, `inroad_v3`) stay as they are in data, exports and payloads - this contract
is only about what a PERSON sees or hears. `vocabulary-1.md` stays in both folders until arx-free has adopted this file.

What is new in v2 (the owner's motivation review, `motivation-plan-2026-09-24/`, verified literature): a set below
the target is no longer "half the stimulus" - it is named as a real load with the **gap to the target as a number**;
and four framing rules for every sentence about a set. Why: gain-framed wording beats loss-framed for prevention
behaviour (Gallagher 2012), positive-first true feedback raises performance in non-elite athletes while negative
feedback does not (García 2019), an offer moves behaviour where an order does not (Ntoumanis 2021), and "half" says
what is missing where a number says what to do (science.json topic `adherence_and_motivation`, 13 references).

Why the contract exists at all: the machine and the report must say the same thing about the same set. "Inroad",
"Kraftabfall", "moderat" and "submax" were unclear for anybody (owner, 2026-09-21); the original software's "Inroad"
is a different scale on top.

| Concept | English (athlete) | Deutsch (Sportler) | Code / data |
|---|---|---|---|
| the measure | fatigue in the set | Ermüdung im Satz | `inroad_v3` (%) |
| class 20 % and more | deep | tief | `deep` |
| class 10-19 % | medium | mittel | `moderate` |
| class below 10 % | light | leicht | `submax` |
| the goal's minimum | fatigue target (e.g. "fatigue target >= 20 %") | Ermüdungsziel | `EFFORT_TARGETS[..].inroad_min` (10 / 20) |
| reached | reached | erreicht | `inroad_v3 >= target - BORDERLINE` |
| below the target | a real load - the target sits N % higher (N = target - fatigue in the set) | eine echte Belastung - das Ziel liegt N % höher | a miss (never "no stimulus", never "half the stimulus" / "halber Reiz") |
| the repeat after a miss | once more, if you like - your call | nochmal, wenn du magst - deine Wahl | an offer, never an order ("once more - properly!") |
| light on purpose | light, no target number | leicht, ohne Zielzahl | rows planned sub-maximal |
| never shown to a person | "inroad", "force drop", "sub-max", "half the stimulus", "strength test" as a verdict | "Inroad", "Kraftabfall", "submax", "moderat", "halber Reiz", "Krafttest" als Urteil | |

Four rules for every sentence about a set (both products, report and live coach):

1. **Positive first, and true.** Name what WAS there (the force, the repetitions, "above last time") before the gap;
   claim only what the rule measured - "above last time" is not "your best" unless it is one.
2. **Gain frame.** Say what the effort gains, never what the miss costs; while repetitions remain, the target is
   "still in reach", never "gone".
3. **The athlete's own reference.** The comparison is the athlete's own last comparable set (the grey line, last
   time's number), never another person, never an ideal.
4. **Offers, not orders.** A repeat, a second set, a harder target are offered ("if you like", "your call"); the
   decision is the athlete's. Sarcasm and put-downs only in an opt-in style the athlete chose.

One-sentence explanation, shown once per screen where the measure appears (ARX Insight: chapter 1, code
`fatigue_glossary` in `meanings.json`; arx-free: the live view):

* EN: "Fatigue in the set = the force lost from the strongest stretch of the set to its last repetitions. Deep (20 %
  and more) means close to failure - the full stimulus for muscle size; medium (10-19 %) is a real load, the full
  stimulus starts at 20 %; light (under 10 %) is technique or a strength test." + "Your goal asks for {deep|medium} -
  at least {target} % in every set that counts."
* DE: "Ermüdung im Satz = der Kraftverlust vom stärksten Abschnitt des Satzes bis zu den letzten Wiederholungen.
  Tief (ab 20 %) heißt nahe am Versagen - der volle Reiz für Muskelaufbau; mittel (10-19 %) ist eine echte
  Belastung, der volle Reiz beginnt bei 20 %; leicht (unter 10 %) ist Technik oder Krafttest." + "Dein Ziel verlangt
  {tief|mittel} - mindestens {target} % in jedem Satz, der zählt."

The original software's Inroad Mode (30-40 % of the set's maximum, momentary force) is a different scale: never show
both under one name. Cues to the athlete follow the ARX Academy manner without copying it: all-out from the first
repetition, the force built fast but smoothly - the machine sets the speed, never a jerk.
