# Contract `insight-progress-1` - the shape of ARX Insight's progress factors that arx-free shows

Version 1 (2026-09-24, ARX Insight v0.27.0). Owner: **ARX Insight** (the rule and the numbers are its); arx-free
DISPLAYS them in its trend view (arx-free 0.12.0, `docs/feature-progress-trend.md`) and never rebuilds the rule.
arx-free's request 8: pin the fields it relies on, so that a rename is a contract version, not a silent break.

Where: `GET /api/report?user_id=<the original's user id>` on ARX Insight's local listener (loopback, `X-ARX-Token:
local`), the report's `history.progress_factors` - plus `today` at the report's top level. No new route. arx-free reads
it when the history opens, caches it for the session, draws the points and its own moving average over a window the
kiosk chooses (display only - not part of this contract), shows the numbers and Insight's sentence, and falls back to
its own per-set count when Insight is not running. Nothing is written back.

## The pinned shape

```json
{"overall": {"strength_index": 1.08, "change_pct": 8.0, "exercises_in_index": 6, "exercises_total": 8,
             "interp": {"code": "overall_index", "params": {...}, "text": {"meaning": "...", "action": "..."}}},
 "exercises": [
   {"name": "Row", "ex": 3, "status": "progressing", "n": 7,
    "change_pct": 12.0, "change_4w_pct": 5.0, "change_quarter_pct": null, "rate_pct_per_week": 1.2, "rate_quality": "good",
    "span_days": 34, "baseline_date": "2026-08-20", "latest_date": "2026-09-23",
    "points": [{"date": "2026-08-20", "index": 1.0}, {"date": "2026-08-24", "index": 1.03}],
    "interp": {"code": "progress_progressing", "params": {...}, "text": {"meaning": "...", "action": "..."}}}]}
```

* `overall.strength_index`: the whole-body index, 1.00 = the athlete's own baseline, geometric mean over the exercises
  with an index (`exercises_in_index` of `exercises_total`); null while too few exercises are comparable.
  `overall.change_pct` = the index as a percentage change.
* `exercises[]` in Insight's attention order (regressing first). `name` = the catalogue name (the same words as
  `config/exercises.json`), `ex` = the catalogue code, `n` = comparable training days used, `status` one of
  `progressing`, `progressing_context`, `stable`, `stable_context`, `plateau`, `plateau_context`, `regressing`,
  `regressing_context`, `familiarisation`, `not_comparable`, `insufficient`. `change_pct` (baseline -> latest),
  `change_4w_pct` (last 28 days), `change_quarter_pct` (last 91 days), `rate_pct_per_week` (the fitted slope; null when
  `rate_quality` is `low`), `rate_quality` (`good` | `fair` | `low` | null), `span_days`, `baseline_date`,
  `latest_date` - each null when not known. `points[]` = one per comparable day, `index` 1.0 = the exercise's own
  baseline (a point may carry more fields; `date` and `index` are pinned).
* `interp` = one of Insight's self-explaining items: `{code, params, text: {meaning, action}}` - the sentence is
  rendered ONCE, in the language of the report (`cfg.language` of that athlete's profile, else the device's), not in
  both languages. `code` is stable per status (`progress_<status>`, `overall_index`, `overall_index_pending`).
* Numbers are in Insight's units of record (percent, dates ISO, index dimensionless) - no kg / lb in this block.

Everything not listed above may change without a new version. A rename or a change of meaning of a pinned field is
`insight-progress-2`. ARX Insight's `tests/test_contracts.py` asserts this shape on a fixture report.
