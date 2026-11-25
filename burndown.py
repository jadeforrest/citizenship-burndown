#!/usr/bin/env python3
"""
When each citizenship cohort is likely to be processed.

    python3 burndown.py [COHORT]

reads the history flpt_watch.py records (flpt_history.csv) and prints, for
every settled cohort from 2025-01, when an oldest-first sweep (on top of a
steady age-blind trickle) reaches it, when it finishes with it, and when half
of those it leaves are processed, each with your chance of having been
processed by then. COHORT (default 2025-07) is then shown in detail: its
50/75/90% times with and without the sweep, and how close to oldest-first
the processing has been. Times are the month (YYYY-MM) they come to;
--durations shows how long from the latest period instead (1y 5m), and
--short prints only the estimates table. The report fits itself to the
terminal; set BURNDOWN_WIDTH to override the detected width.
"""

import argparse
import contextlib
import csv
import json
import math
import os
import re
import shutil
import statistics
import sys
import textwrap
import tempfile
from pathlib import Path
from typing import NamedTuple


# The narrowest and widest terminal the report lays itself out for; wider
# terminals get this much and the tables don't need more.
MIN_WIDTH = 40
MAX_WIDTH = 117

# Tables use the whole terminal, but prose stops being readable long before
# that, so paragraphs wrap at the narrower of the two.
PROSE_WIDTH = 78


# Written by flpt_watch.py; DEFAULT_COHORT is the one shown in detail.
HISTORY_PATH = Path(__file__).with_name("flpt_history.csv")
DEFAULT_COHORT = "2025-07"


def month_key(text: str) -> str | None:
    """
    "2026-9", " 2026-09 " or "2026/09" as "2026-09"; None if it isn't a month.

    History periods and cohorts sort as strings, so a hand-edited "2026-9"
    left unpadded would sort after "2026-10" and silently reverse the series.
    """
    match = re.fullmatch(r"\s*(\d{4})[-/](\d{1,2})\s*", text or "")
    if not match or not 1 <= int(match.group(2)) <= 12:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def history_count(raw: str) -> float | None:
    """A people-ahead cell as a count: "9,700" -> 9700.0; None unless finite and >= 0."""
    try:
        count = float(raw.replace(",", ""))
    except ValueError:
        return None
    return count if math.isfinite(count) and count >= 0 else None


def load_cohorts(path: Path, rejected: list[int] | None = None
                 ) -> dict[str, list[tuple[str, float]]]:
    """
    Each cohort's (period, people ahead) series from the watcher's history,
    newest period first.

    A period recorded more than once (IRCC revising a month) keeps the row
    written last. Rows with a blank count (the feed had no figure) are
    skipped quietly; rows that look hand-mangled — a period or cohort that
    isn't a month, a count that isn't a finite number >= 0 — are skipped and
    their line numbers appended to `rejected`.
    """
    by_cohort: dict[str, dict[str, float]] = {}
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw = (row.get("people_ahead") or "").strip()
                if not raw:
                    continue
                period = month_key(row.get("period"))
                cohort = month_key(row.get("cohort"))
                count = history_count(raw)
                if period and cohort and count is not None:
                    by_cohort.setdefault(cohort, {})[period] = count
                elif rejected is not None:
                    rejected.append(reader.line_num)
    except FileNotFoundError:
        return {}
    return {
        cohort: sorted(periods.items(), reverse=True)
        for cohort, periods in sorted(by_cohort.items())
    }


def month_index(period: str) -> int:
    """A YYYY-MM month as a count of months, so months subtract."""
    year, month = period.split("-")
    return int(year) * 12 + int(month) - 1


def month_label(index: int) -> str:
    """The inverse of month_index."""
    return f"{index // 12}-{index % 12 + 1:02d}"


class OwnCohort(NamedTuple):
    """A cohort's own still-waiting count beside its people-ahead count."""
    cohort: str
    waiting: dict[str, float]   # period -> applicants from this month still waiting
    ahead: dict[str, float]     # period -> people ahead of them


def own_cohort(cohorts: dict[str, list[tuple[str, float]]],
               cohort: str) -> OwnCohort:
    """
    The applicants from `cohort` itself still waiting, period by period.

    The next month's cohort counts everyone who applied before it, so its
    people-ahead minus this cohort's is this cohort's own pending files. Only
    periods where both cohorts have a figure are included.
    """
    ahead = dict(cohorts.get(cohort, []))
    after = dict(cohorts.get(month_label(month_index(cohort) + 1), []))
    waiting = {p: after[p] - count for p, count in ahead.items() if p in after}
    return OwnCohort(cohort, waiting, ahead)


def shrink_rate(start: float, end: float, months: int) -> float | None:
    """
    Average share cleared per month, compounded, going from `start` to `end`
    over `months`: 2,500 -> 2,200 over 9 months is 1.4% a month. Negative
    when the count grew; None when there's nothing to measure.
    """
    if start <= 0 or end < 0 or months <= 0:
        return None
    return 1 - (end / start) ** (1 / months)


# Trailing windows (in months) for the own-cohort rates, shown alongside the
# full span of the data. Published counts are rounded to the nearest 100, so a
# single month of a cohort a couple of thousand strong is mostly rounding.
OWN_WINDOWS = (3, 6)


# ANSI styling. Off unless main() finds a terminal that wants it, so anything
# capturing the output (tests, a pipe, a file) gets plain text. Layout is always
# measured on the plain text and painted afterwards, so colour never moves a
# column.
COLOR = False
STYLES = {
    "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "cyan": "36",
}


def color_enabled(stream=None, environ=None) -> bool:
    """
    Whether to emit ANSI styling: a TTY that isn't TERM=dumb.

    NO_COLOR (no-color.org) turns it off and FORCE_COLOR turns it on regardless
    of the stream, each only when set to something non-empty; NO_COLOR wins.
    """
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):
        return False
    if env.get("FORCE_COLOR", "0") not in ("", "0"):
        return True
    if env.get("TERM") == "dumb":
        return False
    isatty = getattr(sys.stdout if stream is None else stream, "isatty", None)
    return bool(isatty and isatty())


def paint(text: str, *styles: str) -> str:
    """Wrap each line of `text` in the named STYLES, when COLOR is on."""
    if not COLOR or not styles:
        return text
    start = "\033[" + ";".join(STYLES[s] for s in styles) + "m"
    return "\n".join(f"{start}{line}\033[0m" if line else line
                     for line in text.split("\n"))


def output_width() -> int:
    """Terminal width to render into, clamped to what the report can use."""
    raw = os.environ.get("BURNDOWN_WIDTH", "").strip()
    cols = 0
    if raw:
        try:
            cols = int(raw)
        except ValueError:
            cols = 0
    else:
        cols = shutil.get_terminal_size((80, 24)).columns
    if cols <= 0:
        cols = 80
    return max(MIN_WIDTH, min(cols, MAX_WIDTH))


def wrap(text: str, width: int, indent: str = "", hanging: str | None = None) -> str:
    """Fill prose to the terminal, without splitting numbers or hyphenated words."""
    return textwrap.fill(
        text,
        width=min(width, PROSE_WIDTH),
        initial_indent=indent,
        subsequent_indent=indent if hanging is None else hanging,
        break_long_words=False,
        break_on_hyphens=False,
    )


def stat_line(label: str, value: str, width: int, indent: str = "",
              pad: int = 26, style: tuple[str, ...] = ("bold",)) -> str:
    """A 'Label:        value' row, tightened then wrapped if it won't fit."""
    head = f"{indent}{label + ':':<{pad}}"
    if len(head + value) <= width:
        return head + paint(value, *style)
    head = f"{indent}{label}: "
    if len(head + value.strip()) <= width:
        return head + paint(value.strip(), *style)
    return wrap(f"{label}: {value.strip()}", width, indent=indent,
                hanging=indent + "  ")


