# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two independent, stdlib-only Python scripts for tracking Canadian citizenship
proof-of-citizenship processing times:

- [burndown.py](burndown.py) — interactive CLI that estimates how long a
  backlog will take to clear. The user pastes a comma-separated list of
  monthly backlog counts (newest first, oldest last); the script infers the
  monthly processing rate and prints low/likely/high time-remaining estimates.
  `--summarize` and `--fifo` skip the prompt for per-cohort estimates and a
  processing-order report (both described under Running).
- [flpt_watch.py](flpt_watch.py) — polls IRCC's published processing-times
  feed hourly, tracks the `citizen-proofs-2025/07` people-ahead figure and
  wait-time estimate, and beeps and posts a macOS notification when
  `flpt_lastupdated` changes (i.e.
  IRCC has refreshed the feed). It also appends every
  `citizen-proofs-202x/*` cohort's people-ahead count and wait-time estimate to
  `flpt_history.csv` whenever they change, which `burndown.py` reads.

There is no build step or package manifest. The scripts and
[test_burndown.py](test_burndown.py) /
[test_flpt_watch.py](test_flpt_watch.py) use the stdlib only (`unittest`) —
keep them dependency-free. Dev tooling is separate: [check.sh](check.sh) builds
a gitignored `.venv` from [requirements-dev.txt](requirements-dev.txt)
(coverage, lizard) and is the only thing that uses it.

## Running

```bash
python3 burndown.py
```

`burndown.py` is interactive: prompts for one line of input via `input("> ")`,
then prints the analysis to stdout. When `flpt_history.csv` exists it first
lists every cohort with two or more months of data; the prompt accepts a menu
number, a cohort (`2025-07` or `2025/07`), or pasted counts. Pressing return
uses `DEFAULT_COHORT` from the history, or `DEFAULT_INPUT` when there's no
history (or that cohort has under two months).

The report sizes itself to the terminal (`output_width()`, overridable with
`BURNDOWN_WIDTH`). `layout_for()` picks one of three shapes for the quantile
tables: `wide` (>= `WIDE_TABLE`, 117 cols) is the full side-by-side grid,
`compact` (>= `COMPACT_TABLE`, 80 cols) abbreviates each cell to
`fmt_months_compact` and shortens the row labels, and `stacked` (anything
narrower, floored at `MIN_WIDTH` = 40) gives each scenario its own block with
one line per quantile. No figure is dropped at any width — only its shape
changes. Prose wraps at `PROSE_WIDTH` (78) even on wider terminals.

```bash
BURNDOWN_WIDTH=50 python3 burndown.py   # check the narrow layouts
```

`python3 burndown.py --fifo [COHORT]` (default `DEFAULT_COHORT`; `2025/7`
style is normalised by `cohort_arg`) skips the prompt and asks how close to
oldest-first the processing is. Only *settled* cohorts are compared, because
of what Jade calls **the bulge**: a new cohort's own count first shows about
2,000, then swings up, sometimes 10-30x, and falls back during its first few
months before settling (2026-03 went 2,300 -> 68,500 -> 7,400; cohorts have
settled within about 3-7 months). It isn't steady late intake, since the
counts come back down, and the cause isn't known. `settled_from` finds where
a cohort settled: the start of its first `BULGE_MONTHS` (3) steady months in a
row, a month being `steady` if the count rose by no more than `BULGE_RISE`
(200, two counts rounded to 100) and fell by no more than `BULGE_DROP` (15%).
`settled_cohorts` is the run of consecutive settled cohorts from the first one
on or after `SUMMARY_FROM`, stopping at the first still bulging, so post-C-3
cohorts join as soon as their bulge has settled. Rates, order scores and sizes
read a cohort's counts only from where it settled (`settled_waiting`); an
`OrderScore` window that opens before a newer cohort settled leaves that
cohort out and names the newest it did compare (`OrderScore.newest`). It
prints:

- each settled cohort's own still-waiting count and compounded monthly
  clearance over the `OWN_WINDOWS`, after an `earlier` row for everyone
  before the first of them (`cohort_rates`);
- the trickle (`trickle_rate`, via `fifo_background`): the pooled compounded
  clearance over `RECENT_MONTHS` of the settled cohorts the sweep hasn't
  reached (`Position.work > 0`), a ratio of sums (`window_totals`) so the
  rounding to 100 mostly averages out. It replaced a median of per-cohort
  6-month rates on 2026-11-20: the front cohorts pulled that median up to
  1.8% when the cohorts behind them were clearing about 1.3-1.6%;
