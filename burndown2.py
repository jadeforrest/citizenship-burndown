#!/usr/bin/env python3
"""
Burn-down timeline analyzer.

Enter a comma-separated list of backlog counts (the number of people ahead of
you in line), where the FIRST number is the current month and each number after
it is the previous month. Example:

    9700, 10200, 10500, 11300, 11700, 13000, 14500, 15800

The script figures out how many people are processed per month, computes low /
likely / high estimates from the stats, and prints how much time is probably
left before the queue clears.

Each estimate is broken out by how much of the cohort ahead of you has been
processed (25% / 50% / 75% / 100%). If applications are pulled in roughly
random order rather than strict FIFO, the 50% column is the point where you'd
have a coin-flip chance of already being done, and 100% is the point where
everyone ahead of you is finished.

A final section flips that around: instead of "how long until I'm X% likely to
be done", it reports the per-month odds — what your chance of being pulled was
in each month of the window, and your chance of being done by a few fixed
horizons.

The report fits itself to the terminal: wide windows get the full side-by-side
tables, medium ones get abbreviated cells, and narrow ones get one block per
scenario. Nothing is dropped at any width. Set BURNDOWN_WIDTH to override the
detected width.

If flpt_watch.py has been recording flpt_history.csv, the prompt also offers
each cohort it has tracked: type a cohort (2025-07) or its menu number and its
monthly people-ahead counts are read out newest first, as if you'd pasted them.

    python3 burndown2.py --summarize

skips the prompt and prints a one-line summary for every cohort from 2025-01:
its own applicants cleared last month, and the recent and median paces with
the months to 50% and 100% cleared.
"""

import argparse
import csv
import math
import os
import re
import shutil
import statistics
import sys
import textwrap
from pathlib import Path
from typing import NamedTuple


def parse_numbers(raw: str) -> list[float]:
    """Parse a comma- (or whitespace-) separated line into a list of numbers."""
    # Allow commas, spaces, tabs, and stray pipes as separators.
    for sep in ("|", "\t"):
        raw = raw.replace(sep, ",")
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    numbers = []
    for p in parts:
        if not p:
            continue
        try:
            numbers.append(float(p))
        except ValueError:
            raise ValueError(f"Could not read {p!r} as a number.")
    return numbers


def extract_backlog(numbers: list[float]) -> tuple[list[float], list[float], str]:
    """
    Detect and strip a leading counter/index column.

    Some pastes come as two columns: [marker, value, marker, value, ...]
    e.g.  1  2600  1  2700  1  3100  2  7800
    where the odd-position tokens (1, 1, 1, 2, ...) are page/row markers to
    ignore. This keeps every 2nd token when the input clearly looks like
    counter/value pairs, and otherwise leaves the list untouched.

    Returns (values, dropped, description).
    """
    n = len(numbers)
    if n >= 4 and n % 2 == 0:
        markers = numbers[0::2]   # 1st, 3rd, 5th ...  (candidate counters)
        values = numbers[1::2]    # 2nd, 4th, 6th ...  (candidate backlog)
        # Treat as two-column only if every marker is much smaller than its
        # paired value AND the biggest marker is far below the smallest value.
        # That ratio guard keeps genuine single-column data from being halved.
        looks_paired = (
            all(m < v for m, v in zip(markers, values))
            and max(markers) * 5 <= min(values)
        )
        if looks_paired:
            return values, markers, "two-column (dropped counter, kept every 2nd value)"
    return numbers, [], "single column"


# Fractions of the cohort ahead of you that have to clear. With roughly random
# processing order, "25% of the queue processed" is also the point where you'd
# have a 25% chance of already being done — i.e. these are your 25th/50th/75th
# percentile wait times, with 100% as the guaranteed-done upper end.
QUANTILES = (0.25, 0.50, 0.75, 1.00)

# Width of one fmt_months() cell, for lining the quantile columns up, plus the
# abbreviated cell used when the full-width table won't fit the terminal.
CELL = 20
CELL_COMPACT = 13

# Left-hand block of a scenario row, before the quantile columns start:
# wide is "  " + label(18) + "  " + rate(7) + "/mo  ", compact squeezes the
# label and trims the padding either side of the rate.
WIDE_LEFT = 2 + 18 + 2 + 7 + 5
COMPACT_LEFT = 2 + 11 + 1 + 7 + 4


def table_width(cell: int, left: int) -> int:
    """Total width of a quantile table with the given cell and left block."""
    return left + len(QUANTILES) * cell + (len(QUANTILES) - 1)


WIDE_TABLE = table_width(CELL, WIDE_LEFT)
COMPACT_TABLE = table_width(CELL_COMPACT, COMPACT_LEFT)

