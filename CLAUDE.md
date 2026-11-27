# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two independent, stdlib-only Python scripts for tracking Canadian citizenship
proof-of-citizenship processing times:

- [burndown.py](burndown.py) — a report of when each tracked cohort is
  likely to be processed under an oldest-first sweep model, with one cohort
  in detail (described under Running).
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
python3 burndown.py [COHORT]           # the report (default DEFAULT_COHORT)
python3 burndown.py --short [COHORT]   # only its estimates table
```

`burndown.py` prints the report (`print_fifo`, described below); `--short`
prints only its estimates table (`print_short`: `print_split`, COHORT's row
bold, with no banner or notes). Until 2026-11-25 it was two reports,
`--summarize` and `--fifo [COHORT]`, and with no flags an interactive
random-order analyzer (paste monthly people-ahead counts, or pick a cohort,
for low/likely/high times to clear); all three were removed then.

The report sizes itself to the terminal (`output_width()`, overridable with
`BURNDOWN_WIDTH`, clamped to `MIN_WIDTH` 40 to `MAX_WIDTH` 117). Its tables
fall back to a narrower shape (`print_columns` returning False) rather than
drop a figure, and prose wraps at `PROSE_WIDTH` (78) even on wider terminals.

```bash
BURNDOWN_WIDTH=50 python3 burndown.py   # check the narrow layouts
```

The report's cohort (default `DEFAULT_COHORT`; `2025/7` style is normalised
by `cohort_arg`) is shown in detail, and the report asks how close to
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
  the sweep finishes with the cohort. `fmt_duration` rounds to whole
  months, as years and months (`1y 1m`, `<1m` under one): the inputs are too rough for tenths.

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
50% marginally best (it can't tell 10% from 50% either: the horizons are too
short). The share actually left, measured since the Wayback backfill
(`measured_passes`, below), disagrees: the early-2026 sweep left 2024-07 to
2024-12 at 10-25% each, 18% pooled (14-22%). The 50-55% once quoted for
2024-12 and 2025-01 predates those months being in the history. Whether the
slower sweep since (2025-01 still falling, 1,100 -> 500 over 7 months) leaves
the same isn't known yet; `STRAGGLERS` stays at 50% until it has finished
with 2025-01 to 2025-03. Lowering it also changes the pace (below 50%, the
recent pace drops from 171 to 87 a month, why not yet traced), so for
2025-07 30% moves the 50% time from 1y 8m to 3y 4m, while 65-80% barely
moves it. `sweep_left` and `position` take a `share` argument so the
backtest can try others.

The report (`print_fifo`) runs, in order: `print_every_cohort` (the column
notes, `summary_items`, then two tables, one row per settled cohort, the
report's cohort in bold), `print_target` (that cohort's 50/75/90% times with
and without the sweep, `print_estimates`), `print_order_section` (each
cohort's own clearance, `print_fifo_table`, the trickle and leftover rates,
and the cohort's `print_order` score), and last the model notes once
(`sweep_items`, `unsettled_note`, `BACKTEST_NOTE`). A cohort with no
own-cohort figure for the latest period gets a note instead of
`print_target` and the order score.

- The counts (`print_counts`, `COUNT_FIELDS`): people ahead, "to sweep"
  (`Position.work`), its own still waiting, "leftover" (`leftover_percent`:
  1 − take / waiting, roughly the chance a still-waiting applicant is a
  leftover — shown only for the cohort the sweep is in and those it has
  passed, since behind it it's `STRAGGLERS` by construction) and "% fifo"
  (its `OrderScore` over `RECENT_MONTHS` against the newer settled cohorts,
  `fifo_percent`, clamped to ±999%; for the newest cohorts it's mostly
  rounding).
- The estimates (`print_split`, `SPLIT_HEADS`, cells from `split_cells`):
  three times, each an `Odds` (the time, and `sweep_chance` by it as a
  percent, drawn as one cell by `odds_text`): when the sweep arrives, when it
  finishes with the cohort, and when half of those it leaves have been
  processed at the leftover rate (both from `sweep_split`). A time that's a
  word (`now`, `passed`, `-`, `never`) has no chance. The outcome has two
  humps, done by the sweep's end or a leftover with a long tail, so these
  replaced the 50%/90% columns on 2026-11-20 (a 50%-90% range hid that;
  the cohort in detail still shows 50/75/90%). Until 2026-11-25 there were four columns,
  the chance only at the sweep's end ("done by then") and the leftovers'
  time headed "if left, half by".

Each estimate is at the rates over the latest `RECENT_MONTHS` window
(`model_rates` at the latest period). Until 2026-11-24 each cell also showed
a bracketed low-high range over the last four windows, and `--fifo` a "best
3 mo" pace row; both were dropped as confusing, since the recent window has
been the most accurate. Only the recent pace is used because it
backtested closest: from 2026-05 and 2026-08 cutoffs, median paces (which
still hold the fast early-2026 months) over-predicted clearance badly.
Times show as the
month they come to by default (`fmt_when`: the latest period plus the
rounded months, `2028-04` for 17m from 2026-11; `never` stays, and past year
9999 it falls back to a duration). `--durations` shows how long instead, as `fmt_duration` years and
months (`1y 5m`, `11m`, `2y`; past `99y 11m` just the years). `main()`
sets the module-level `DATES_FROM` (through the `dates_from` context manager)
only while the report prints, so code called directly gets durations; the
widget feed asks for dates. Both tables go through
`print_columns` (about 55 and 52 columns on today's data); narrower, the
counts become a sentence per cohort (`summary_lead_text`) and the estimates
a heading per cohort with a line per cell. In `print_target`'s estimate rows, too
narrow a terminal puts each label and its pace on a line above the cells.
The explanations are `bullets` lists (`summary_items`, `estimate_items`,
`sweep_items` with `leftover_prose`); `ARRIVES_ITEM` went with the merge,
since `summary_items` explains the arrival. (The older random-order
`--summarize` table, with 50%/100% times and a best guess, was removed on
2026-11-20, and the 25-75% range cell with it.)

Output is coloured (headings, rules, fast/slow rows, trend, warnings) when
stdout is a TTY and `TERM` isn't `dumb`; `NO_COLOR=1` turns it off and
`FORCE_COLOR=1` forces it on through a pipe. `main()` sets the module-level
`COLOR` from `color_enabled()`, so a report called directly (as the tests do)
stays plain unless `COLOR` is patched. `paint()` is only ever applied to text
that's already been padded and wrapped — never put escapes inside a string
before it's measured, or column widths and `textwrap` will miscount. A test
pins this: stripping the escapes from coloured output must give back the plain
report byte for byte.

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

It ends with the leftover share measured (`measured_passes`, `pass_report`),
the direct check on `STRAGGLERS`: for each cohort the sweep has reached, its
own count before the pass and where it levelled off. A pass starts with a fall
over `PASS_MONTHS` (3) of more than `PASS_DROP` (25%) and 200 (`falls`; one
month at a time misses the slow sweep, and 15% caught 2025-03's trickle and
rounding). `before` is the highest count in the 6 months up to it, so a slow
sweep's first months count. It has `levelled_off` once `FLOOR_MONTHS` (3) months
in a row fall by no more than 5% of `before` or 100, monthly and in all,
searching only from where the fall was seen. Counts are only read from
`SETTLE_MONTHS` (9) after the cohort's month, past any bulge, and cohorts
under `MIN_BEFORE` (500) are left out. The range is two standard deviations
of the rounding (each own count is two figures rounded to 100, sd about 41),
and the finished passes are pooled, which narrows it. A share is final once
recorded: rerun at every earlier cutoff, none changed. Watch 2025-01 (still
falling) and 2025-02/03 as the sweep finishes with them.

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

The report and `--short` are pinned by
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
re-seed, delete the file. Periods 2025-10 to 2026-08 (every
cohort) were backfilled on 2026-11-27 from the Wayback Machine's monthly
captures of the feed (`observed_at` = `wayback:<timestamp>`), replacing the
hand-typed rows and seeds; the captures for 2026-09 to 2026-11 matched the
watcher's rows exactly, and the only hand-typed row that didn't match
(2026-01, cohort 2025-02: 7,800, December's figure, for 6,900) was a typo.
The September 2025 capture is in an older format with no cohorts. Hand edits are fine — keep the header. `burndown`
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
The feed (version 3) holds the cohort's report cells for `WIDGET_FIELDS` (arrives, sweep ends,
leftovers half), each (`widget_row`) a label, its value as the chance by then (`22%`), and its detail
as `by` the month in `short_month` form (`by 4'28` for 2028-04); a time that's a word has
that word as its value and no detail. Until 2026-11-25 it had four rows, each a date with
its duration under it.
[scriptable/citizenship.js](scriptable/citizenship.js) draws them as columns of
chance, date and label, each line in a fixed-height stack so the three
kinds of line line up across the columns. The values never shrink to fit, so
they are all one size (26pt medium, 16pt small). To install it, copy it to
`~/Library/Mobile Documents/iCloud~dk~simonbs~Scriptable/Documents/Citizenship.js`, then add
a Scriptable widget on the iPhone and pick that script. The tests patch `burndown.WIDGET_PATH`,
so they never write to iCloud. How to change and deploy the widget:
`~/Developer/ops/docs/iphone-widgets.md`.

