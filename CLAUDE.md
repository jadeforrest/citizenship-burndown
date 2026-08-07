# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two independent, stdlib-only Python scripts for tracking Canadian citizenship
proof-of-citizenship processing times:

- [burndown2.py](burndown2.py) — interactive CLI that estimates how long a
  backlog will take to clear. The user pastes a comma-separated list of
  monthly backlog counts (newest first, oldest last); the script infers the
  monthly processing rate and prints low/likely/high time-remaining estimates.
- [flpt_watch.py](flpt_watch.py) — polls IRCC's published processing-times
  feed hourly, tracks the `citizen-proofs-2025/07` people-ahead figure and
  wait-time estimate, and beeps once when `flpt_lastupdated` changes (i.e.
  IRCC has refreshed the feed).

There is no build step, package manifest, test suite, or linter configured in
this repo.

## Running

```bash
python3 burndown2.py
```

`burndown2.py` is interactive: prompts for one line of input via `input("> ")`,
then prints the analysis to stdout.

To exercise the analysis logic without the interactive prompt, pipe input in:

```bash
echo "10200, 10500, 11300, 11700, 13000, 14500, 15800" | python3 burndown2.py
```

```bash
python3 flpt_watch.py          # run forever, checking hourly
python3 flpt_watch.py --once   # check once and exit (cron/testing)
```

`flpt_watch.py` fetches via `curl` (shelled out) rather than `urllib` —
Canada.ca's CDN appears to block Python's default TLS fingerprint but allows
curl's. State (last `flpt_lastupdated` seen and the latest backlog figure) is
persisted to `flpt_state.json` next to the script, which is gitignored since
it's just a local cache.

## Code structure

Everything lives in [burndown2.py](burndown2.py) as a small pipeline of pure
functions plus a `main()` that wires them to stdin/stdout:

1. `parse_numbers` — tokenizes the raw input line (commas/whitespace/pipes/tabs
   as separators) into floats.
2. `extract_backlog` — heuristically detects and strips an interleaved
   counter/index column (e.g. pasted data shaped like `1 2600 1 2700 2 7800`)
   using a ratio guard (`max(markers) * 5 <= min(values)`) so genuine
   single-column data isn't mistaken for paired data.
3. `analyze` — the core logic: reverses input to chronological order, computes
   month-over-month processed counts, derives mean/median/stdev/recent-pace
   rates, and prints fast/likely/median/recent/slow time-remaining scenarios
   plus best/likely/worst absolute bounds.
4. `fmt_months` — human-readable month/year formatting for a rate result.

When changing the parsing heuristics in `extract_backlog`, keep in mind it's a
best-effort guess with no way to ask the user for confirmation — the ratio
guard is intentionally conservative to avoid silently halving legitimate data.