# Below COMPACT_TABLE the tables stack instead of sitting side by side; the
# stacked form and the prose still need a floor to wrap against.
MIN_WIDTH = 40
MAX_WIDTH = WIDE_TABLE

# Tables use the whole terminal, but prose stops being readable long before
# that, so paragraphs wrap at the narrower of the two.
PROSE_WIDTH = 78

# Horizons (in months) for the forward-looking odds table.
HORIZONS = (1, 3, 6, 12, 24)

# Used when you just press return at the prompt: keep your own numbers here and
# rerunning the script is a single keystroke.
DEFAULT_INPUT = "9700, 10100, 10200, 10500, 11300, 11700, 13000, 14500, 15800"

# Written by flpt_watch.py. When it exists, pressing return analyzes
# DEFAULT_COHORT from it instead of DEFAULT_INPUT.
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
    newest period first — the same order the prompt expects pasted numbers in.

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


def missing_months(periods: list[str]) -> list[str]:
    """YYYY-MM months absent between the oldest and newest of `periods`."""
    present = {month_index(p) for p in periods}
    return [month_label(i)
            for i in range(min(present), max(present) + 1) if i not in present]


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


def own_windows(own: OwnCohort) -> list[tuple[int, float | None, float | None]]:
    """
    (months, own-cohort rate, people-ahead rate) over each trailing window
    ending at the newest period with an own-cohort figure, shortest first and
    the full span last. Windows whose starting month is missing are skipped.
    """
    if len(own.waiting) < 2:
        return []
    periods = sorted(own.waiting)
    last = month_index(periods[-1])
    span = last - month_index(periods[0])
    rows = []
    for n in [w for w in OWN_WINDOWS if w < span] + [span]:
        start = month_label(last - n)
        if start in own.waiting:
            rows.append((n,
                         shrink_rate(own.waiting[start], own.waiting[periods[-1]], n),
                         shrink_rate(own.ahead[start], own.ahead[periods[-1]], n)))
    return rows


def cohort_menu(cohorts: dict[str, list[tuple[str, float]]]) -> list[str]:
    """Cohorts with enough data to analyze (two months or more), in menu order."""
    return [c for c, series in cohorts.items() if len(series) >= 2]


def describe_cohort(cohort: str, series: list[tuple[str, float]]) -> str:
    span = "month" if len(series) == 1 else "months"
    return (f"{cohort}  {len(series)} {span} of data "
            f"({series[-1][0]} to {series[0][0]})")


def pick_cohort(raw: str, cohorts: dict, menu: list[str]) -> str | None:
    """
    The cohort `raw` names, if any: a menu number, or YYYY-MM / YYYY/MM.

    Anything else (pasted counts, typos) returns None and is parsed as numbers.
    """
    text = raw.strip().replace("/", "-")
    if text.isdigit() and 1 <= int(text) <= len(menu):
        return menu[int(text) - 1]
    return text if text in cohorts else None


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


def layout_for(width: int) -> str:
    """Which quantile-table shape fits: 'wide', 'compact', or 'stacked'."""
    if width >= WIDE_TABLE:
        return "wide"
    if width >= COMPACT_TABLE:
        return "compact"
    return "stacked"


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


def fmt_months(months: float) -> str:
    """Human-friendly rendering of a month count, never wider than CELL."""
    if months == float("inf"):
        return "never (rate <= 0)"
    years = months / 12
    text = (f"{months:>5.1f} mo  (~{years:.1f} yr)" if years >= 1
            else f"{months:>5.1f} mo  (~{years:.2f} yr)")
    if len(text) > CELL:
        # Degenerate inputs (a negative backlog, a rate near zero) run past the
        # column; drop to the tighter form rather than break the alignment.
        text = fmt_months_compact(months)
    return text


def fmt_months_compact(months: float) -> str:
    """Same figures as fmt_months, squeezed into CELL_COMPACT characters."""
    if months == float("inf"):
        return "never"
    years = months / 12
    text = (f"{months:>5.1f}mo {years:.1f}y" if years >= 1
            else f"{months:>5.1f}mo {years:.2f}y")
    if len(text) > CELL_COMPACT:
        text = f"{months:,.0f}mo {years:,.0f}y"
    if len(text) > CELL_COMPACT:
        text = f"{months:.2e}mo"
    return text


def quantile_header(indent: str, cell: int = CELL) -> str:
    """Column header naming each quantile of the cohort."""
    cells = " ".join(f"{q * 100:.0f}% done".center(cell) for q in QUANTILES)
    return indent + cells


def fmt_pct(fraction: float) -> str:
    """Render a 0..1 probability as a percentage."""
    return f"{fraction * 100:.1f}%"