def fmt_pct(fraction: float) -> str:
    """Render a 0..1 probability as a percentage."""
    return f"{fraction * 100:.1f}%"


def rate_cell(rate: float | None) -> tuple[str, tuple[str, ...]]:
    """Text and tone for a shrink_rate() result."""
    if rate is None:
        return "n/a", ()
    if rate < 0:
        return "grew", ("yellow",)
    return fmt_pct(rate), ()


def processed_per_month(entered: list[float]) -> list[float]:
    """People cleared each month, oldest first, from counts given newest first."""
    chrono = list(reversed(entered))
    return [chrono[i] - chrono[i + 1] for i in range(len(chrono) - 1)]


# The "recent" pace averages this many of the latest months (fewer if that's
# all the data there is).
RECENT_MONTHS = 3


def recent_pace(processed: list[float]) -> tuple[float, int]:
    """Average throughput over the latest RECENT_MONTHS months, and how many."""
    n = min(RECENT_MONTHS, len(processed))
    return statistics.mean(processed[-n:]), n


# --summarize and --fifo cover every tracked cohort from this application month on.
SUMMARY_FROM = "2025-01"

# Widest a summary count or month cell gets; fmt_count / fmt_duration
# degrade to e-notation rather than grow past it.
SUMMARY_CELL = 7


def fmt_count(value: float) -> str:
    """A count with thousands separators, in at most SUMMARY_CELL characters."""
    text = f"{value:,.0f}"
    return text if len(text) <= SUMMARY_CELL else f"{value:.0e}"


def fmt_duration(months: float) -> str:
    """
    Whole months as years and months, in at most SUMMARY_CELL characters:
    "5m", "1y", "1y 5m", "<1m" under a month, "never" for inf. The inputs
    are too rough for tenths of a month to mean anything. Past "99y 11m"
    the months are dropped, and past what fits the years go to e-notation.
    """
    if months == float("inf"):
        return "never"
    if months < 1:
        return "<1m"
    years, rest = divmod(round(months), 12)
    if not years:
        return f"{rest}m"
    text = f"{years:,}y" + (f" {rest}m" if rest else "")
    if len(text) <= SUMMARY_CELL:
        return text
    text = f"{years:,}y"
    return text if len(text) <= SUMMARY_CELL else f"{years:.0e}y"


# The period --summarize and --fifo count their times from, so fmt_when()
# shows the month a time comes to; None (--durations, and code called
# directly) shows how long from now.
DATES_FROM: str | None = None


@contextlib.contextmanager
def dates_from(period: str | None):
    """Set DATES_FROM to `period` for the block, then put back what it was."""
    global DATES_FROM
    saved, DATES_FROM = DATES_FROM, period
    try:
        yield
    finally:
        DATES_FROM = saved


def fmt_when(months: float) -> str:
    """
    fmt_duration(), or with DATES_FROM set the YYYY-MM month that many
    (rounded) months after it: "2028-04". Past what fits in SUMMARY_CELL
    (the year 10000) it falls back to a duration, and inf is "never" either way.
    """
    if DATES_FROM is None or not math.isfinite(months):
        return fmt_duration(months)
    text = month_label(month_index(DATES_FROM) + round(max(months, 0.0)))
    return text if len(text) <= SUMMARY_CELL else fmt_duration(months)


def times_note(last: str) -> str:
    """How the estimate times read: a month, or (--durations) how long from `last`."""
    if DATES_FROM is None:
        return f"All times are how long from {last}, in years and months"
    return (f"All times are shown as the month (YYYY-MM) they come to, counted "
            f"from {last}")


def print_banner(title: str, width: int) -> None:
    """A report's title between two rules."""
    banner = paint("=" * min(width, 60), "cyan")
    print("\n" + banner)
    print(paint(title, "bold", "cyan"))
    print(banner)


# The bulge: a new cohort's own count first shows about 2,000, then swings up,
# sometimes 10-30x, and falls back before settling, within about 3-7 months so
# far (2026-03 went 2,300 -> 68,500 -> 7,400). --fifo only reads a cohort once
# its bulge has settled: BULGE_MONTHS steady months in a row, where a month is
# steady if the count rose by no more than BULGE_RISE (two counts rounded to
# the nearest 100 can differ by 200 on rounding alone) and fell by no more than
# BULGE_DROP (the sweep's fastest months took 2025-01 down 40%, but by then it
# had settled).
BULGE_MONTHS = 3
BULGE_RISE = 200
BULGE_DROP = 0.15

# Share of a cohort an oldest-first sweep leaves behind for later. When the
# early-2026 sweep moved on, 2024-12 and 2025-01 were at 50-55% of their
# January counts. backtest.py scores 20-60% within about a point of each other
# once the pace has settled, with 50% marginally best; re-run it as the
# history grows.
STRAGGLERS = 0.5

# A cohort's size, for how much of it the sweep will take, is its largest own
# still-waiting count from this period on. IRCC moved to the C-3 process in
# December 2025 with a surge of applications, and the counts jump around until
# it settled (2025-11 reads 12,600-14,100 in 2026-01/02, about 2,500 after).
SIZE_FROM = "2026-03"

# The chances --fifo times: months until you're this likely to have been
# processed, after the months until the sweep arrives. 50% is about when the
# sweep finishes with your cohort.
ESTIMATE_ODDS = (0.50, 0.75, 0.90)

# The leftovers' rate is measured over the longest window of up to this many
# months (and at least RECENT_MONTHS) that opens after the sweep had passed
# them. The tail is one series of about 1,500 rounded to 100, so over 3 months
# a single rounding step swung it 2.0-4.1%.
LEFTOVER_MONTHS = 6

# A cohort counts as passed once the sweep has had nothing left to take from
# it for this many periods in a row, so one rounding step in its own count
# doesn't flip it between "now" and "passed".
PASSED_MONTHS = 2

# Furthest out months_to_chance() looks before calling it "never".
ESTIMATE_CAP = 1200.0


def latest_period(cohorts: dict[str, list[tuple[str, float]]]) -> str:
    """The newest period any cohort has a figure for."""
    return max(series[0][0] for series in cohorts.values() if series)


def steady(before: float, after: float) -> bool:
    """One month's change in an own count that's within the bulge bounds."""
    return (after - before <= BULGE_RISE
            and after >= before * (1 - BULGE_DROP))


def settled_from(waiting: dict[str, float]) -> str | None:
    """
    The first period of a cohort's first run of BULGE_MONTHS steady months
    in a row (consecutive periods, all with figures): where its bulge had
    settled. None if it hasn't yet. Once settled, a cohort stays settled.
    """
    for start in sorted(waiting):
        months = [month_label(month_index(start) + k)
                  for k in range(BULGE_MONTHS + 1)]
        if all(m in waiting for m in months) and all(
                steady(waiting[a], waiting[b]) for a, b in zip(months, months[1:])):
            return start
    return None


def settled_waiting(waiting: dict[str, float]) -> dict[str, float]:
    """A cohort's own counts from when its bulge settled; {} while it's bulging."""
    start = settled_from(waiting)
    if start is None:
        return {}
    return {p: n for p, n in waiting.items() if p >= start}


def settled_cohorts(cohorts: dict[str, list[tuple[str, float]]],
                    start: str = SUMMARY_FROM) -> list[str]:
    """
    The run of consecutive cohorts from the first one on or after `start`
    whose bulge has settled (settled_from), stopping at the first that
    hasn't: the ones --fifo reads.
    """
    present = [c for c, series in cohorts.items() if c >= start and series]
    if not present:
        return []
    names = []
    index = month_index(min(present))
    while settled_from(own_cohort(cohorts, month_label(index)).waiting):
        names.append(month_label(index))
        index += 1
    return names