### Shared timeline page

`python3 burndown.py --json` prints the `--short` table as data
(`estimates_feed`: each settled cohort's `SPLIT_HEADS` cells as `when` and
`chance`, plus `period`, the highlighted `cohort`, and `updated`, IRCC's own
last-updated date from the history via `feed_updated`), and (version 2)
`model` (`model_feed`): the trickle and leftover rates, the leftovers'
half-life, their windows, the sweep's share, the highlighted cohort's recent
order score and the first period, which the page's notes quote in place of
hand-written figures (`.live` spans, dotted-underlined, filled by `data-k`;
the HTML holds the last values as a fallback). It has no timestamp of
its own, so the output only changes when the figures do.
[artifact/index.html](artifact/index.html) draws it as a per-cohort timeline
(waiting, the sweep, the leftovers to their half mark), x-axis gridlines at
each January, and is published as a private claude.ai artifact
(https://claude.ai/artifact/TPdW5KacHCsxiYfxPoGm3N) with the data as its
`data.json`. [artifact/refresh.sh](artifact/refresh.sh) regenerates
`data.json.new` and prints `CHANGED` or `UNCHANGED` against the last published
`data.json` (both gitignored); after publishing, move `.new` over it. Only a
Claude session with the Artifact tool can publish: headless `claude -p` has
no Artifact tool (checked 2026-11-25), so `flpt_watch.py` can't do it itself.
Instead the desktop app's daily `citizenship-artifact` scheduled task
(9:15, `~/.claude/scheduled-tasks/citizenship-artifact/SKILL.md`) runs
`refresh.sh` and publishes only on `CHANGED`; it runs while the app is open.

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

History handling, ahead of the report:

- `month_key` — normalises a hand-typed month to `YYYY-MM` (or `None`).
  Periods sort as strings, so an unpadded month would otherwise reverse a
  series silently.
- `load_cohorts(path, rejected=None)` — each cohort's series, newest first,
  last row per period winning (`history_count` parses each count). Mangled
  rows' line numbers go to `rejected`, which `main()` reports with
  `warn_rejected`.
- `own_cohort` → `OwnCohort` — a cohort's own still-waiting count per period:
  the next month's cohort's people-ahead minus this one's, for periods both
  have. Single months are never shown on their own: IRCC rounds to the
  nearest 100, so one month of a ~2,000-person cohort is mostly rounding.
- `color_enabled` / `paint` — see the colour notes above. `fmt_pct` renders a
  0..1 rate; `wrap` and `stat_line` fit prose and label/value rows to the
  terminal.
- `main(argv=())` takes its arguments explicitly (`__main__` passes
  `sys.argv[1:]`) so the tests, which run under unittest's own argv, can call
  it bare.