def monthly_odds(chrono: list[float], processed: list[float]) -> list[float | None]:
    """
    Per-month probability that you personally would have been pulled.

    The input counts are *people ahead of you*, so `processed[i]` is the number
    of people ahead of you who cleared in month i. Under uniform random
    processing every file in the queue has the same chance of being drawn, so
    the fraction of the ahead-of-you cohort that cleared is also your own odds
    for that month. Months where the queue grew (or the starting count is
    non-positive) have no meaningful probability and come back as None.
    """
    odds: list[float | None] = []
    for i, done in enumerate(processed):
        start = chrono[i]
        if start <= 0 or done <= 0:
            odds.append(None)
        else:
            odds.append(min(done / start, 1.0))
    return odds


def done_by(current: float, rate: float, months: float) -> float | None:
    """
    Chance you're finished within `months`, at a steady `rate` per month.

    Random draws without replacement: once k of the `current` people ahead of
    you have been pulled, the chance you were one of them is exactly k/current.
    So the probability grows linearly until the queue empties.
    """
    if rate <= 0 or current <= 0:
        return None
    return min(rate * months / current, 1.0)


def print_scenarios(
    rows: list[tuple[str, str, float, tuple[str, ...]]],
    months_left,
    layout: str,
    width: int,
) -> None:
    """
    Render the quantile table for `rows` of (label, short label, rate, style),
    where style names the STYLES that colour the row's label.

    Wide and compact both put the quantiles across the page; stacked gives each
    scenario its own block with one line per quantile, so the same numbers
    survive a terminal too narrow for any grid.
    """
    if layout == "stacked":
        for n, (label, _short, rate, style) in enumerate(rows):
            if n:
                print()
            print(f"  {paint(f'{label:<18s}', *style)} {max(rate, 0):>7,.0f}/mo")
            for q in QUANTILES:
                cell = fmt_months(months_left(rate, q))
                print(f"      {q * 100:>3.0f}% done  {cell}".rstrip())
        return

    wide = layout == "wide"
    cell_w = CELL if wide else CELL_COMPACT
    fmt = fmt_months if wide else fmt_months_compact
    print(paint(quantile_header(" " * (WIDE_LEFT if wide else COMPACT_LEFT),
                                cell_w).rstrip(), "dim"))
    for label, short, rate, style in rows:
        cells = " ".join(
            f"{fmt(months_left(rate, q)):{cell_w}s}" for q in QUANTILES
        )
        if wide:
            print(f"  {paint(f'{label:18s}', *style)}  "
                  f"{max(rate, 0):>7,.0f}/mo  {cells}".rstrip())
        else:
            print(f"  {paint(f'{short:11s}', *style)} "
                  f"{max(rate, 0):>7,.0f}/mo {cells}".rstrip())


def odds_cell(done: float, chance: float | None) -> tuple[str, tuple[str, ...]]:
    """A lookback month's odds as text, plus the STYLES that colour it."""
    if chance is not None:
        return fmt_pct(chance), ()
    if done < 0:
        return "grew", ("red",)
    if done == 0:
        return "flat", ("yellow",)
    return "n/a", ()


def print_lookback(
    chrono: list[float],
    processed: list[float],
    odds: list[float | None],
    width: int,
) -> None:
    """Month-by-month table of the odds you'd already have been pulled."""
    queues = [f"{v:,.0f}" for v in chrono[: len(processed)]]
    cleared = [f"{v:,.0f}" for v in processed]

    # Try the spelled-out headings first, then abbreviations if they overflow.
    headings = (
        ("month", "queue at start", "cleared", "chance", "  <- last month"),
        ("mo", "queue", "done", "odds", "  <-"),
    )
    for month_h, queue_h, done_h, odds_h, note in headings:
        month_w = max(len(month_h), 3)
        queue_w = max(len(queue_h), max(len(v) for v in queues))
        done_w = max(len(done_h), max(len(v) for v in cleared))
        odds_w = max(len(odds_h), 6)
        total = 4 + month_w + 2 + queue_w + 2 + done_w + 2 + odds_w + len(note)
        if total <= width:
            break

    print(paint(wrap("Looking back — your chance of being picked, month by "
                     "month", width, indent="  ", hanging="  "), "bold"))
    print(paint(f"    {month_h:>{month_w}}  {queue_h:>{queue_w}}  "
                f"{done_h:>{done_w}}  {odds_h:>{odds_w}}", "dim"))
    for i, (done, chance) in enumerate(zip(processed, odds)):
        age = len(processed) - i
        cell, tone = odds_cell(done, chance)
        emph = ("bold",) if age == 1 else ()
        print(paint(f"    {f'-{age}':>{month_w}}  {queues[i]:>{queue_w}}  "
                    f"{cleared[i]:>{done_w}}  ", *emph)
              + paint(f"{cell:>{odds_w}}", *emph, *tone)
              + paint(note if age == 1 else "", *emph))