def window_rate(series: dict[str, float], last: str, months: int) -> float | None:
    """shrink_rate() of `series` over the `months` ending at `last`, if both ends exist."""
    first = month_label(month_index(last) - months)
    if first in series and last in series:
        return shrink_rate(series[first], series[last], months)
    return None


class CohortRates(NamedTuple):
    """One --fifo table row: a cohort (or the tail before them) and its rates."""
    label: str
    waiting: float | None              # still waiting in the latest period
    rates: tuple[float | None, ...]    # window_rate() for each OWN_WINDOWS


def cohort_rates(cohorts: dict[str, list[tuple[str, float]]],
                 names: list[str], last: str) -> list[CohortRates]:
    """
    The table rows: everyone before the first of `names` ("earlier", read
    straight from its people-ahead count), then each cohort's own count.
    """
    tail = dict(cohorts[names[0]])
    rows = [CohortRates("earlier", tail.get(last),
                        tuple(window_rate(tail, last, n) for n in OWN_WINDOWS))]
    for name in names:
        waiting = settled_waiting(own_cohort(cohorts, name).waiting)
        rows.append(CohortRates(name, waiting.get(last),
                                tuple(window_rate(waiting, last, n)
                                      for n in OWN_WINDOWS)))
    return rows


def window_totals(series: list[dict[str, float]], last: str,
                  months: int) -> tuple[float, float] | None:
    """
    The summed counts at the start and end of the `months` ending at `last`,
    over the series with figures at both ends; None if none has.
    """
    first = month_label(month_index(last) - months)
    kept = [s for s in series if first in s and last in s]
    if not kept:
        return None
    return sum(s[first] for s in kept), sum(s[last] for s in kept)


def trickle_rate(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                 last: str, months: int = RECENT_MONTHS) -> float | None:
    """
    The background trickle: the compounded monthly clearance, over the
    `months` ending at `last`, of the settled cohorts the sweep hasn't
    reached yet (position().work > 0), pooled as a ratio of sums so the
    rounding to 100 in each count mostly averages out. The cohorts at the
    front are left out: they clear at the sweep's speed, not the trickle's.
    """
    behind = [settled_waiting(own_cohort(cohorts, name).waiting) for name in names
              if (pos := position(cohorts, name, last)) and pos.work > 0]
    totals = window_totals(behind, last, months)
    return None if totals is None else shrink_rate(*totals, months)


def passed_by(cohorts: dict[str, list[tuple[str, float]]], name: str,
              period: str) -> bool:
    """
    Whether the sweep had finished with `name` by `period`: only leftovers
    left in it, there and in the PASSED_MONTHS - 1 periods before (a period
    without its figure doesn't count against it).
    """
    for k in range(PASSED_MONTHS):
        pos = position(cohorts, name, month_label(month_index(period) - k))
        if pos is None and k > 0:
            continue
        if pos is None or pos.work > 0 or pos.take > 0:
            return False
    return True


def tail_passed(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                period: str) -> bool:
    """
    Whether the sweep had left the tail before `names[0]` by `period`: it had
    started on `names[0]`, whose own count was more than rounding
    (BULGE_RISE) below its size. Until then the tail is what it's sweeping
    (the early-2026 sweep took it from 5,800 to about 1,500).
    """
    own = own_cohort(cohorts, names[0]).waiting
    size = cohort_size(own)
    return size is not None and period in own and size - own[period] >= BULGE_RISE


def window_groups(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                  first: str) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """
    The series a window opening at `first` splits into: the leftovers the
    sweep had passed by then (the tail once tail_passed, and any passed_by
    cohort), and everyone else (the tail until then, and every other
    settled cohort's own counts).
    """
    tail = dict(cohorts[names[0]])
    left, rest = ([tail], []) if tail_passed(cohorts, names, first) else ([], [tail])
    for name in names:
        group = left if passed_by(cohorts, name, first) else rest
        group.append(settled_waiting(own_cohort(cohorts, name).waiting))
    return left, rest


def leftover_months(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                    last: str) -> int | None:
    """
    The window leftover_rate() measures over at `last`: the longest from
    LEFTOVER_MONTHS down to RECENT_MONTHS that opens with leftovers the
    sweep had already passed, with figures at both ends. None if none does.
    """
    for months in range(LEFTOVER_MONTHS, RECENT_MONTHS - 1, -1):
        first = month_label(month_index(last) - months)
        if window_totals(window_groups(cohorts, names, first)[0], last, months):
            return months
    return None


def leftover_rate(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                  last: str, months: int | None = None) -> float | None:
    """
    How fast the leftovers clear: the compounded monthly clearance, over the
    `months` ending at `last` (default leftover_months()), of the leftovers
    the sweep had passed when the window opened (window_groups), pooled as a
    ratio of sums like trickle_rate(). None if it hadn't passed anyone
    measurable then.
    """
    months = leftover_months(cohorts, names, last) if months is None else months
    if months is None:
        return None
    first = month_label(month_index(last) - months)
    totals = window_totals(window_groups(cohorts, names, first)[0], last, months)
    return None if totals is None else shrink_rate(*totals, months)


def sweep_pace(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
               last: str, background: float,
               months: int = RECENT_MONTHS) -> float | None:
    """
    The sweep's files a month over the `months` ending at `last`: the
    clearance of everyone it hadn't passed when the window opened
    (window_groups, with figures at both ends), less what a `background`
    trickle would have taken of them. The leftovers it had passed clear at
    their own leftover_rate(), so their clearance isn't the sweep's:
    counting it here as well would spend it twice. One pace for every
    cohort, since there is one sweep. None without figures.
    """
    first = month_label(month_index(last) - months)
    totals = window_totals(window_groups(cohorts, names, first)[1], last, months)
    if totals is None:
        return None
    start, end = totals
    trickle = start * (1 - (1 - min(max(background, 0.0), 1.0)) ** months)
    return (start - end - trickle) / months


class OrderScore(NamedTuple):
    """Who cleared over a window: the people ahead of a cohort, or it and newer."""
    months: int
    ahead_start: float
    ahead_cleared: float
    newer_start: float
    newer_cleared: float
    newest: str = ""    # the newest cohort compared, if not just the target

    def observed(self) -> float | None:
        """Share of the clearance that came from the people ahead."""
        total = self.ahead_cleared + self.newer_cleared
        return self.ahead_cleared / total if total > 0 else None

    def random(self) -> float:
        """That share if every file were equally likely: their share of the files."""
        return self.ahead_start / (self.ahead_start + self.newer_start)

    def score(self) -> float | None:
        """0 for random order, 1 for strict oldest-first (all of it from ahead)."""
        seen = self.observed()
        if seen is None or self.random() >= 1:
            return None
        return (seen - self.random()) / (1 - self.random())


def compared(cohorts: dict[str, list[tuple[str, float]]], newer: list[str],
             first: str) -> list[tuple[str, dict[str, float]]]:
    """
    (cohort, settled own counts) for each of `newer` that had settled by
    `first`.
    """
    settled = [settled_waiting(own_cohort(cohorts, c).waiting) for c in newer]
    return [(c, w) for c, w in zip(newer, settled) if first in w]