- an `OrderScore` per window: of the files cleared, the share that were ahead
  of COHORT versus the settled cohorts *after* it (COHORT itself is in
  neither, so a cohort at the front of the sweep doesn't count against its
  own score; with nothing newer there's no score), rescaled so 0 is random
  order and 1 strict oldest-first;
- the leftover rate (`leftover_rate`): the same pooled clearance of the
  leftovers the sweep had already passed when the window opened
  (`window_groups`: the `earlier` tail once `tail_passed`, plus any
  `passed_by` cohort), over the longest window from `LEFTOVER_MONTHS` (6)
  down to `RECENT_MONTHS` that opens after they were passed
  (`leftover_months`, kept in `Rates.leftover_months`). The tail is one
  series of about 1,500 rounded to 100, so a 3-month window swung 2.0-4.1% on
  a single rounding step (changed 2026-11-20; it's 3.6% over 5 months now).
  `passed_by` needs `PASSED_MONTHS` (2) periods in a row with nothing left to
  take, so one rounding step doesn't flip a cohort between `now` and
  `passed` (a period without its figure doesn't count against it); the
  `passed` label uses it too. The tail counts as passed once the first settled
  cohort is more than `BULGE_RISE` below its size, i.e. the sweep has moved
  on to it; before that (the early-2026 sweep) the tail is what it's
  sweeping;
- for each pace row (none, and the recent `RECENT_MONTHS` window), when the
  sweep `arrives` (`arrival_text`: months, `now`, `passed`, `never`), then
  whole months until COHORT's chance of being processed reaches each
  `ESTIMATE_ODDS` (50%, 75%, 90%, under `ESTIMATE_HEADS`) — from
  `sweep_chance`: the trickle compounding, an oldest-first sweep, and the
  leftovers it leaves clearing at the leftover rate. The 50% is about when
  the sweep finishes with the cohort. `fmt_months_short` rounds to whole
  months with an `m` (`13m`, `<1m` under one): the inputs are too rough for tenths.

There is one sweep, so there is one pace for every cohort (`sweep_pace`):
the clearance over the window of everyone it hadn't passed when the window
opened (`window_groups`, figures at both ends), less what the trickle would
have taken of them. The leftovers' clearance is theirs, at the leftover rate,
not the sweep's: until 2026-11-20 the tail's clearance was counted in the pace
*and* the leftovers were assumed to clear only at the trickle, which spent it
twice (about 43 of 213 a month). `model_rates` bundles a window's trickle,
leftover rate (never below the trickle) and pace as `Rates`.
`fifo_paces(cohorts, names, last, background)` gives the recent pace, net of
the trickle. (Until 2026-11-20 each cohort
got its own pace from its own people-ahead drop less trickle × people ahead;
for the later cohorts that was a small difference of two big numbers, and the
rows contradicted each other, 40-240 a month.)

The sweep works from COHORT's `Position` (`position`): `work`, the files it
still has to clear ahead of COHORT, then `take` of COHORT's own `waiting`.
Both come from `sweep_left`. For the cohort the sweep is in now (no work
ahead of it) that's its current count minus its `sweep_level`, floored at 0,
so leftovers already passed don't hold it up again. The level is
`STRAGGLERS` (50%) of its count the period before the cohort ahead was first
down to *its* level (`first_down_to`), i.e. of whoever was waiting when the
sweep got there; the first settled cohort, or one reached at its first
counted period, uses its size instead. Until 2026-11-20 every in-sweep cohort
used its size, which disagreed with the rule behind it (the size is before
the trickle thinned it, and a maximum of rounded counts): 2025-03's take
would have dropped from 683 to 516 the month the sweep arrived, its 50%
jumping from 5 to 9 months on no new data. Every cohort behind it (`behind=True`) gives up all but `STRAGGLERS` of
whoever is still waiting when the sweep gets there, which is (1 − share) of
its current count because the trickle thins the cohort and its take alike;
charging the trickle's past losses to the sweep's share (the old rule) pushed
leftover % to 52-64%. `arrival` thins the work ahead by the trickle too:
`ln(1 + work·L / pace) / L` with L the trickle's monthly log-rate, rather
than `work / pace` (2026-02 was 80 months the old way, about 40 now), and
`sweep_chance` takes `take / waiting` of whoever the trickle has left at
arrival, then the rest (all of a cohort it has passed) clear at `leftover`. Size (`cohort_size`) is the largest own count from `SIZE_FROM`
(2026-03) or from where the cohort's bulge settled, whichever is later
(`None` while it's bulging or with no count from then): the C-3 change in
December 2025 makes earlier counts jump, and this also keeps 2025-11's
2026-03 count of 4,200 out (its size is 2,900). Everyone before
`SUMMARY_FROM` is taken as already passed (the early-2026 sweep took that
tail from 5,800 to about 1,500). `months_to_chance` bisects the chance (it
only rises), `inf` past `ESTIMATE_CAP`.

Once the sweep has passed you, only the leftover rate is left, so the 75% and
90% times are set by it and the leftover share, not by your place in the
queue. The pre-2025 leftovers have been clearing at 3.5-5.5% a month, 2-3x
the trickle (`leftover_prose` gives their half-life), and 2025-01, just
passed, faster still. Those long times are an extrapolation the backtest
can't check: its horizons are 1-6 months, and in most of its cases the sweep
never reaches the cohort, so the sweep predicts exactly what the trickle
alone does.
`STRAGGLERS` was a hand-picked 30% until [backtest.py](backtest.py) (below)
put 20-60% within about a point of each other once the pace had settled, with
50% marginally best and matching the 50-55% that 2024-12 and 2025-01 were left
at. `sweep_left` and `position` take a `share` argument so the backtest can
try others.

`python3 burndown.py --summarize` skips the prompt and runs
`print_fifo_summary`: the column notes (`summary_items`) and model notes
(`sweep_items`) as bullet lists, then two tables, one row per settled cohort.

- The counts (`print_counts`, `COUNT_FIELDS`): people ahead, "to sweep"
  (`Position.work`), its own still waiting, "leftover" (`leftover_percent`:
  1 − take / waiting, roughly the chance a still-waiting applicant is a
  leftover — shown only for the cohort the sweep is in and those it has
  passed, since behind it it's `STRAGGLERS` by construction) and "% fifo"
  (its `OrderScore` over `RECENT_MONTHS` against the newer settled cohorts,
  `fifo_percent`, clamped to ±999%; for the newest cohorts it's mostly
  rounding).
- The estimates (`print_split`, `SPLIT_HEADS`, cells from `split_cells`):
  arrives, then `sweep_split` — when the sweep finishes with the cohort, the
  chance you've been processed by then (by the trickle or the sweep, read off
  `sweep_chance`), and when half of those it leaves have been processed at
  the leftover rate. The outcome has two humps, done by the sweep's end or a
  leftover with a long tail, so these replaced the 50%/90% columns on
  2026-11-20 (a 50%-90% range hid that; `--fifo` still shows 50/75/90%).

Each estimate is at the rates over the latest `RECENT_MONTHS` window
(`model_rates` at the latest period). Until 2026-11-24 each cell also showed
a bracketed low-high range over the last four windows, and `--fifo` a "best
3 mo" pace row; both were dropped as confusing, since the recent window has
been the most accurate. Only the recent pace is used because it
backtested closest: from 2026-05 and 2026-08 cutoffs, median paces (which
still hold the fast early-2026 months) over-predicted clearance badly.
`--summarize --fifo COHORT` just bolds COHORT's rows. `--dates` (with
`--summarize` or `--fifo`, an error alone) shows every time as the month it
comes to instead (`fmt_when`: the latest period plus the rounded months,
`2028-04` for 17m from 2026-11; `never` stays, and past year 9999 it falls
back to months). `print_report` sets the module-level `DATES_FROM` only while
the report prints, so the widget feed, which shares `split_cells`, keeps months. Both tables go through
`print_columns` (about 55 and 70 columns on today's data); narrower, the
counts become a sentence per cohort (`summary_lead_text`) and the estimates
a heading per cohort with a line per cell. In `--fifo`'s estimate rows, too
narrow a terminal puts each label and its pace on a line above the cells.
The explanations are `bullets` lists: `ARRIVES_ITEM`, `sweep_items` (with
`leftover_prose`), `unsettled_note` and `BACKTEST_NOTE` are shared with
`--fifo`, so a wording change there shows up in both. (The older random-order
`--summarize` table, with 50%/100% times and a best guess, was removed on
2026-11-20, and the 25-75% range cell with it.)

Output is coloured (headings, rules, fast/slow rows, trend, warnings) when
stdout is a TTY and `TERM` isn't `dumb`; `NO_COLOR=1` turns it off and
`FORCE_COLOR=1` forces it on through a pipe. `main()` sets the module-level
`COLOR` from `color_enabled()`, so `analyze()` called directly (as the tests do)
stays plain unless `COLOR` is patched. `paint()` is only ever applied to text
that's already been padded and wrapped — never put escapes inside a string
before it's measured, or column widths and `textwrap` will miscount. A test
pins this: stripping the escapes from coloured output must give back the plain
report byte for byte.

To exercise the analysis logic without the interactive prompt, pipe input in:

```bash
echo "10200, 10500, 11300, 11700, 13000, 14500, 15800" | python3 burndown.py
```

Run the tests with:

```bash
python3 -m unittest test_burndown test_flpt_watch   # fast pass, ~0.2s
```

The full fast-pass gate (tests under branch coverage, CRAP ≤ 12 for every
function the change touches, and jscpd for clones) is:

```bash
./check.sh            # scores uncommitted changes (vs HEAD)
./check.sh main~3     # scores everything changed since main~3
CRAP_ALL=1 ./check.sh # lists every function's score, touched or not
```

[crap.py](crap.py) maps `git diff -U0 REF` hunks onto lizard's functions (a
file REF doesn't have counts as wholly changed) and reads coverage from
`coverage.json`; [test_crap.py](test_crap.py) needs lizard, so it only runs
under `check.sh`. jscpd comes through `npx` (pinned in `check.sh`), configured
by [.jscpd.json](.jscpd.json): `maxLines` is raised because jscpd silently
skips files over 1,000 lines by default, which covers both big files here.

```bash
python3 backtest.py                 # score the models on every usable cutoff
python3 backtest.py --from 2026-07  # only cutoffs after the early-2026 pace settled
```

[backtest.py](backtest.py) (stdlib, tested by
[test_backtest.py](test_backtest.py)) cuts the history off at each period
from `SIZE_FROM`, predicts each settled cohort's own share cleared 1-6 months
on, and compares that with what the history went on to show. Models: the
sweep at each of `SHARES` (one shared `Rates` per cutoff, `Case.pace` and
`Case.leftover`), the cohort's own recent rate carried forward, random order
(the cohort's own people-ahead drop, `ahead_pace`), a gradient
(`gradient_cleared`: each group's rate an `isotonic_falling` function of the
middle of its place in the queue, fitted on the cutoff's last window and
simulated forward, so a cohort speeds up as the queue ahead drains), and two
baselines, `trickle only` and `nothing`. It then scores again only the cases
where the 50% sweep is in or reaches the cohort within the horizon
(`Case.reached`), since everywhere else the sweep predicts exactly what the
trickle alone does. From 2026-07 on: 50% sweep 4.0% mean error (3.8% with the
pre-2026-11-20 pace and leftovers, the same within noise, as the changes are
mostly beyond the horizons), gradient 4.3% with almost no bias, trickle only
5.1%; on the 15 reached cases 10.2%, gradient 11.6%, trickle only 19.4%. On
the full range every sweep model is badly over-optimistic, because the recent
pace at the early cutoffs still held the fast early-2026 months. The pace,
not the leftover share, is what moves the error. Re-run it when more months
arrive before changing `STRAGGLERS` or switching the report to the gradient.

The `FuzzPipeline` and `FuzzFifo` cases run 300 random inputs by default so
the suite stays a quick pre-commit gate. For a longer campaign (20k cases still runs in ~2s):

```bash
BURNDOWN_FUZZ_CASES=20000 python3 -m unittest test_burndown.FuzzPipeline
BURNDOWN_FUZZ_CASES=20000 python3 -m unittest test_burndown.FuzzFifo        # ~12s
FLPT_FUZZ_CASES=5000 python3 -m unittest test_flpt_watch.FuzzSnapshots   # ~6s
```

When mutation-testing this repo by patching `burndown.py` in place, disable
bytecode caching (`python3 -B`, `PYTHONDONTWRITEBYTECODE=1`) and clear
`__pycache__` between runs. Same-length edits (`== 1` -> `== 2`) leave the file
size and mtime-second unchanged, so Python silently reuses the stale `.pyc` and
every such mutant appears to survive.

The own-cohort block and the `--summarize` and `--fifo` output are pinned by
exact-text tests (added to kill text, padding and alignment mutants), so a
wording or layout change there means updating those expected strings too.

```bash
python3 flpt_watch.py          # run forever, checking hourly
python3 flpt_watch.py --once   # check once and exit (cron/testing)
```

`flpt_watch.py` fetches via `curl` (shelled out) rather than `urllib` —
Canada.ca's CDN appears to block Python's default TLS fingerprint but allows
curl's. State (last `flpt_lastupdated` seen and the latest backlog figure) is
persisted to `flpt_state.json` next to the script, which is gitignored since
it's just a local cache.

`flpt_history.csv` is the durable record (not gitignored). One row per
(feed `period`, `cohort`), both `YYYY-MM`; `period` comes from
`flpt_lastupdated`. A cohort gets a new row when its text figures or the feed
date differ from its latest row, so every published month is recorded even
when the figures are flat. Rows are append-only; the last row for a
(period, cohort) wins. An empty or missing file is seeded from `SEED_HISTORY`
(hand-recorded 2025-07 months, `observed_at` = `seed`) on the next run; to
re-seed, delete the file. Hand edits are fine — keep the header. `burndown`
normalises hand-typed months (`2026-9`, `2026/09`) and thousands separators,
and skips rows it can't read (a period or cohort that isn't a month, a count
that isn't a finite number >= 0), listing their line numbers above the menu.

The watcher runs hourly under launchd as `com.jade.flpt-watch`
(the plist is in Jade's private `ops` repo, `ops/launchd/`, symlinked into
`~/Library/LaunchAgents/`), logging to the gitignored
`flpt_watch.launchd.log`. After editing the plist:

```bash
launchctl bootout gui/$(id -u)/com.jade.flpt-watch; ~/Developer/ops/install-all.sh
```

### iPhone widget

`check_once` also calls `write_widget` (after history, before the state is saved, and like history a failure is only logged).
`burndown.write_widget` writes `widget_feed(DEFAULT_COHORT)` to `WIDGET_PATH`, which is
`citizenship.json` in Scriptable's iCloud folder, and skips the write when that folder doesn't exist.
The feed holds the cohort's `--summarize` cells for `WIDGET_FIELDS` (arrives, sweep ends,
done by then, if left half by), each a label and its value.
[scriptable/citizenship.js](scriptable/citizenship.js) draws them. To install it, copy it to
`~/Library/Mobile Documents/iCloud~dk~simonbs~Scriptable/Documents/Citizenship.js`, then add
a Scriptable widget on the iPhone and pick that script. The tests patch `burndown.WIDGET_PATH`,
so they never write to iCloud. How to change and deploy the widget:
`~/Developer/ops/docs/iphone-widgets.md`.

## Code structure

### flpt_watch.py

`check_once` fetches the feed, records history, saves state, and only then
beeps and notifies — in that order on purpose. History is diffed against the
CSV rather than the state, so a history failure is caught, logged to stderr
and retried on the next run on its own; letting it escape would skip
`save_state` and repeat the same notification every hour.

- `section` / `feed_text` — the feed is someone else's JSON, so every value
  read from it goes through these: a missing or non-object section becomes
  `{}`, and `null` becomes `""` (anything else non-string is `str()`'d). Keep
  that normalisation: `changed_rows` compares text, and a raw `None` would
  differ from the `""` read back from the CSV every run.
- `leading_number` / `feed_period` — the first number in a feed string, and
  the `YYYY-MM` of `flpt_lastupdated`. An unreadable date falls back to the
  current month with a warning on stderr.
- `snapshot_rows` → `changed_rows` → `append_history`, wrapped by
  `record_history`, which seeds an empty file via `seed_rows` first.

### burndown.py

History and prompt handling, ahead of the analysis pipeline:

- `month_key` — normalises a hand-typed month to `YYYY-MM` (or `None`).
  Periods sort as strings, so an unpadded month would otherwise reverse a
  series silently.
- `load_cohorts(path, rejected=None)` — each cohort's series, newest first,
  last row per period winning (`history_count` parses each count). Mangled
  rows' line numbers go to `rejected`.
- `missing_months`, `cohort_menu` (cohorts with two or more months),
  `describe_cohort`, `pick_cohort` (menu number or `YYYY-MM` / `YYYY/MM`) and
  `resolve_input`, which turns whatever was typed into a line of counts plus
  the cohort it came from (`None` for pasted counts).
- `own_cohort` → `OwnCohort` — a cohort's own still-waiting count per period:
  the next month's cohort's people-ahead minus this one's, for periods both
  have. `main()` passes it to `analyze(..., own=)` only when a cohort was
  picked (not for pasted counts).
- `color_enabled` / `paint` — see the colour notes above.
- `main(argv=())` takes its arguments explicitly (`__main__` passes
  `sys.argv[1:]`) so the tests, which run under unittest's own argv, can call
  it bare. `print_report` picks the report: `--summarize` (with or without
  `--fifo`) goes to `print_fifo_summary`, which prints
  `print_counts` and `print_split`; `--fifo` goes to
  `print_fifo`, which prints `print_fifo_table`, `print_order` and
  `print_estimates` in turn.

The analysis itself is a small pipeline of pure functions, which `main()`
wires to stdin/stdout:

1. `parse_numbers` — tokenizes the raw input line (commas/whitespace/pipes/tabs
   as separators) into floats.
2. `extract_backlog` — heuristically detects and strips an interleaved
   counter/index column (e.g. pasted data shaped like `1 2600 1 2700 2 7800`)
   using a ratio guard (`max(markers) * 5 <= min(values)`) so genuine
   single-column data isn't mistaken for paired data.
3. `analyze` — takes an optional `width` (defaults to `output_width()`; the
   tests pass it explicitly so the layout doesn't depend on whoever's terminal
   is running them). The core logic: reverses input to chronological order, computes
   month-over-month processed counts, derives mean/median/stdev/recent-pace
   rates, and prints fast/likely/median/recent/slow time-remaining scenarios
   plus best/likely/worst absolute bounds. Both tables are broken out across
   the `QUANTILES` columns (25/50/75/100% of the cohort ahead cleared) —
   assuming roughly random processing order, those columns are the
   corresponding percentiles of your own wait.
4. `fmt_months` — human-readable month/year formatting for a rate result, and
   `fmt_months_compact` the abbreviated form. Both are guaranteed not to exceed
   their column (`CELL` / `CELL_COMPACT`), degrading the format rather than
   breaking alignment when a degenerate input produces a negative or enormous
   month count. `quantile_header` renders the matching column headings for
   either cell width. `fmt_pct` renders a 0..1 probability. `wrap` and
   `stat_line` fit prose and label/value rows to the terminal.
5. `monthly_odds` — per-month probability you'd have been pulled, given the
   backlog at the start of each month. Returns `None` for months where the
   queue grew or stalled rather than a misleading zero.
6. `done_by` — forward-looking probability of being done within N months at a
   steady rate. Exact under random draws without replacement: once k of the
   `current` people ahead have cleared, your chance of being among them is
   k/current, so it grows linearly to 1.
7. `random_order_odds` — prints the odds section (`HORIZONS` sets the
   forward-looking rows). It's the inverse of the quantile tables: those map a
   probability to a time, this maps a time to a probability. A test pins the
   two together, so change them as a pair.
8. `print_own_cohort` — when `analyze` got an `OwnCohort`, a block after the
   look-back: `own_windows` gives the compounded monthly clearance
   (`shrink_rate`) of your own cohort beside the people-ahead rate, over the
   `OWN_WINDOWS` trailing windows plus the full span. Single months are
   deliberately not shown: IRCC rounds to the nearest 100, so one month of a
   ~2,000-person cohort is mostly rounding. With fewer than two shared months
   (e.g. the newest cohort) it prints a note and the people-ahead odds stand
   alone. The look-ahead still uses the people-ahead model.

The key modelling assumption: the input counts are *people ahead of you*, and
under uniform random processing every file in the queue has the same per-month
draw probability. So the fraction of the ahead-of-you cohort that cleared in a
month is also your own odds for that month — no need to know the size of the
queue behind you. The history shows that assumption doesn't hold: older
cohorts clear much faster than newer ones, which is why the own-cohort block
exists — it only assumes random order *within* your application month.

`print_scenarios`, `print_lookback` and `print_lookahead` own the layout.
The last two size their columns from the data (and fall back to abbreviated
headings when the spelled-out ones don't fit), so their widths vary with the
input — a fuzz case asserts no rendered line ever exceeds the target width.

When changing the parsing heuristics in `extract_backlog`, keep in mind it's a
best-effort guess with no way to ask the user for confirmation — the ratio
guard is intentionally conservative to avoid silently halving legitimate data.