def print_lookahead(
    current: float,
    mean: float,
    recent: float,
    recent_n: int,
    width: int,
) -> None:
    """Forward-looking odds of being done by each horizon, at two paces."""
    avg_head, rec_head = "avg pace", f"recent {recent_n} mo"
    avg_rate, rec_rate = f"({max(mean, 0):,.0f}/mo)", f"({max(recent, 0):,.0f}/mo)"
    labels = ["next month"] + [f"{h} months" for h in HORIZONS[1:]]
    label_w = max(len(s) for s in labels)

    joined = (f"{avg_head} {avg_rate}", f"{rec_head} {rec_rate}")
    col = max(len(s) for s in joined) + 3
    stacked_head = 4 + label_w + 2 * col > width
    if stacked_head:
        # Pace and rate on two header lines so the columns can be narrower.
        col = max(len(s) for s in (avg_head, rec_head, avg_rate, rec_rate)) + 2

    print(paint(wrap("Looking ahead — chance you're done by the end of...",
                     width, indent="  ", hanging="  "), "bold"))
    if stacked_head:
        print(paint(f"    {'':{label_w}}{avg_head:>{col}}{rec_head:>{col}}", "dim"))
        print(paint(f"    {'':{label_w}}{avg_rate:>{col}}{rec_rate:>{col}}", "dim"))
    else:
        print(paint(f"    {'':{label_w}}{joined[0]:>{col}}{joined[1]:>{col}}",
                    "dim"))
    for label, h in zip(labels, HORIZONS):
        cells = ""
        for rate in (mean, recent):
            p = done_by(current, rate, h)
            tone = ("red",) if p is None else ("green",) if p >= 1 else ()
            cells += paint(f"{('never' if p is None else fmt_pct(p)):>{col}}",
                           *tone)
        print(f"    {label:{label_w}}{cells}")


def rate_cell(rate: float | None) -> tuple[str, tuple[str, ...]]:
    """Text and tone for a shrink_rate() result."""
    if rate is None:
        return "n/a", ()
    if rate < 0:
        return "grew", ("yellow",)
    return fmt_pct(rate), ()


def print_own_cohort(own: OwnCohort, width: int) -> None:
    """Your cohort's own monthly clearance, beside the people-ahead rate."""
    later = month_label(month_index(own.cohort) + 1)
    print(paint(f"  Your own cohort ({own.cohort})", "bold"))   # never wraps
    rows = own_windows(own)
    if not rows:
        print(wrap(f"No {later} figures for the same months, so there's no "
                   "own-cohort rate; the chances above count everyone ahead "
                   "of you.", width, indent="    ", hanging="    "))
        return
    print(wrap(f"Everyone from {own.cohort} still waiting is {later}'s people "
               "ahead minus yours. If files within a month are pulled in "
               "random order, the share of them cleared each month is your "
               "chance, however much older files are put first.",
               width, indent="    ", hanging="    "))
    first, last = min(own.waiting), max(own.waiting)
    print(stat_line("Still waiting",
                    f"{own.waiting[first]:,.0f} in {first} -> "
                    f"{own.waiting[last]:,.0f} in {last}",
                    width, "    ", pad=16, style=()))

    # Try the spelled-out headings first, then abbreviations if they overflow.
    for corner, own_h, ahead_h, long in (
            ("per month", "your cohort", "people ahead", True),
            ("/mo", "cohort", "ahead", False)):
        labels = [(f"last {n} months" if n > 1 else "last month") if long
                  else f"{n} mo" for n, _, _ in rows]
        label_w = max(len(s) for s in labels)   # each outruns its corner
        # A rate cell is at most 6 wide (100.0%); both own headings already are.
        own_w, ahead_w = len(own_h), max(len(ahead_h), 6)
        if 4 + label_w + 2 + own_w + 2 + ahead_w <= width:
            break
    print(paint(f"    {corner:<{label_w}}  {own_h:>{own_w}}  "
                f"{ahead_h:>{ahead_w}}", "dim"))
    for label, (_, mine, theirs) in zip(labels, rows):
        (mine_t, mine_tone), (theirs_t, theirs_tone) = rate_cell(mine), rate_cell(theirs)
        print(f"    {label:<{label_w}}  " + paint(f"{mine_t:>{own_w}}", *mine_tone)
              + "  " + paint(f"{theirs_t:>{ahead_w}}", *theirs_tone))
    print(wrap("IRCC rounds these counts to the nearest 100, so a single "
               "month of one cohort is mostly noise; the longer windows are "
               "steadier.", width, indent="    ", hanging="    "))