def order_score(cohorts: dict[str, list[tuple[str, float]]], target: str,
                newer: list[str], last: str, months: int) -> OrderScore | None:
    """
    Clearance over the `months` ending at `last` among the people ahead of
    `target` and among `newer` (the settled cohorts after it; the target
    itself is in neither, so a cohort at the front of a sweep doesn't count
    against its own score). Each cohort's own counts are read from when its
    bulge settled (settled_waiting), so a newer cohort that was still
    bulging when the window opened is left out. None unless the people ahead
    and every cohort compared have figures at both ends, and at least one
    newer cohort is compared.
    """
    first = month_label(month_index(last) - months)
    kept = compared(cohorts, newer, first)
    series = [dict(cohorts.get(target, []))] + [w for _, w in kept]
    if not kept or any(first not in s or last not in s for s in series):
        return None
    ahead, *own = series
    newer_start = sum(s[first] for s in own)
    if ahead[first] + newer_start <= 0:
        return None
    return OrderScore(months, ahead[first], ahead[first] - ahead[last],
                      newer_start, newer_start - sum(s[last] for s in own),
                      kept[-1][0])


def describe_score(score: float | None) -> str:
    """Plain words for an OrderScore.score()."""
    if score is None:
        return "nothing cleared"
    if score >= 0.75:
        return "close to oldest-first"
    if score >= 0.25:
        return "between random and oldest-first"
    if score > -0.25:
        return "close to random"
    return "newer files first"


class Position(NamedTuple):
    """Where a cohort stands for the sweep."""
    work: float      # files the sweep still has to clear ahead of it
    waiting: float   # its own applicants still waiting
    take: float      # how many of those the sweep will clear before moving on


def cohort_size(waiting: dict[str, float]) -> float | None:
    """
    A cohort's size: its largest own still-waiting count from SIZE_FROM, or
    from when its bulge settled if that's later. None while it's bulging, or
    with no count from then on.
    """
    settled = settled_from(waiting)
    if settled is None:
        return None
    return max((n for period, n in waiting.items()
                if period >= max(SIZE_FROM, settled)), default=None)


def sweep_left(waiting: dict[str, float], last: str,
               share: float | None = None, behind: bool = False,
               level: float | None = None) -> float:
    """
    How much of a cohort the sweep has still to clear in `last`. If it's
    working through the cohort now: what's waiting above `level`, the
    leftovers it leaves behind (sweep_level(); `share` of its size without
    one), 0 once it's down to that. If it's `behind` (unswept work ahead of
    it): all but `share` (default STRAGGLERS) of whoever is still waiting
    when the sweep gets there, and since the trickle thins the cohort and
    its take alike, that's (1 - share) of what's waiting now. 0 without the
    figures to tell.
    """
    size = cohort_size(waiting)
    if size is None or last not in waiting:
        return 0.0
    share = STRAGGLERS if share is None else share
    if behind:
        return max((1 - share) * waiting[last], 0.0)
    return max(waiting[last] - (share * size if level is None else level), 0.0)


def counted_from(waiting: dict[str, float]) -> str | None:
    """The first period cohort_size() reads: SIZE_FROM or settled_from(), if later."""
    settled = settled_from(waiting)
    return None if settled is None else max(SIZE_FROM, settled)


def first_down_to(waiting: dict[str, float], level: float | None,
                  last: str) -> str | None:
    """The first counted period up to `last` with the count at or below `level`."""
    start = counted_from(waiting)
    if start is None or level is None:
        return None
    return next((p for p in sorted(waiting)
                 if start <= p <= last and waiting[p] <= level), None)


def sweep_level(waiting: dict[str, float], ahead: dict[str, float] | None,
                ahead_level: float | None, last: str, share: float) -> float | None:
    """
    The count the sweep leaves a cohort at: `share` of whoever was waiting
    when it got there, the same rule sweep_left() applies to a cohort behind
    it. It got there once the cohort `ahead` was first down to its own
    `ahead_level` by `last`, so this is `share` of the cohort's count the
    period before. With no cohort ahead (the first), or no count then, it's
    `share` of its size. None if the sweep hasn't left the cohort ahead.
    """
    if ahead is not None:
        reached = first_down_to(ahead, ahead_level, last)
        if reached is None:
            return None
        before = month_label(month_index(reached) - 1)
        start = counted_from(waiting)
        if start and before >= start and before in waiting:
            return share * waiting[before]
    size = cohort_size(waiting)
    return None if size is None else share * size


def position(cohorts: dict[str, list[tuple[str, float]]], target: str,
             last: str, share: float | None = None) -> Position | None:
    """
    `target`'s Position in `last`, None without its own count then. The work
    ahead is sweep_left() of each cohort from SUMMARY_FROM up to `target`,
    each one `behind` once there's work ahead of it: everyone before
    SUMMARY_FROM is a straggler the early-2026 sweep passed, and so is
    `target` itself if it's before SUMMARY_FROM.
    """
    own = own_cohort(cohorts, target).waiting
    if last not in own:
        return None
    first = max(month_index(SUMMARY_FROM), month_index(min(cohorts)))
    portion = STRAGGLERS if share is None else share
    work, take = 0.0, 0.0
    ahead, level = None, None
    for i in range(first, month_index(target) + 1):
        waiting = own if i == month_index(target) else own_cohort(
            cohorts, month_label(i)).waiting
        level = sweep_level(waiting, ahead, level, last, portion)
        left = sweep_left(waiting, last, share, work > 0, level)
        if i == month_index(target):
            take = left
        else:
            work += left
        ahead = waiting
    return Position(work, own[last], take)


def arrival(background: float, pos: Position, pace: float) -> float:
    """
    Months until the sweep reaches the cohort: 0 with nothing ahead, inf
    with no sweep. The trickle keeps thinning the work ahead while the sweep
    clears `pace` a month, so with a monthly log-rate L the work runs out at
    ln(1 + work * L / pace) / L rather than work / pace.
    """
    work = max(pos.work, 0.0)
    if work <= 0 or background >= 1:
        return 0.0
    if pace <= 0:
        return float("inf")
    rate = -math.log1p(-max(background, 0.0))
    return math.log1p(work * rate / pace) / rate if rate > 0 else work / pace


def unit(rate: float) -> float:
    """A monthly rate held to 0..1."""
    return min(max(rate, 0.0), 1.0)


def sweep_chance(months: float, background: float, pos: Position,
                 pace: float, leftover: float | None = None) -> float:
    """
    Your chance of having been processed `months` from now, under a sweep plus
    background model. Every month a share `background` of every cohort clears
    whatever its age. Meanwhile an oldest-first sweep clears `pace` files a
    month: first the `pos.work` ahead of your cohort (arrival()), then
    `pos.take` / `pos.waiting` of whoever in your cohort the trickle has left
    by then, and moves on. Whoever it leaves (all of a cohort it has passed)
    clears at `leftover` a month from then on (the background if None, and
    never slower). Files within a month are taken in random order.
    """
    base = unit(background)
    rest = max(unit(base if leftover is None else leftover), base)
    cap = unit(max(pos.take, 0.0) / pos.waiting) if pos.waiting > 0 else 0.0
    if pos.waiting > 0 and cap <= 0 and pos.work <= 0:
        return 1 - (1 - rest) ** months
    arrive = arrival(base, pos, pace) if pace > 0 and pos.waiting > 0 else months
    there = pos.waiting * (1 - base) ** arrive
    if months <= arrive or there <= 0:
        return 1 - (1 - base) ** months
    done = arrive + cap * there / pace
    if months <= done:
        return 1 - (1 - base) ** months * (1 - (months - arrive) * pace / there)
    return 1 - (1 - base) ** done * (1 - cap) * (1 - rest) ** (months - done)


def months_to_chance(chance: float, background: float, pos: Position,
                     pace: float, leftover: float | None = None) -> float:
    """Months until sweep_chance() reaches `chance`; inf past ESTIMATE_CAP."""
    def reached(m: float) -> bool:
        return sweep_chance(m, background, pos, pace, leftover) >= chance
    if not reached(ESTIMATE_CAP):
        return float("inf")
    lo, hi = 0.0, ESTIMATE_CAP
    for _ in range(60):     # sweep_chance only rises with time, so bisect
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if reached(mid) else (mid, hi)
    return hi


