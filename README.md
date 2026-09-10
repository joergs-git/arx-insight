<!-- ARX Insight -->
<p align="center">
  <img src="docs/hero.svg" alt="ARX Insight" width="100%">
</p>

   <img src="docs/spin-omni-3.webp" alt="ARX Insight" width="49%">    <img src="docs/spin-alpha-3.webp" alt="ARX Insight" width="49%">

<h1 align="center">ARX Insight</h1>

<p align="center">
  <b>Local, private strength analytics for the ARX adaptive-resistance machine.</b><br>
  Reads your training data straight off the machine, turns it into real sport-science metrics,<br>
  and gives you AI-written feedback — all on your own PC, with your own API key.
</p>

<p align="center">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20(macOS%2FLinux%20too)-2C6E8F">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3E86A8">
  <img alt="license" src="https://img.shields.io/badge/license-CC0%20(public%20domain)-5E9138">
  <img alt="status" src="https://img.shields.io/badge/status-experimental-C9552F">
</p>

---

## Why this exists

ARX gives you a brilliant machine, but its analysis lives only in the cloud, with no way to
dig into your own numbers over time. Yet all of your data sits **locally** on the PC next to the
machine. **ARX Insight** is the missing local companion: it opens a read-only copy of that data
and shows you what actually happened — and what to do next.

> Not affiliated with ARX. Independent, community-made, experimental. Not medical advice.

## What you get

- 📈 **Force curve of your strongest set** — the full ~20 Hz curve, with the peak of every rep.
- 🔥 **Effort / inroad** — did the set reach deep fatigue, or were the early reps sub-maximal?
- 🧭 **Progress per exercise** — best set per day, with trend and a cautious forecast, on two axes (last sessions / last days).
- 🧍 **Whole-body index** — a self-referenced score of how close you are to your own bests, across Push / Pull / Drive.
- 🛌 **Load & recovery** — intelligently *effort-aware*: the 48–72 h rule only applies after a truly maximal session; a sub-maximal day needs far less.
- 🗓️ **Next-session suggestion** — a ~15-minute, muscle-group-balanced plan matched to your goal.
- ⏱️ **Training time & work** — motivational totals, per week / month / year.
- ⚠️ **Injury-aware** — flag a shoulder, knee, etc. as *careful* or *avoid*, and the plan won't push it.
- 🤖 **AI analysis** in plain language, using **your own** Claude API key (only aggregated, name-free numbers are sent).
- 🌍 **English / German**, **lb-inch / kg-cm**, big touch-friendly UI, one-click **PDF**.

<p align="center"><i>Example report (anonymized):</i></p>
<p align="center">
  <!-- Add a real screenshot here after your first run, e.g. docs/example-report.png -->
  <img src="docs/hero.svg" alt="Example report preview" width="80%">
</p>

> 📷 **Make it yours:** drop a photo of your ARX machine into `docs/` and a screenshot of your
> first report, then reference them here. (We ship no ARX photo — those belong to their owners.)

## Install on Windows (plug and play)

No IT knowledge needed.

1. On the GitHub page click **Code → Download ZIP**, then unzip it anywhere (e.g. your Desktop).
2. Open the unzipped folder and **double-click `Install ARX Insight.bat`**.
3. That's it. The installer will, fully automatically:
   - install Python if it's missing,
   - set up a private environment and the two required packages,
   - download the Firebird database client if needed,
   - **find your ARX database automatically** (or ask you to pick the file `DB.FDB4`),
   - put an **“ARX Insight” shortcut on your Desktop**, and
   - open the app in your browser.

Next time, just use the Desktop shortcut (or `Start ARX Insight.bat`).

### First screen

On first launch you set language, units, your weekly training target, and — optionally — your
Claude API key (for the AI analysis). Then search a person by name, set a training goal with two
simple sliders, and read the report. **Exit** returns you to the search screen.

## Run it manually (macOS / Linux / advanced)

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python arx_app.py --db "/path/to/Resources/DB.FDB4"
```

Or generate just the report data (no UI): `python arx_report.py --db "..." --ai`.

## How it works

| Piece | Role |
|---|---|
| `arx_report.py` | Read-only engine: opens a **copy** of the Firebird DB, computes every metric, optionally calls Claude |
| `arx_app.py` | Tiny local web server + the touch UI in `web/` |
| `exercises.json` | ARX Omni catalog: exercise code → name / muscle group / compound-isolation |
| `config.json`, `goals.json` | Your settings and per-person goals (kept **out** of git) |

The ARX database is never modified — the tool always works on a temporary copy.

## Privacy

Everything runs on your machine. Your API key lives only in the local, git-ignored `config.json`.
The AI request contains **only aggregated, name-free metrics** — never a person's name. The
database, your keys, goals and generated reports are all git-ignored.

## Disclaimer

ARX Insight is an **experimental aid**, not medical, therapeutic, or professional training advice.
**No liability** is accepted; anything you derive from it is at your own risk. If you feel pain or
suspect an injury, consult a physician or qualified trainer.

## License

[CC0 1.0](LICENSE) — public domain. Do whatever you like. No warranty.
