# Contract `vocabulary-1` - the words the athlete reads for the fatigue rule

Version 1 (2026-09-21). **Owner: ARX Insight** (the report and the AI coach); arx-free adopts the same words in its
live display and live coach. Code labels of `effort-v3` (`deep / moderate / submax`, `inroad_v3`) stay as they are
in data, exports and payloads - this contract is only about what a PERSON sees or hears.

Why: the machine and the report must say the same thing about the same set. "Inroad", "Kraftabfall", "moderat" and
"submax" were unclear for anybody (owner, 2026-09-21); the original software's "Inroad" is a different scale on top.

| Concept | English (athlete) | Deutsch (Sportler) | Code / data |
|---|---|---|---|
| the measure | fatigue in the set | Ermüdung im Satz | `inroad_v3` (%) |
| class 20 % and more | deep | tief | `deep` |
| class 10-19 % | medium | mittel | `moderate` |
| class below 10 % | light | leicht | `submax` |
| the goal's minimum | fatigue target (e.g. "fatigue target >= 20 %") | Ermüdungsziel | `EFFORT_TARGETS[..].inroad_min` (10 / 20) |
| reached | reached | erreicht | `inroad_v3 >= target - BORDERLINE` |
| below the target | a real load, but half the stimulus | eine echte Belastung, aber nur der halbe Reiz | a miss (never "no stimulus") |
| light on purpose | light, no target number | leicht, ohne Zielzahl | rows planned sub-maximal |
| never shown to a person | "inroad", "force drop", "sub-max" | "Inroad", "Kraftabfall", "submax", "moderat" | |

One-sentence explanation, shown once per screen where the measure appears (ARX Insight: chapter 1, code
`fatigue_glossary` in `meanings.json`; arx-free: the live view):

* EN: "Fatigue in the set = the force lost from the strongest stretch of the set to its last repetitions. Deep (20 %
  and more) means close to failure - the full stimulus for muscle size; medium (10-19 %) is a real load but half the
  stimulus; light (under 10 %) is technique or a strength test." + "Your goal asks for {deep|medium} - at least
  {target} % in every set that counts."
* DE: "Ermüdung im Satz = der Kraftverlust vom stärksten Abschnitt des Satzes bis zu den letzten Wiederholungen.
  Tief (ab 20 %) heißt nahe am Versagen - der volle Reiz für Muskelaufbau; mittel (10-19 %) ist eine echte
  Belastung, aber nur der halbe Reiz; leicht (unter 10 %) ist Technik oder Krafttest." + "Dein Ziel verlangt
  {tief|mittel} - mindestens {target} % in jedem Satz, der zählt."

The original software's Inroad Mode (30-40 % of the set's maximum, momentary force) is a different scale: never show
both under one name. Cues to the athlete follow the ARX Academy manner without copying it: all-out from the first
repetition, the force built fast but smoothly - the machine sets the speed, never a jerk.