def fifo_paces(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
               last: str, background: float) -> list[tuple[str, float | None]]:
    """
    The --fifo estimate rows: (label, sweep pace); None for no sweep at all,
    the only row when there's no RECENT_MONTHS window to measure a pace in.
    The sweep row is sweep_pace() over the RECENT_MONTHS ending at `last`.
    """
    recent = sweep_pace(cohorts, names, last, background)
    if recent is None:
        return [("no sweep", None)]
    return [("no sweep", None), (f"sweep, recent {RECENT_MONTHS} mo", recent)]


def print_fifo_table(rows: list[CohortRates], target: str, width: int) -> None:
    """Each settled cohort's own monthly clearance over the OWN_WINDOWS."""
    heads = [f"last {n} mo" for n in OWN_WINDOWS]
    if 2 + 7 + 2 + 7 + sum(2 + len(h) for h in heads) > width:
        heads = [f"{n} mo" for n in OWN_WINDOWS]
    print(paint(f"  {'cohort':<7}  {'waiting':>7}"
                + "".join(f"  {h}" for h in heads), "dim"))
    for row in rows:
        emph = ("bold",) if row.label == target else ()
        waiting = "n/a" if row.waiting is None else fmt_count(row.waiting)
        cells = ""
        for head, rate in zip(heads, row.rates):
            text, tone = rate_cell(rate)
            cells += "  " + paint(f"{text:>{len(head)}}", *emph, *tone)
        print(paint(f"  {row.label:<7}  {waiting:>7}", *emph) + cells)


def print_order(scores: list[OrderScore], target: str, newer: list[str],
                width: int) -> None:
    """The oldest-first score for each window, with the shares behind it."""
    if not newer:
        print(wrap(f"No settled cohort is newer than {target}, so there's "
                   "nothing behind it to compare with.",
                   width, indent="  ", hanging="  "))
        return
    span = newer[0] if len(newer) == 1 else f"{newer[0]} to {newer[-1]}"
    print(wrap(f"Of the files cleared, the share that were ahead of {target}, "
               f"against those behind it from {span}. Strict oldest-first "
               "would make it 100%; random order would match their share of "
               "the files.", width, indent="  ", hanging="  "))
    for s in scores:
        seen, score = s.observed(), s.score()
        seen_t = "n/a" if seen is None else f"{seen * 100:.0f}%"
        score_t = "n/a" if score is None else f"{score:.2f}"
        line_span = (newer[0] if s.newest in ("", newer[0])
                     else f"{newer[0]} to {s.newest}")
        print(wrap(f"Last {s.months} months: {fmt_count(s.ahead_cleared)} "
                   f"ahead and {fmt_count(s.newer_cleared)} from {line_span} "
                   f"cleared, {seen_t} ahead against {s.random() * 100:.0f}% "
                   f"at random. Score {score_t} (0 random, 1 oldest-first): "
                   f"{describe_score(score)}.",
                   width, indent="    ", hanging="      "))


def odds_heads(odds: tuple[float, ...]) -> tuple[str, ...]:
    """Estimate column headings: arrives, then each chance."""
    return ("arrives",) + tuple(f"{q:.0%}" for q in odds)


ESTIMATE_HEADS = odds_heads(ESTIMATE_ODDS)


def estimate_cells(background: float, pos: Position, pace: float | None,
                   leftover: float | None = None,
                   passed: bool | None = None) -> list[str]:
    """
    One estimate row as text: when the sweep arrives (arrival_text), then
    whole months to each of ESTIMATE_ODDS.
    """
    return [arrival_text(background, pos, pace, passed)] + [
        fmt_when(months_to_chance(q, background, pos, pace or 0.0,
                                          leftover))
        for q in ESTIMATE_ODDS]


def fmt_share(fraction: float) -> str:
    """A 0..1 chance in whole percent: "61%"."""
    return f"{100 * fraction:.0f}%"


class Split(NamedTuple):
    """A cohort's two outcomes: the sweep (or trickle) gets you, or you're left."""
    ends: float     # months until the sweep finishes with the cohort (0 if it has)
    done: float     # chance of having been processed by then
    half: float     # months until half of those it leaves have been processed


def sweep_split(background: float, pos: Position, pace: float,
                leftover: float | None = None) -> Split:
    """
    sweep_chance() split at the sweep's end: when that is, how likely you
    are to have been processed by then (by the trickle or the sweep), and
    when half of whoever it leaves are processed, at `leftover` (the
    background if None, never slower). A cohort the sweep has finished with
    ends now, with no chance yet; without a sweep it never ends.
    """
    rest = max(unit(background if leftover is None else leftover), unit(background))
    if pos.work <= 0 and pos.take <= 0:
        ends = 0.0
    elif pace <= 0 or pos.waiting <= 0:
        return Split(float("inf"), 0.0, float("inf"))
    else:
        arrive = arrival(unit(background), pos, pace)
        there = pos.waiting * (1 - unit(background)) ** arrive
        cap = unit(max(pos.take, 0.0) / pos.waiting)
        ends = arrive + cap * there / pace
    done = sweep_chance(ends, background, pos, pace, leftover)
    if rest >= 1:
        return Split(ends, done, ends)
    return Split(ends, done, ends + (math.log(0.5) / math.log1p(-rest)
                                     if rest > 0 else float("inf")))


SPLIT_HEADS = ("arrives", "sweep ends", "leftovers half")


class Odds(NamedTuple):
    """A SPLIT_HEADS time, and your chance of having been processed by it."""
    when: str       # fmt_when(), or a word: now, passed, -, never
    chance: str     # fmt_share(), "" where there's no time to read it at


def split_cells(rates: Rates, pos: Position,
                passed: bool | None = None) -> list[Odds]:
    """
    One cohort's SPLIT_HEADS at `rates`, each with sweep_chance() by then:
    when the sweep arrives, when it finishes with the cohort (sweep_split()),
    and when half of whoever it leaves have been processed. Where the
    arrival is a word (now, passed, -, never) it has no chance; a cohort the
    sweep has passed keeps only its leftovers' time.
    """
    word = arrival_text(rates.background, pos, rates.pace, passed)
    if word in ("-", "never"):
        return [Odds(word, ""), Odds(word, ""), Odds("-", "")]
    pace = rates.pace or 0.0
    split = sweep_split(rates.background, pos, pace, rates.leftover)
    def odds(months: float, chance: float | None = None) -> Odds:
        if not math.isfinite(months):
            return Odds(fmt_when(months), "")
        if chance is None:
            chance = sweep_chance(months, rates.background, pos, pace, rates.leftover)
        return Odds(fmt_when(months), fmt_share(chance))
    half = odds(split.half)
    if word == "passed":
        return [Odds(word, ""), Odds("-", ""), half]
    arrive = Odds(word, "") if word == "now" else odds(arrival_months(rates, pos))
    return [arrive, odds(split.ends, split.done), half]


def odds_text(odds: Odds) -> str:
    """An Odds as one table cell: "2028-04  22%", or the time alone."""
    return f"{odds.when}  {odds.chance:>3}" if odds.chance else odds.when


def arrival_months(rates: Rates, pos: Position) -> float:
    """arrival() at `rates`' pace; inf with no sweep."""
    if rates.pace is None or rates.pace <= 0:
        return float("inf")
    return arrival(rates.background, pos, rates.pace)