def random_order_odds(
    current: float,
    chrono: list[float],
    processed: list[float],
    mean: float,
    recent: float,
    recent_n: int,
    width: int,
    own: OwnCohort | None = None,
) -> None:
    """Print the per-month odds section (backward look, then forward look)."""
    print("\n" + paint("Random-order odds", "bold", "cyan"))
    print(wrap("If files are pulled in random order rather than strict FIFO, your "
               "chance of being picked in a month is that month's throughput "
               "divided by the queue at the start of the month.",
               width, indent="  ", hanging="  "))
    print(paint("-" * width, "dim"))

    odds = monthly_odds(chrono, processed)
    print_lookback(chrono, processed, odds, width)

    seen = [c for c in odds if c is not None]
    last = odds[-1]
    print()
    if last is not None:
        print(paint(wrap(f"Last month you had a {fmt_pct(last)} chance of "
                         "being processed.", width, indent="    ",
                         hanging="    "), "bold"))
    else:
        moved = "grew" if processed[-1] < 0 else "didn't move"
        print(paint(wrap(f"Last month the queue {moved}, so your chance of "
                         "being processed was effectively nil.",
                         width, indent="    ", hanging="    "), "yellow"))
    if seen:
        span = "month" if len(seen) == 1 else "months"
        print(wrap(f"Across the {len(seen)} {span} where the queue shrank, the "
                   f"average was {fmt_pct(statistics.mean(seen))}.",
                   width, indent="    ", hanging="    "))

    if own is not None:
        print()
        print_own_cohort(own, width)

    print()
    print_lookahead(current, mean, recent, recent_n, width)


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


def time_to_clear(current: float, rate: float, fraction: float = 1.0) -> float:
    """Months until `fraction` of `current` has cleared at `rate` a month."""
    return current * fraction / rate if rate > 0 else float("inf")