def arrival_text(background: float, pos: Position, pace: float | None,
                 passed: bool | None = None) -> str:
    """
    Months until the sweep reaches the cohort: "-" for no sweep, "now" if
    it's working through the cohort, "passed" if it has finished with it
    (`passed`, from passed_by(); without it, nothing left to take now),
    "never" if it isn't moving.
    """
    if pace is None:
        return "-"
    if (pos.work <= 0 and pos.take <= 0) if passed is None else passed:
        return "passed"
    if pace <= 0:
        return "never"
    if pos.work <= 0:
        return "now"
    return fmt_when(arrival(background, pos, pace))


def pace_text(pace: float | None) -> str:
    """A sweep pace for an estimate row: "-" for no sweep, never below 0."""
    return "-" if pace is None else fmt_count(max(pace, 0))


def estimate_widths(rows: list[list[str]], heads: tuple[str, ...]) -> list[int]:
    """Each estimate column as wide as its heading or its widest cell."""
    return [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]


def estimate_group(cells: list[str], widths: list[int]) -> str:
    """Estimate cells right-aligned to `widths`, any that never clear in red."""
    return "  ".join(paint(f"{t:>{w}}", *(("red",) if "never" in t else ()))
                     for t, w in zip(cells, widths))


def print_estimates(paces: list[tuple[str, float | None]], background: float,
                    pos: Position, width: int,
                    leftover: float | None = None,
                    passed: bool | None = None) -> None:
    """
    Months until your chance reaches ESTIMATE_ODDS, one row per pace. When
    a row doesn't fit on one line, its label and pace go on a line above.
    """
    rows = [(label, pace_text(pace),
             estimate_cells(background, pos, pace, leftover, passed))
            for label, pace in paces]
    label_w = max(len(label) for label, _, _ in rows)
    widths = estimate_widths([cells for *_, cells in rows], ESTIMATE_HEADS)
    heads = "".join(f"  {h:>{w}}" for h, w in zip(ESTIMATE_HEADS, widths))
    if 2 + label_w + 2 + SUMMARY_CELL + len(heads) > width:
        print(paint(f"  {heads}", "dim"))
        for label, pace_t, cells in rows:
            print(f"  {label}" + ("" if pace_t == "-" else f", {pace_t} a month")
                  + f"\n    {estimate_group(cells, widths)}")
        return
    print(paint(f"  {'':{label_w}}  {'pace/mo':>{SUMMARY_CELL}}{heads}", "dim"))
    for label, pace_t, cells in rows:
        print(f"  {label:<{label_w}}  {pace_t:>{SUMMARY_CELL}}  "
              f"{estimate_group(cells, widths)}")


def fifo_start(cohorts: dict[str, list[tuple[str, float]]], title: str,
               width: int) -> list[str]:
    """The banner, then the settled cohorts, or a note and [] if there are none."""
    print_banner(title, width)
    return settled_or_note(cohorts, width)


def settled_or_note(cohorts: dict[str, list[tuple[str, float]]],
                    width: int) -> list[str]:
    """The settled cohorts, or a note and [] if there are none."""
    names = settled_cohorts(cohorts)
    if not names:
        print(wrap(f"No cohort from {SUMMARY_FROM} on in {HISTORY_PATH.name} "
                   "has settled after its bulge yet.", width))
    return names


def unsettled_note(names: list[str]) -> str:
    """Why the cohorts after the last settled one are left out."""
    return (f"Cohorts newer than {names[-1]} are left out because of the "
            "bulge: a new cohort's count swings up, sometimes many times over, "
            "then falls back over its first few months. A cohort is used once "
            f"it has held steady for {BULGE_MONTHS} months (no rise beyond "
            f"rounding, no fall over {BULGE_DROP:.0%}).")


def fifo_background(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                    last: str) -> tuple[float | None, float]:
    """
    The trickle_rate() at `last`, and the background the model uses from it:
    a growing set of cohorts clears nothing.
    """
    trickle = trickle_rate(cohorts, names, last)
    return trickle, 0.0 if trickle is None else max(trickle, 0.0)


class Rates(NamedTuple):
    """The model's inputs, measured over one window."""
    background: float      # the trickle, never below 0
    leftover: float        # how fast the leftovers clear, never below the trickle
    pace: float | None     # the sweep's files a month; None with no window
    leftover_months: int | None = None   # leftover's window; None: the trickle's


def model_rates(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                end: str) -> Rates:
    """The trickle, leftover rate and sweep pace over the windows ending at `end`."""
    _, background = fifo_background(cohorts, names, end)
    months = leftover_months(cohorts, names, end)
    leftover = None if months is None else leftover_rate(cohorts, names, end, months)
    return Rates(background, max(leftover or 0.0, background),
                 sweep_pace(cohorts, names, end, background), months)


def half_life(rate: float) -> str:
    """Months until half of a group clearing at `rate` (> 0) a month is left."""
    return f"{math.log(0.5) / math.log1p(-min(rate, 0.999)):.0f} months"


def bullets(items: list[str], width: int) -> str:
    """`items` as a list, one "- " bullet each, wrapped with a hanging indent."""
    return "\n".join(wrap(f"- {item}", width, hanging="  ") for item in items)


def estimate_items(odds: tuple[float, ...]) -> list[str]:
    """What the estimate columns mean, for the chances shown: one item each."""
    shown = [f"{q:.0%}" for q in odds]
    listed = ", ".join(shown[:-1]) + f" and {shown[-1]}" if len(shown) > 1 else shown[0]
    return [f"{listed}: months until your chance of having been processed "
            "reaches that. The 50% is about when the sweep finishes with your "
            "cohort."]


def leftover_prose(background: float, leftover: float, first: str,
                   months: int | None = None) -> str:
    """How fast the leftovers clear, and so how long they wait."""
    if leftover <= 0:
        return ("With no trickle and nothing it has passed clearing, its "
                "leftovers never clear.")
    if leftover <= background:
        rate = ("Leftovers the sweep has passed clear at the trickle (they "
                "haven't lately cleared faster)")
    else:
        rate = (f"Leftovers clear at {fmt_pct(leftover)} a month (pooled over "
                f"the last {months or RECENT_MONTHS} months: everyone before "
                f"{first}, plus any cohort the sweep had passed when the window "
                "opened)")
    return (f"{rate}: half are still waiting after {half_life(leftover)}. So "
            "once the sweep has passed you, this rate, not your place in the "
            "queue, sets how long you wait.")


def sweep_items(rates: Rates, pace_words: str, first: str) -> list[str]:
    """How the estimate model works, one item per part, for both reports."""
    return [f"A trickle clears {fmt_pct(rates.background)} of every cohort a "
            "month, old or new: the pooled rate over the last "
            f"{RECENT_MONTHS} months of the cohorts the sweep hasn't reached.",
            f"An oldest-first sweep clears {pace_words} on top of the trickle. "
            "It works through one cohort at a time until "
            f"{STRAGGLERS:.0%} of those waiting when it got there are left, "
            "then moves on. Within a cohort, who it takes is random.",
            f"For the cohort it's in now, that {STRAGGLERS:.0%} is of its count "
            "the month before the cohort ahead got down to its leftovers "
            f"({first}, the first cohort counted: of its largest count "
            f"since {SIZE_FROM}, after the C-3 change). A cohort counts as "
            f"passed after {PASSED_MONTHS} months in a row with nothing left "
            "for the sweep to take.",
            "The trickle thins the files ahead too, so the sweep arrives "
            "sooner than its pace alone suggests.",
            leftover_prose(rates.background, rates.leftover, first,
                           rates.leftover_months)]


BACKTEST_NOTE = ("The backtest only checks 1-6 months ahead, and the sweep "
                 "reaches few cohorts that soon, so arrival times and "
                 "everything after them are extrapolation.")