def analyze(entered: list[float], width: int | None = None,
            own: OwnCohort | None = None) -> None:
    if len(entered) < 2:
        print("Need at least two months to compute a rate.")
        return

    width = output_width() if width is None else max(MIN_WIDTH, width)
    layout = layout_for(width)

    current = entered[0]

    # Input is newest -> oldest; flip to chronological oldest -> newest.
    chrono = list(reversed(entered))

    # People processed each month (positive = queue shrinking).
    processed = processed_per_month(entered)

    # ---- direction sanity check -------------------------------------------
    net = chrono[-1] - chrono[0]  # newest - oldest
    if net > 0:
        print()
        print(paint(wrap(f"⚠  Heads up: the queue is GROWING over this window "
                         f"(net +{net:,.0f} since the oldest month). Estimates "
                         "below assume the average rate; if it keeps growing "
                         "it won't clear.", width, indent="  ",
                         hanging="     "), "bold", "yellow"))
        print()

    # ---- stats ------------------------------------------------------------
    lo = min(processed)
    hi = max(processed)
    mean = statistics.mean(processed)
    median = statistics.median(processed)
    stdev = statistics.stdev(processed) if len(processed) >= 2 else 0.0

    recent, recent_n = recent_pace(processed)

    fast = mean + stdev   # +1 sigma
    slow = mean - stdev   # -1 sigma

    def months_left(rate: float, fraction: float = 1.0) -> float:
        return time_to_clear(current, rate, fraction)

    # ---- report -----------------------------------------------------------
    banner = paint("=" * min(width, 60), "cyan")
    print("\n" + banner)
    print(paint("BURN-DOWN ANALYSIS", "bold", "cyan"))
    print(banner)
    print(stat_line("Current backlog", f"{current:>10,.0f}", width,
                    pad=28))
    print(stat_line("Months of data", f"{len(entered):>10d}", width,
                    pad=28, style=()))
    print(stat_line("Chronological (old->new)",
                    " -> ".join(f"{v:,.0f}" for v in chrono), width, pad=28,
                    style=()))
    print(stat_line("Processed each month",
                    ", ".join(f"{v:,.0f}" for v in processed), width, pad=28,
                    style=()))

    print("\n" + paint("Rate stats (people/month)", "bold", "cyan"))
    print(paint("-" * min(width, 60), "dim"))
    print(stat_line("Slowest month", f"{lo:>10,.0f}", width, "  "))
    print(stat_line("Fastest month", f"{hi:>10,.0f}", width, "  "))
    print(stat_line("Average", f"{mean:>10,.0f}", width, "  "))
    print(stat_line("Median", f"{median:>10,.0f}", width, "  "))
    print(stat_line("Std deviation", f"{stdev:>10,.0f}", width, "  "))
    print(stat_line(f"Recent pace (last {recent_n} mo)", f"{recent:>10,.0f}",
                    width, "  "))

    # Trend note.
    if len(processed) >= 4:
        first_half = statistics.mean(processed[: len(processed) // 2])
        second_half = statistics.mean(processed[len(processed) // 2 :])
        if second_half > first_half * 1.15:
            trend, tone = "ACCELERATING (speeding up)", ("bold", "green")
        elif second_half < first_half * 0.85:
            trend, tone = "DECELERATING (slowing down)", ("bold", "red")
        else:
            trend, tone = "roughly steady", ()
        print(stat_line("Trend", trend, width, "  ", style=tone))

    table_rule = paint("-" * width, "dim")
    axis = "Rows below each scenario" if layout == "stacked" else "Columns"

    print("\n" + paint("Time remaining scenarios", "bold", "cyan"))
    print(wrap(f"{axis} = share of the cohort ahead of you that has cleared. "
               "With random processing order those are your 25th/50th/75th "
               "percentile waits; 100% is everyone ahead of you being done.",
               width, indent="  ", hanging="  "))
    print(table_rule)
    print_scenarios(
        [
            ("Fast  (+1 sigma)", "Fast  +1sd", fast, ("green",)),
            ("Likely (average)", "Likely avg", mean, ("bold",)),
            ("Median", "Median", median, ()),
            (f"Recent ({recent_n} mo pace)", f"Recent {recent_n}mo", recent, ()),
            ("Slow  (-1 sigma)", "Slow  -1sd", slow, ("red",)),
        ],
        months_left,
        layout,
        width,
    )

    print()
    print(paint(wrap("Absolute bounds (single best / worst month)", width),
                "bold", "cyan"))
    print(table_rule)
    print_scenarios(
        [
            ("Best case  (max)", "Best (max)", hi, ("green",)),
            ("Likely     (avg)", "Likely(avg)", mean, ("bold",)),
            ("Worst case (min)", "Worst (min)", lo, ("red",)),
        ],
        months_left,
        layout,
        width,
    )

    random_order_odds(current, chrono, processed, mean, recent, recent_n, width,
                      own)
    print(paint("=" * width, "cyan") + "\n")


# --summarize covers every tracked cohort from this application month on.
SUMMARY_FROM = "2025-01"

# Widest a summary count or month cell gets; fmt_count / fmt_months_short
# degrade to e-notation rather than grow past it.
SUMMARY_CELL = 7


def fmt_count(value: float) -> str:
    """A count with thousands separators, in at most SUMMARY_CELL characters."""
    text = f"{value:,.0f}"
    return text if len(text) <= SUMMARY_CELL else f"{value:.0e}"


def fmt_months_short(months: float) -> str:
    """Months to clear (the unit is in the heading), in at most SUMMARY_CELL characters."""
    if months == float("inf"):
        return "never"
    for text in (f"{months:.1f}", f"{months:,.0f}"):
        if len(text) <= SUMMARY_CELL:
            return text
    return f"{months:.0e}"


def own_last_month(cohorts: dict[str, list[tuple[str, float]]],
                   cohort: str) -> float | None:
    """
    How many of `cohort`'s own applicants cleared in its latest month: the
    drop in own_cohort()'s still-waiting count. None unless the next cohort
    has figures for both that month and the one before it.
    """
    waiting = own_cohort(cohorts, cohort).waiting
    last = cohorts[cohort][0][0]
    before = month_label(month_index(last) - 1)
    if last in waiting and before in waiting:
        return waiting[before] - waiting[last]
    return None


def summary_rows(cohorts: dict[str, list[tuple[str, float]]],
                 start: str = SUMMARY_FROM
                 ) -> list[tuple[str, float, float | None, float, float]]:
    """
    (cohort, people ahead now, own applicants cleared last month, recent
    rate, median rate) for each cohort from `start` on with two or more
    months of data — the same paces analyze() calls Recent and Median.
    """
    rows = []
    for cohort in cohort_menu(cohorts):
        if cohort >= start:
            entered = [count for _, count in cohorts[cohort]]
            processed = processed_per_month(entered)
            rows.append((cohort, entered[0], own_last_month(cohorts, cohort),
                         recent_pace(processed)[0], statistics.median(processed)))
    return rows


def summary_cells(current: float, rate: float) -> tuple[str, str, str]:
    """Pace, time to 50% and time to 100% for one summary row."""
    return (fmt_count(rate),
            fmt_months_short(time_to_clear(current, rate, 0.5)),
            fmt_months_short(time_to_clear(current, rate, 1.0)))


class SummaryWidths(NamedTuple):
    """
    Column widths that vary with the data. The cleared and pace columns don't:
    fmt_count never exceeds SUMMARY_CELL, the width of their headings.
    """
    ahead: int
    cell: int

    def table(self) -> int:
        """Width of the side-by-side summary table."""
        return (2 + 7 + 2 + self.ahead + 2 + SUMMARY_CELL
                + 2 * (3 + SUMMARY_CELL + 2 * (2 + self.cell)))


def summary_layout(rows: list) -> tuple[list, SummaryWidths]:
    """summary_rows() rendered to text cells, and the column widths they need."""
    cells = [(cohort, fmt_count(current),
              "n/a" if cleared is None else fmt_count(cleared),
              summary_cells(current, recent), summary_cells(current, median))
             for cohort, current, cleared, recent, median in rows]
    times = [t for *_, recent, median in cells for g in (recent, median)
             for t in g[1:]]
    return cells, SummaryWidths(
        ahead=max(len("ahead"), *(len(c[1]) for c in cells)),
        cell=max(len("never"), *map(len, times)))


def summary_group(values: tuple[str, str, str], w: SummaryWidths) -> str:
    """One pace's pace / 50% / 100% cells, with never-clearing times in red."""
    pace, half, full = values
    cells = [paint(f"{t:>{w.cell}}", *(("red",) if t == "never" else ()))
             for t in (half, full)]
    return f"{pace:>{SUMMARY_CELL}}  " + "  ".join(cells)


SUMMARY_PACES = (f"recent ({RECENT_MONTHS} mo)", "median")


def print_summary_table(cells: list, w: SummaryWidths) -> None:
    """The summary as one row per cohort, both paces side by side."""
    group_w = SUMMARY_CELL + 2 * (2 + w.cell)
    lead = " " * (2 + 7 + 2 + w.ahead + 2 + SUMMARY_CELL)
    print(paint((lead + "".join(f"   {name:^{group_w}}"
                                for name in SUMMARY_PACES)).rstrip(), "dim"))
    heads = f"{'pace/mo':>{SUMMARY_CELL}}  {'50%':>{w.cell}}  {'100%':>{w.cell}}"
    print(paint(f"  {'cohort':<7}  {'ahead':>{w.ahead}}  "
                f"{'cleared':>{SUMMARY_CELL}}" + f"   {heads}" * 2, "dim"))
    for cohort, ahead, cleared, recent, median in cells:
        emph = ("bold",) if cohort == DEFAULT_COHORT else ()
        print(paint(f"  {cohort:<7}  {ahead:>{w.ahead}}  {cleared:>{SUMMARY_CELL}}",
                    *emph)
              + f"   {summary_group(recent, w)}   {summary_group(median, w)}")


def print_summary_stacked(cells: list, w: SummaryWidths, width: int) -> None:
    """The summary as a small block per cohort, one line per pace."""
    print(paint(f"    {'':6}  {'pace/mo':>{SUMMARY_CELL}}  {'50%':>{w.cell}}  "
                f"{'100%':>{w.cell}}", "dim"))
    for cohort, ahead, cleared, recent, median in cells:
        print(paint(wrap(f"{cohort}  {ahead} ahead, {cleared} cleared", width,
                         indent="  ", hanging="    "), "bold"))
        for name, values in zip(SUMMARY_PACES, (recent, median)):
            print(f"    {name.split()[0]:<6}  {summary_group(values, w)}")


def print_summary(cohorts: dict[str, list[tuple[str, float]]], width: int) -> None:
    """Recent and median times to 50% / 100% for every cohort from SUMMARY_FROM."""
    banner = paint("=" * min(width, 60), "cyan")
    print("\n" + banner)
    print(paint("COHORT SUMMARY", "bold", "cyan"))
    print(banner)
    rows = summary_rows(cohorts)
    if not rows:
        print(wrap(f"No cohort from {SUMMARY_FROM} on has two months of data "
                   f"in {HISTORY_PATH.name} yet.", width))
        return
    print(wrap(f"Months until 50% and 100% of the people ahead of each cohort "
               f"from {SUMMARY_FROM} have cleared, at its recent pace (the "
               f"average of its last {RECENT_MONTHS} months) and its median "
               "monthly pace. Cleared is how many of the cohort's own "
               "applicants were processed last month (the gap to the next "
               "cohort, so rounded to the nearest 100; n/a without it).", width))
    print()

    cells, w = summary_layout(rows)
    if w.table() <= width:
        print_summary_table(cells, w)
    else:
        print_summary_stacked(cells, w, width)

    gaps = [c for c, *_ in rows if missing_months([p for p, _ in cohorts[c]])]
    if gaps:
        print()
        print(paint(wrap(f"⚠  Months missing for {', '.join(gaps)}: their "
                         "rates treat the recorded months as consecutive.",
                         width, hanging="    "), "yellow"))
    print(paint("=" * width, "cyan") + "\n")


def print_cohort_menu(cohorts: dict, menu: list[str], width: int) -> None:
    print(wrap(f"Cohorts tracked in {HISTORY_PATH.name} (application month, "
               "months of data):", width))
    for n, cohort in enumerate(menu, 1):
        print(wrap(describe_cohort(cohort, cohorts[cohort]), width,
                   indent=f"  {n:>3}) ", hanging=" " * 7))
    thin = len(cohorts) - len(menu)
    if thin:
        print(wrap(f"({thin} more with only one month so far, not enough for a "
                   "rate.)", width, indent="       ", hanging="       "))
    print()


def cohort_numbers(cohort: str, series: list[tuple[str, float]],
                   width: int) -> str:
    """Report which cohort was picked, and return its counts as pasted input."""
    print(wrap(f"Using cohort {describe_cohort(cohort, series)}", width,
               hanging="    "))
    gaps = missing_months([period for period, _ in series])
    if gaps:
        print(paint(wrap(f"⚠  No data for {', '.join(gaps)}: the rates below "
                         "treat the recorded months as consecutive.", width,
                         hanging="    "), "yellow"))
    return ", ".join(f"{count:.0f}" for _, count in series)


def print_prompt(cohorts: dict, menu: list[str], default_cohort: str | None,
                 width: int) -> None:
    if menu:
        print_cohort_menu(cohorts, menu, width)
        print(wrap("Pick a cohort by number or YYYY-MM, or enter backlog counts, "
                   "comma-separated.", width))
    else:
        print(wrap("Enter backlog counts, comma-separated.", width))
    print(wrap("First number = current month, each next = previous month.", width))
    default_text = f"cohort {default_cohort}" if default_cohort else DEFAULT_INPUT
    print(wrap(f"Press return on its own to use: {default_text}",
               width, hanging="    "))
    print()


def resolve_input(raw: str, cohorts: dict, menu: list[str],
                  default_cohort: str | None, width: int) -> tuple[str, str | None]:
    """
    Turn what was typed (blank, a cohort, or counts) into a line of counts,
    and the cohort it came from (None for pasted counts).
    """
    if not raw.strip() and default_cohort:
        raw = default_cohort
    elif not raw.strip():
        raw = DEFAULT_INPUT
        print(wrap(f"Using the default: {DEFAULT_INPUT}", width, hanging="    "))

    cohort = pick_cohort(raw, cohorts, menu)
    if cohort:
        raw = cohort_numbers(cohort, cohorts[cohort], width)
    return raw, cohort


def warn_rejected(rejected: list[int], width: int) -> None:
    """Flag the history rows load_cohorts couldn't read, by line number."""
    if rejected:
        lines = ", ".join(map(str, rejected))
        print(paint(wrap(f"⚠  Skipped unreadable rows in {HISTORY_PATH.name} "
                         f"(line{'s' if len(rejected) > 1 else ''} {lines}).", width, hanging="    "), "yellow"))


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate how long the queue ahead of you will take to clear.")
    parser.add_argument(
        "--summarize", action="store_true",
        help=f"no prompt: recent and median times to 50%% and 100%% for every "
             f"cohort from {SUMMARY_FROM} in {HISTORY_PATH.name}")
    return parser.parse_args(list(argv))


def main(argv=()) -> None:
    global COLOR
    args = parse_args(argv)
    COLOR = color_enabled()
    width = output_width()
    rejected: list[int] = []
    cohorts = load_cohorts(HISTORY_PATH, rejected)
    warn_rejected(rejected, width)
    if args.summarize:
        print_summary(cohorts, width)
        return
    menu = cohort_menu(cohorts)
    default_cohort = DEFAULT_COHORT if DEFAULT_COHORT in menu else None
    print_prompt(cohorts, menu, default_cohort, width)

    try:
        raw = input(paint("> ", "bold"))
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    raw, cohort = resolve_input(raw, cohorts, menu, default_cohort, width)

    try:
        tokens = parse_numbers(raw)
    except ValueError as e:
        print(paint(f"Error: {e}", "bold", "red"))
        sys.exit(1)

    numbers, dropped, mode = extract_backlog(tokens)

    if dropped:
        print(f"\nInput mode: {mode}")
        print(wrap("Using:   " + ", ".join(f"{v:,.0f}" for v in numbers),
                   width, indent="  ", hanging="      "))
        print(wrap("Ignored: " + ", ".join(f"{v:,.0f}" for v in dropped),
                   width, indent="  ", hanging="      "))

    # A cohort's counts never look like a two-column paste in practice, but if
    # one did, the counts analyzed would no longer be that cohort's.
    own = own_cohort(cohorts, cohort) if cohort and not dropped else None
    analyze(numbers, width, own)


if __name__ == "__main__":
    main(sys.argv[1:])