def print_fifo(cohorts: dict[str, list[tuple[str, float]]], width: int,
               target: str = DEFAULT_COHORT) -> None:
    """
    The report: every settled cohort's estimates, then `target`'s in detail,
    then how close to oldest-first processing is, then how the model works.
    """
    names = fifo_start(cohorts, "WHEN EACH COHORT IS PROCESSED", width)
    if not names:
        return
    last = latest_period(cohorts)
    rates = model_rates(cohorts, names, last)
    print_every_cohort(cohorts, names, last, rates, width, target)
    known = target in cohorts and last in own_cohort(cohorts, target).waiting
    if known:
        print_target(cohorts, names, last, rates, target, width)
    else:
        print()
        print(wrap(f"No {target} own-cohort figure for {last}, so there's no "
                   "order score or estimate for it.", width))
    print_order_section(cohorts, names, last, rates, target if known else None,
                        width)
    print("\n" + paint("How the model works", "bold", "cyan"))
    pace_words = ("files at a pace too new to measure" if rates.pace is None else
                  f"{fmt_count(max(rates.pace, 0))} files a month (last "
                  f"{RECENT_MONTHS} months)")
    print(bullets(sweep_items(rates, pace_words, names[0])
                  + [unsettled_note(names), BACKTEST_NOTE], width))
    print(paint("=" * width, "cyan") + "\n")


def print_target(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                 last: str, rates: Rates, target: str, width: int) -> None:
    """When `target` is likely to be processed: 50/75/90% with and without the sweep."""
    pos = position(cohorts, target, last)
    ahead = dict(cohorts[target])[last]
    print("\n" + paint(f"When will {target} be processed?", "bold", "cyan"))
    print(bullets(estimate_items(ESTIMATE_ODDS), width))
    print()
    print(wrap(f"Of the {fmt_count(ahead)} ahead, "
               f"{fmt_count(pos.work)} are still to be swept, then the sweep "
               f"takes {fmt_count(pos.take)} of its {fmt_count(pos.waiting)} "
               "(fewer by then, as the trickle thins both). The rows: no sweep "
               f"is the trickle alone, recent the latest {RECENT_MONTHS}-month "
               "pace.",
               width))
    print()
    print_estimates(fifo_paces(cohorts, names, last, rates.background),
                    rates.background, pos, width, rates.leftover,
                    passed_by(cohorts, target, last))


def print_order_section(cohorts: dict[str, list[tuple[str, float]]],
                        names: list[str], last: str, rates: Rates,
                        target: str | None, width: int) -> None:
    """
    Each cohort's own clearance, the trickle and leftover rates, and (with a
    `target` that has figures) its oldest-first score.
    """
    print("\n" + paint("Processing order", "bold", "cyan"))
    print(wrap(f"Each cohort's own applicants still waiting in {last}, and the "
               "share of them cleared per month (compounded) over the last "
               f"{' and '.join(map(str, OWN_WINDOWS))} months. 'earlier' is "
               f"everyone who applied before {names[0]}.", width))
    print()
    print_fifo_table(cohort_rates(cohorts, names, last), target or "", width)
    trickle, _ = fifo_background(cohorts, names, last)
    leftover = (None if rates.leftover_months is None
                else leftover_rate(cohorts, names, last, rates.leftover_months))
    print()
    labels = [f"Trickle, last {RECENT_MONTHS} mo",
              f"Leftovers, last {rates.leftover_months or RECENT_MONTHS} mo"]
    for label, rate in zip(labels, (trickle, leftover)):
        print(stat_line(label, "n/a" if rate is None else f"{fmt_pct(rate)} a month",
                        width, pad=max(map(len, labels)) + 2))
    if target is None:
        return
    newer = [c for c in names if c > target]
    scores = [s for n in OWN_WINDOWS
              if (s := order_score(cohorts, target, newer, last, n))]
    print("\n" + paint("Oldest-first or random?", "bold", "cyan"))
    print_order(scores, target, newer, width)


# --summarize shows each cohort's estimates at the recent RECENT_MONTHS pace
# only, which backtested closest (median and best ran far too fast).
def fifo_percent(score: float | None) -> str:
    """An OrderScore.score() as "% fifo"; clamped to +-999% for degenerate data."""
    return "n/a" if score is None else f"{max(min(score, 9.99), -9.99):.0%}"


class FifoSummaryRow(NamedTuple):
    """One cohort of the --summarize report, as text."""
    cohort: str
    ahead: str
    work: str
    waiting: str
    leftover: str
    fifo: str
    split: tuple[Odds, ...] = ()          # split_cells(), under SPLIT_HEADS


def leftover_percent(pos: Position) -> str:
    """The share of `pos.waiting` the sweep won't take, as "leftover %"."""
    if pos.waiting <= 0:
        return "n/a"
    return f"{1 - min(max(pos.take, 0.0), pos.waiting) / pos.waiting:.0%}"


def fifo_summary_rows(cohorts: dict[str, list[tuple[str, float]]],
                      names: list[str], last: str,
                      rates: Rates) -> list[FifoSummaryRow]:
    """
    Each settled cohort's people ahead, files still to be swept ahead of it
    and own still waiting in `last`, its leftover share if the sweep is in
    it or has passed it (behind the sweep it's always STRAGGLERS), its
    OrderScore over the last RECENT_MONTHS against the settled cohorts after
    it, and its split_cells() at `rates` (model_rates() at `last`).
    """
    rows = []
    for name in names:
        pos = position(cohorts, name, last)
        if pos is None:
            continue
        newer = [c for c in names if c > name]
        score = order_score(cohorts, name, newer, last, RECENT_MONTHS)
        rows.append(FifoSummaryRow(
            name, fmt_count(dict(cohorts[name])[last]), fmt_count(pos.work),
            fmt_count(pos.waiting),
            leftover_percent(pos) if pos.work <= 0 else "",
            fifo_percent(score and score.score()),
            tuple(split_cells(rates, pos, passed_by(cohorts, name, last)))))
    return rows


COUNT_FIELDS = (("ahead", "ahead"), ("to sweep", "work"), ("waiting", "waiting"),
                ("leftover", "leftover"), ("% fifo", "fifo"))


def print_columns(rows: list[tuple[str, tuple[str, ...]]], heads: tuple[str, ...],
                  width: int, target: str = "") -> bool:
    """
    (cohort, cells) rows as a table under `heads`, `target`'s row bold, if
    it fits in `width`; False, printing nothing, if it doesn't.
    """
    widths = [max(len(h), *(len(cells[i]) for _, cells in rows))
              for i, h in enumerate(heads)]
    if 2 + 7 + sum(2 + w for w in widths) > width:
        return False
    print(paint(f"  {'cohort':<7}" + "".join(
        f"  {h:>{w}}" for h, w in zip(heads, widths)), "dim"))
    for cohort, cells in rows:
        emph = ("bold",) if cohort == target else ()
        print(paint(f"  {cohort:<7}", *emph) + "".join(
            "  " + paint(f"{c:>{w}}", *emph, *(("red",) if "never" in c else ()))
            for c, w in zip(cells, widths)))
    return True


def summary_lead_text(r: FifoSummaryRow) -> str:
    """A cohort's counts as a sentence, for terminals too narrow for the table."""
    return (f"{r.cohort}  {r.ahead} ahead ({r.work} to sweep), {r.waiting} "
            "waiting, " + (f"{r.leftover} leftover, " if r.leftover else "")
            + f"{r.fifo} fifo")


def print_counts(rows: list[FifoSummaryRow], width: int, target: str) -> None:
    """The counts table; narrower than that, a sentence per cohort."""
    table = [(r.cohort, tuple(getattr(r, f) for _, f in COUNT_FIELDS)) for r in rows]
    if not print_columns(table, tuple(h for h, _ in COUNT_FIELDS), width, target):
        for r in rows:
            print(paint(wrap(summary_lead_text(r), width, indent="  ",
                             hanging="    "), "bold"))


def print_split(rows: list[FifoSummaryRow], width: int, target: str) -> None:
    """The estimates table; narrower than that, one line per cell per cohort."""
    table = [(r.cohort, tuple(map(odds_text, r.split))) for r in rows]
    if print_columns(table, SPLIT_HEADS, width, target):
        return
    head_w = max(map(len, SPLIT_HEADS))
    for cohort, cells in table:
        print(paint(f"  {cohort}", "bold"))
        for head, cell in zip(SPLIT_HEADS, cells):
            print(f"    {head:>{head_w}}  {estimate_group([cell], [len(cell)])}")


def summary_items(names: list[str], last: str) -> list[str]:
    """What each --summarize column means."""
    return [
        "Ahead: people ahead of the cohort. To sweep: those the sweep still "
        "has to clear before it gets to the cohort. Waiting: the cohort's own "
        "applicants still waiting.",
        "Leftover: the share of those waiting that the sweep will leave "
        f"behind. Blank for cohorts it hasn't reached, where it's "
        f"{STRAGGLERS:.0%} by assumption.",
        f"% fifo: how close to oldest-first processing has been over the last "
        f"{RECENT_MONTHS} months, against the cohorts newer than it. 0% is "
        "random order, 100% strictly oldest first, below 0% the newer files "
        "went first. For the newest cohorts it's mostly rounding.",
        "Estimates: your chance of having been processed by when the sweep "
        "arrives ('now' if it's in the cohort, 'passed' if it's done with "
        "it), by when it ends (after that you're a leftover), and by when "
        "half the leftovers have been processed. 'never' means the sweep "
        "or the leftovers aren't moving, '-' that there's no estimate.",
        f"{times_note(last)}, at the rates over the last {RECENT_MONTHS} "
        "months."]


def print_every_cohort(cohorts: dict[str, list[tuple[str, float]]],
                       names: list[str], last: str, rates: Rates, width: int,
                       target: str) -> None:
    """Every settled cohort's counts and estimates, `target`'s row bold."""
    rows = fifo_summary_rows(cohorts, names, last, rates)
    print(wrap(f"For each cohort from {names[0]}, in {last}:", width))
    print(bullets(summary_items(names, last), width))
    print()
    if not rows:
        print(wrap(f"No settled cohort has figures for {last}.", width))
        return
    print_counts(rows, width, target)
    print()
    print(paint("Estimates:", "bold"))
    print_split(rows, width, target)


def print_short(cohorts: dict[str, list[tuple[str, float]]], width: int,
                target: str = DEFAULT_COHORT) -> None:
    """--short: only the report's estimates table, `target`'s row bold."""
    names = settled_or_note(cohorts, width)
    if not names:
        return
    last = latest_period(cohorts)
    rows = fifo_summary_rows(cohorts, names, last, model_rates(cohorts, names, last))
    if rows:
        print_split(rows, width, target)
    else:
        print(wrap(f"No settled cohort has figures for {last}.", width))


# The iPhone widget (scriptable/citizenship.js) reads this; flpt_watch writes it
# hourly, but only once Scriptable's iCloud folder exists.
WIDGET_PATH = Path("~/Library/Mobile Documents/iCloud~dk~simonbs~Scriptable/"
                   "Documents/citizenship.json").expanduser()
# The SPLIT_HEADS the widget shows, and what it calls them.
WIDGET_FIELDS = (("arrives", "Sweep arrives"), ("sweep ends", "Sweep done"),
                 ("leftovers half", "Leftovers half"))


def widget_feed(cohorts: dict[str, list[tuple[str, float]]],
                cohort: str = DEFAULT_COHORT) -> dict | None:
    """
    `cohort`'s --summarize estimates as the widget's JSON: a title, the period
    they're from, and one row per WIDGET_FIELDS: your chance of having been
    processed by then (value; the time itself where it's a word with no
    chance, such as now or passed) and "by" its month as short_month()
    (detail, "" with no chance). None if it isn't settled or has no figure
    for the latest period.
    """
    names = settled_cohorts(cohorts)
    if cohort not in names:
        return None
    last = latest_period(cohorts)
    cells = widget_cells(cohorts, names, last, cohort)
    if cells is None:
        return None
    return {"version": 3, "title": f"Citizenship {cohort}", "period": last,
            "rows": [widget_row(label, cells[head]) for head, label in WIDGET_FIELDS]}


def widget_row(label: str, odds: Odds) -> dict:
    """One widget column: the chance, and "by" the month it's at."""
    if not odds.chance:
        return {"label": label, "value": short_month(odds.when), "detail": ""}
    return {"label": label, "value": odds.chance,
            "detail": f"by {short_month(odds.when)}"}


def short_month(text: str) -> str:
    """A YYYY-MM month as the widget's M'YY (2028-01 -> 1'28); anything else as is."""
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    year, month = text.split("-")
    return f"{int(month)}'{year[2:]}"


def widget_cells(cohorts: dict[str, list[tuple[str, float]]], names: list[str],
                 last: str, cohort: str) -> dict[str, Odds] | None:
    """`cohort`'s summary Odds by SPLIT_HEADS, as months from `last`; None if absent."""
    with dates_from(last):
        rows = fifo_summary_rows(cohorts, names, last,
                                 model_rates(cohorts, names, last))
    row = next((r for r in rows if r.cohort == cohort), None)
    return None if row is None else dict(zip(SPLIT_HEADS, row.split))


def write_widget(history: Path = HISTORY_PATH, path: Path | None = None,
                 cohort: str = DEFAULT_COHORT) -> bool:
    """
    Write widget_feed() to `path` (WIDGET_PATH, read at call time so tests
    can patch it) atomically. False, writing nothing, when the folder
    doesn't exist (Scriptable isn't set up) or there's no feed.
    """
    path = path or WIDGET_PATH
    feed = widget_feed(load_cohorts(history), cohort)
    if feed is None or not path.parent.is_dir():
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(feed, f, indent=1)
    os.replace(tmp, path)
    return True


def warn_rejected(rejected: list[int], width: int) -> None:
    """Flag the history rows load_cohorts couldn't read, by line number."""
    if rejected:
        lines = ", ".join(map(str, rejected))
        print(paint(wrap(f"⚠  Skipped unreadable rows in {HISTORY_PATH.name} "
                         f"(line{'s' if len(rejected) > 1 else ''} {lines}).", width, hanging="    "), "yellow"))


def cohort_arg(text: str) -> str:
    """argparse type for --fifo: a month, normalised by month_key()."""
    cohort = month_key(text)
    if cohort is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM month: {text!r}")
    return cohort


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="When each cohort in the processing-times history is "
                    "likely to be processed.")
    parser.add_argument(
        "cohort", nargs="?", default=DEFAULT_COHORT, type=cohort_arg,
        help=f"the cohort to show in detail (default {DEFAULT_COHORT})")
    parser.add_argument(
        "--short", action="store_true",
        help="only the estimates table")
    parser.add_argument(
        "--durations", action="store_true",
        help="show each time as how long from the latest period (1y 5m) "
             "rather than the month (YYYY-MM) it comes to")
    return parser.parse_args(list(argv))


def main(argv=()) -> None:
    """The report (--short: its estimates table) for the history's cohorts."""
    global COLOR
    args = parse_args(argv)
    COLOR = color_enabled()
    width = output_width()
    rejected: list[int] = []
    cohorts = load_cohorts(HISTORY_PATH, rejected)
    warn_rejected(rejected, width)
    with dates_from(None if args.durations or not cohorts else latest_period(cohorts)):
        (print_short if args.short else print_fifo)(cohorts, width, args.cohort)


if __name__ == "__main__":
    main(sys.argv[1:])
