#!/usr/bin/env python3
"""
Tests for burndown.py.

Stdlib-only (unittest), to match the rest of the repo — no dependencies to
install, just:

    python3 -m unittest test_burndown -v

The fuzz cases at the bottom run a small number of random inputs by default so
this stays a fast pre-commit check. Crank it for a longer campaign with:

    BURNDOWN_FUZZ_CASES=20000 python3 -m unittest test_burndown.FuzzPipeline
"""

import contextlib
import csv
import io
import math
import os
import random
import re
import tempfile
import types
import unittest
import unittest.mock

from pathlib import Path

import burndown as b


def chrono_to_processed(chrono: list[float]) -> list[float]:
    """Same month-over-month diff analyze() does, for building test inputs."""
    return [chrono[i] - chrono[i + 1] for i in range(len(chrono) - 1)]


# Pin a width for the assertions that read the report as text: the layout is
# terminal-dependent now, and a test that renders at whatever size the runner's
# terminal happens to be is a test that fails on someone else's machine.
WIDE = b.WIDE_TABLE
COMPACT = b.COMPACT_TABLE
STACKED = b.MIN_WIDTH


def run_analyze(entered: list[float], width: int = WIDE,
                own: "b.OwnCohort | None" = None) -> str:
    """Call analyze() and capture what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        b.analyze(entered, width, own)
    return buf.getvalue()


# The 2025-07 cohort and the 2025-08 cohort behind it, as recorded for
# 2026-01..2026-10: own-cohort counts 2,500 -> 2,200 over 9 months.
JULY = [15800.0, 14500, 13000, 11700, 11300, 10500, 10200, 10100, 9700, 9300]
AUGUST = [18300.0, 16900, 15300, 14000, 13500, 12800, 12400, 12300, 11900, 11500]
PERIODS = [f"2026-{m:02d}" for m in range(1, 11)]
JULY_COHORTS = {
    "2025-07": sorted(zip(PERIODS, JULY), reverse=True),
    "2025-08": sorted(zip(PERIODS, AUGUST), reverse=True),
}
JULY_OWN = b.own_cohort(JULY_COHORTS, "2025-07")
JULY_ENTERED = list(reversed(JULY))


class ParseNumbers(unittest.TestCase):
    def test_commas_and_whitespace(self):
        self.assertEqual(b.parse_numbers("1, 2,3  4"), [1.0, 2.0, 3.0, 4.0])

    def test_pipes_and_tabs_are_separators(self):
        self.assertEqual(b.parse_numbers("1|2\t3"), [1.0, 2.0, 3.0])

    def test_trailing_separators_ignored(self):
        self.assertEqual(b.parse_numbers("1, 2, "), [1.0, 2.0])

    def test_empty_input(self):
        self.assertEqual(b.parse_numbers("   "), [])

    def test_floats_and_negatives(self):
        self.assertEqual(b.parse_numbers("-1.5, 2.25"), [-1.5, 2.25])

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            b.parse_numbers("1, banana, 3")


class ExtractBacklog(unittest.TestCase):
    def test_two_column_paste_is_split(self):
        values, dropped, mode = b.extract_backlog(
            [1, 2600, 1, 2700, 2, 7800, 3, 9000]
        )
        self.assertEqual(values, [2600, 2700, 7800, 9000])
        self.assertEqual(dropped, [1, 1, 2, 3])
        self.assertIn("two-column", mode)

    def test_genuine_single_column_is_not_halved(self):
        # The ratio guard exists for exactly this: an ascending single column
        # where each value happens to be smaller than the next.
        data = [10200.0, 10500.0, 11300.0, 11700.0]
        values, dropped, mode = b.extract_backlog(data)
        self.assertEqual(values, data)
        self.assertEqual(dropped, [])
        self.assertEqual(mode, "single column")

    def test_ratio_guard_needs_a_wide_margin(self):
        # Every marker is smaller than its pair here, so only the 5x ratio
        # guard stops this noisy single column from being silently halved.
        data = [100.0, 500.0, 200.0, 600.0]
        self.assertEqual(b.extract_backlog(data)[0], data)

    def test_ratio_guard_boundary_pairs_at_exactly_5x(self):
        values, dropped, mode = b.extract_backlog([1.0, 5.0, 1.0, 6.0])
        self.assertEqual(values, [5.0, 6.0])
        self.assertEqual(dropped, [1.0, 1.0])
        self.assertIn("two-column", mode)

    def test_odd_length_left_alone(self):
        data = [1.0, 2600.0, 1.0]
        self.assertEqual(b.extract_backlog(data)[0], data)

    def test_too_short_left_alone(self):
        data = [1.0, 2600.0]
        self.assertEqual(b.extract_backlog(data)[0], data)

    def test_marker_not_smaller_blocks_pairing(self):
        # 9000 > 8000 breaks the all(m < v) requirement.
        data = [1.0, 2600.0, 9000.0, 8000.0]
        self.assertEqual(b.extract_backlog(data)[0], data)


class MonthlyOdds(unittest.TestCase):
    def test_fraction_of_cohort_cleared(self):
        chrono = [1000.0, 900.0, 800.0]
        odds = b.monthly_odds(chrono, chrono_to_processed(chrono))
        self.assertAlmostEqual(odds[0], 100 / 1000)
        self.assertAlmostEqual(odds[1], 100 / 900)

    def test_growing_month_is_none(self):
        chrono = [1000.0, 1200.0]
        self.assertEqual(b.monthly_odds(chrono, chrono_to_processed(chrono)), [None])

    def test_flat_month_is_none(self):
        chrono = [1000.0, 1000.0]
        self.assertEqual(b.monthly_odds(chrono, chrono_to_processed(chrono)), [None])

    def test_non_positive_start_is_none(self):
        self.assertEqual(b.monthly_odds([0.0, -50.0], [50.0]), [None])

    def test_clamped_at_one(self):
        # Queue cleared entirely (and then some) in one month.
        self.assertEqual(b.monthly_odds([100.0, -20.0], [120.0]), [1.0])

    def test_length_matches_processed(self):
        chrono = [500.0, 400.0, 450.0, 300.0]
        odds = b.monthly_odds(chrono, chrono_to_processed(chrono))
        self.assertEqual(len(odds), 3)


class DoneBy(unittest.TestCase):
    def test_linear_growth(self):
        self.assertAlmostEqual(b.done_by(1000, 100, 3), 0.3)
        self.assertAlmostEqual(b.done_by(1000, 100, 6), 0.6)

    def test_clamped_at_one(self):
        self.assertEqual(b.done_by(1000, 100, 10), 1.0)
        self.assertEqual(b.done_by(1000, 100, 50), 1.0)

    def test_zero_and_negative_rate(self):
        self.assertIsNone(b.done_by(1000, 0, 12))
        self.assertIsNone(b.done_by(1000, -50, 12))

    def test_non_positive_backlog(self):
        self.assertIsNone(b.done_by(0, 100, 12))

    def test_agrees_with_quantile_table(self):
        # The two sections are inverses of each other: the time the scenario
        # table reports for quantile q must be the time at which done_by()
        # reports probability q. If these ever drift apart, the report is
        # telling the user two different stories.
        current, rate = 10500.0, 933.0
        for q in b.QUANTILES:
            months = current * q / rate
            self.assertAlmostEqual(b.done_by(current, rate, months), q, places=9)


class Formatting(unittest.TestCase):
    def test_fmt_pct(self):
        self.assertEqual(b.fmt_pct(0.0294), "2.9%")
        self.assertEqual(b.fmt_pct(1.0), "100.0%")

    def test_fmt_months_infinite(self):
        self.assertEqual(b.fmt_months(float("inf")), "never (rate <= 0)")

    def test_fmt_months_cells_fit_the_column(self):
        # Both tables line their columns up by padding to CELL; anything wider
        # silently breaks the alignment.
        for months in (0.0, 0.5, 9.9, 99.9, 999.9, float("inf")):
            self.assertLessEqual(len(b.fmt_months(months)), b.CELL, months)

    def test_fmt_months_cells_fit_for_absurd_values(self):
        # Negative months (a negative backlog) and enormous ones (a rate barely
        # above zero) reach the formatters too, and used to overflow the cell.
        for months in (-0.5, -1234.5, 1e5, 1e12, 0.0001):
            self.assertLessEqual(len(b.fmt_months(months)), b.CELL, months)
            self.assertLessEqual(
                len(b.fmt_months_compact(months)), b.CELL_COMPACT, months
            )

    def test_quantile_header_has_a_cell_per_quantile(self):
        header = b.quantile_header("")
        self.assertEqual(
            len(header), len(b.QUANTILES) * b.CELL + (len(b.QUANTILES) - 1)
        )

    def test_compact_header_has_a_cell_per_quantile(self):
        header = b.quantile_header("", b.CELL_COMPACT)
        self.assertEqual(
            len(header),
            len(b.QUANTILES) * b.CELL_COMPACT + (len(b.QUANTILES) - 1),
        )

    def test_fmt_months_compact_cells_fit_the_column(self):
        for months in (0.0, 0.5, 9.9, 99.9, 999.9, float("inf")):
            self.assertLessEqual(
                len(b.fmt_months_compact(months)), b.CELL_COMPACT, months
            )

    def test_fmt_months_compact_keeps_both_figures(self):
        # Abbreviated, but still months *and* years — narrow terminals get the
        # same information, not less of it.
        self.assertEqual(b.fmt_months_compact(6.0), "  6.0mo 0.50y")
        self.assertEqual(b.fmt_months_compact(18.0), " 18.0mo 1.5y")
        self.assertEqual(b.fmt_months_compact(float("inf")), "never")


class Widths(unittest.TestCase):
    DATA = [9700.0, 10200, 10500, 11300, 11700, 13000, 14500, 15800]

    def test_layout_thresholds(self):
        self.assertEqual(b.layout_for(WIDE), "wide")
        self.assertEqual(b.layout_for(WIDE - 1), "compact")
        self.assertEqual(b.layout_for(COMPACT), "compact")
        self.assertEqual(b.layout_for(COMPACT - 1), "stacked")

    def test_output_width_honours_the_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"BURNDOWN_WIDTH": "72"}):
            self.assertEqual(b.output_width(), 72)
        # Junk and out-of-range values fall back to something renderable.
        with unittest.mock.patch.dict(os.environ, {"BURNDOWN_WIDTH": "banana"}):
            self.assertEqual(b.output_width(), 80)
        with unittest.mock.patch.dict(os.environ, {"BURNDOWN_WIDTH": "10"}):
            self.assertEqual(b.output_width(), b.MIN_WIDTH)
        with unittest.mock.patch.dict(os.environ, {"BURNDOWN_WIDTH": "500"}):
            self.assertEqual(b.output_width(), b.MAX_WIDTH)

    def test_nothing_overflows_at_any_width(self):
        # The whole point of the narrow layouts: no line may run past the
        # terminal, or it wraps and the columns stop lining up.
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            out = run_analyze(self.DATA, width)
            longest = max(len(line) for line in out.splitlines())
            self.assertLessEqual(longest, width, f"width={width}")

    def test_narrow_widths_are_clamped_not_broken(self):
        # Asking for 10 columns can't be honoured; it must still render.
        out = run_analyze(self.DATA, 10)
        longest = max(len(line) for line in out.splitlines())
        self.assertLessEqual(longest, b.MIN_WIDTH)

    def test_prose_stays_readable_on_wide_terminals(self):
        # Tables use the whole terminal; paragraphs stop at PROSE_WIDTH, or a
        # 117-column window turns the explanations into unreadable ribbons.
        out = run_analyze(self.DATA, b.MAX_WIDTH)
        prose = [ln for ln in out.splitlines()
                 if "random processing order" in ln or "strict FIFO" in ln]
        self.assertTrue(prose)
        for line in prose:
            self.assertLessEqual(len(line), b.PROSE_WIDTH)

    def test_exact_fit_keeps_the_roomier_form(self):
        # Off-by-one here silently downgrades a layout that actually fits.
        padded = b.stat_line("Label", "value", width=1000)
        self.assertEqual(b.stat_line("Label", "value", len(padded)), padded)
        # The look-back table's spelled-out headings need exactly 57 columns
        # for this data; at 57 they must still be chosen, at 56 abbreviated.
        self.assertIn("queue at start", run_analyze(self.DATA, 57))
        self.assertNotIn("queue at start", run_analyze(self.DATA, 56))

    def test_every_layout_keeps_every_number(self):
        # Same figures at every width — narrow reshapes the report, it doesn't
        # drop rows or columns from it.
        for width in (WIDE, COMPACT, STACKED):
            out = run_analyze(self.DATA, width)
            with self.subTest(width=width):
                for q in b.QUANTILES:
                    self.assertIn(f"{q * 100:.0f}% done", out)
                for months in ("1.8", "3.6", "5.3", "7.1"):   # fast +1 sigma
                    self.assertIn(months, out)
                for h in b.HORIZONS[1:]:
                    self.assertIn(f"{h} months", out)
                self.assertIn("next month", out)
                # One look-back row per month of throughput.
                for age in range(1, len(self.DATA)):
                    self.assertIn(f"-{age}", out)
                self.assertIn("9.0%", out)      # done_by, average pace
                self.assertIn("Median", out)
                self.assertIn("Trend", out)


class AnalyzeOutput(unittest.TestCase):
    def test_needs_two_months(self):
        self.assertIn("at least two months", run_analyze([10200.0]))

    def test_reports_last_month_odds(self):
        out = run_analyze([10200.0, 10500.0])
        # 300 of 10,500 cleared.
        self.assertIn("2.9% chance of being processed", out)

    def test_growing_queue_is_flagged(self):
        out = run_analyze([12000.0, 11000.0, 10000.0])
        self.assertIn("queue is GROWING", out)
        self.assertIn("the queue grew", out)
        self.assertIn("never", out)

    def test_flat_queue_wording(self):
        out = run_analyze([5000.0, 5000.0, 5000.0])
        self.assertIn("didn't move", out)
        self.assertNotIn("the queue grew", out)

    def test_singular_month_wording(self):
        out = run_analyze([1000.0, 900.0, 1200.0, 800.0])
        self.assertIn("Across the 1 month where", out)

    def test_plural_month_wording(self):
        out = run_analyze([800.0, 900.0, 1000.0])
        self.assertIn("Across the 2 months where", out)

    def test_last_month_marker_is_on_the_newest_row(self):
        out = run_analyze([10200.0, 10500.0, 11300.0, 11700.0])
        marked = [ln for ln in out.splitlines() if "<- last month" in ln]
        self.assertEqual(len(marked), 1, out)
        # The marker has to sit on the -1 row; on any other row it mislabels
        # which month the headline percentage came from.
        self.assertEqual(marked[0].split()[0], "-1")
        self.assertIn("10,500", marked[0])

    def test_all_horizons_present(self):
        out = run_analyze([10200.0, 10500.0, 11300.0, 11700.0])
        self.assertIn("next month", out)
        for h in b.HORIZONS[1:]:
            self.assertIn(f"{h} months", out)


    def test_default_input_is_analyzable(self):
        # main() feeds DEFAULT_INPUT back through the parser when you press
        # return, so it has to survive the same path a paste does — and it's
        # meant to be edited, so this catches a typo'd replacement too.
        numbers, dropped, mode = b.extract_backlog(
            b.parse_numbers(b.DEFAULT_INPUT)
        )
        self.assertEqual(dropped, [])
        self.assertEqual(mode, "single column")
        self.assertGreaterEqual(len(numbers), 2)
        self.assertIn("BURN-DOWN ANALYSIS", run_analyze(numbers))


def run_main(typed: str, history: Path, argv: tuple[str, ...] = (),
             **env: str) -> str:
    """Run main() with `typed` at the prompt and `history` as the watcher CSV."""
    buf = io.StringIO()
    # Blank NO_COLOR/FORCE_COLOR so the developer's own shell can't switch
    # colour on; patching COLOR restores it after main() sets it.
    env = {"BURNDOWN_WIDTH": "100", "NO_COLOR": "", "FORCE_COLOR": "", **env}
    with unittest.mock.patch("builtins.input", return_value=typed), \
            unittest.mock.patch.object(b, "HISTORY_PATH", history), \
            unittest.mock.patch.object(b, "COLOR", False), \
            unittest.mock.patch.dict(os.environ, env), \
            contextlib.redirect_stdout(buf):
        b.main(argv)
    return buf.getvalue()


HEADER = "period,cohort,people_ahead\n"


def write_history(directory: str, body: str) -> Path:
    path = Path(directory) / "flpt_history.csv"
    path.write_text(HEADER + body)
    return path


class MainDefaults(unittest.TestCase):
    """No watcher history: the prompt behaves as it always did."""

    def _run_main(self, typed: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            return run_main(typed, Path(d) / "missing.csv")

    def test_empty_line_uses_the_default(self):
        out = self._run_main("")
        self.assertIn("Using the default", out)
        expected = b.parse_numbers(b.DEFAULT_INPUT)
        self.assertIn(f"{expected[0]:,.0f}", out)
        self.assertIn(f"{len(expected):>10d}", out)

    def test_whitespace_only_counts_as_empty(self):
        self.assertIn("Using the default", self._run_main("   "))

    def test_eof_cancels(self):
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch("builtins.input", side_effect=EOFError), \
                unittest.mock.patch.object(b, "HISTORY_PATH", Path(d) / "x.csv"), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            b.main()
        self.assertIn("Cancelled.", buf.getvalue())
        self.assertNotIn("BURN-DOWN", buf.getvalue())

    def test_unreadable_input_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_main("12, abc")
        self.assertEqual(ctx.exception.code, 1)

    def test_paired_input_reports_what_was_dropped(self):
        out = self._run_main("1 2600 1 2700 2 7800")
        self.assertIn("Ignored: 1, 1, 2", out)

    def test_typed_input_wins_over_the_default(self):
        out = self._run_main("500, 900, 1400")
        self.assertNotIn("Using the default", out)
        self.assertIn("500", out)


class LoadCohorts(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(b.load_cohorts(Path(d) / "nope.csv"), {})

    def test_newest_first_and_last_revision_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, "2026-08,2025-07,200\n"
                                    "2026-09,2025-07,150\n"
                                    "2026-07,2025-07,300\n"
                                    "2026-09,2025-07,140\n"
                                    "2026-09,2025-08,999\n")
            got = b.load_cohorts(path)
        self.assertEqual(got["2025-07"],
                         [("2026-09", 140.0), ("2026-08", 200.0), ("2026-07", 300.0)])
        self.assertEqual(got["2025-08"], [("2026-09", 999.0)])

    def test_rows_without_a_count_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, "2026-09,2025-07,\n2026-08,2025-07,abc\n"
                                    "2026-07,2025-07,5\n")
            self.assertEqual(b.load_cohorts(path), {"2025-07": [("2026-07", 5.0)]})

    def test_hand_edits_are_normalised(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, '2026-9,2025-7,9700\n'
                                    '2026-10,2025-07," 9,000 "\n'
                                    '2026/11,2025/07,8500\n')
            got = b.load_cohorts(path)
        self.assertEqual(got, {"2025-07": [("2026-11", 8500.0), ("2026-10", 9000.0),
                                           ("2026-09", 9700.0)]})

    def test_zero_is_a_count(self):
        # A cohort that has fully cleared is the one figure that matters most.
        self.assertEqual(b.history_count("0"), 0.0)
        self.assertEqual(b.history_count("0.5"), 0.5)
        self.assertIsNone(b.history_count("-0.5"))

    def test_mangled_rows_are_rejected_by_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, "2026-09,2025-07,9700\n"   # line 2
                                    "Sept,2025-07,9600\n"      # 3
                                    "2026-13,2025-07,9500\n"   # 4
                                    "2026-08,July,9400\n"      # 5
                                    "2026-07,2025-07,nan\n"    # 6
                                    "2026-06,2025-07,inf\n"    # 7
                                    "2026-05,2025-07,-5\n"     # 8
                                    "2026-04,2025-07,abc\n"    # 9
                                    "garbage,2025-07,\n"       # 10: blank count, quiet
                                    "2026-08,2025-07,10100\n")
            rejected: list[int] = []
            got = b.load_cohorts(path, rejected)
        self.assertEqual(got, {"2025-07": [("2026-09", 9700.0), ("2026-08", 10100.0)]})
        self.assertEqual(rejected, [3, 4, 5, 6, 7, 8, 9])

    def test_missing_column_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.csv"
            path.write_text("period,cohort\n2026-09,2025-07\n")
            self.assertEqual(b.load_cohorts(path), {})


class MonthKey(unittest.TestCase):
    def test_accepted(self):
        for raw, want in (("2026-09", "2026-09"), ("2026-9", "2026-09"),
                          (" 2026/12 ", "2026-12"), ("2026-01", "2026-01")):
            self.assertEqual(b.month_key(raw), want, raw)

    def test_rejected(self):
        for raw in ("", None, "Sept", "2026-13", "2026-0", "26-09", "2026-009",
                    "2026-09-01", "202a-09"):
            self.assertIsNone(b.month_key(raw), raw)


class CohortHelpers(unittest.TestCase):
    def test_missing_months_across_a_year_boundary(self):
        self.assertEqual(b.missing_months(["2026-02", "2025-11", "2025-12"]),
                         ["2026-01"])

    def test_no_gaps(self):
        self.assertEqual(b.missing_months(["2026-09", "2026-08"]), [])

    def test_menu_needs_two_months(self):
        cohorts = {"a": [("2026-09", 1)], "b": [("2026-09", 1), ("2026-08", 2)]}
        self.assertEqual(b.cohort_menu(cohorts), ["b"])

    def test_pick_by_number_name_or_slash(self):
        cohorts = {"2025-07": [], "2025-08": []}
        menu = ["2025-07", "2025-08"]
        self.assertEqual(b.pick_cohort("1", cohorts, menu), "2025-07")
        self.assertEqual(b.pick_cohort("2", cohorts, menu), "2025-08")
        self.assertEqual(b.pick_cohort(" 2025-07 ", cohorts, menu), "2025-07")
        self.assertEqual(b.pick_cohort("2025/08", cohorts, menu), "2025-08")

    def test_pick_falls_through_to_numbers(self):
        cohorts = {"2025-07": []}
        menu = ["2025-07"]
        for raw in ("0", "2", "9700, 10200", "2025-09", ""):
            self.assertIsNone(b.pick_cohort(raw, cohorts, menu), raw)

    def test_describe_singular(self):
        self.assertIn("1 month of data",
                      b.describe_cohort("x", [("2026-09", 5.0)]))


class OwnCohortRates(unittest.TestCase):
    def test_month_index_round_trips_across_years(self):
        for i in range(2019 * 12, 2031 * 12):
            self.assertEqual(b.month_index(b.month_label(i)), i)
        self.assertEqual(b.month_index("2026-01") - b.month_index("2025-12"), 1)

    def test_waiting_is_the_gap_to_the_next_cohort(self):
        self.assertEqual(JULY_OWN.waiting["2026-01"], 2500)
        self.assertEqual(JULY_OWN.waiting["2026-10"], 2200)
        self.assertEqual(JULY_OWN.ahead["2026-10"], 9300)

    def test_only_months_both_cohorts_have(self):
        cohorts = {"2025-12": [("2026-02", 10.0), ("2026-01", 20.0)],
                   "2026-01": [("2026-02", 50.0), ("2025-12", 70.0)]}
        own = b.own_cohort(cohorts, "2025-12")   # next cohort across a year end
        self.assertEqual(own.waiting, {"2026-02": 40.0})
        self.assertEqual(b.own_cohort(cohorts, "2026-01").waiting, {})
        self.assertEqual(b.own_cohort({}, "2026-01"), b.OwnCohort("2026-01", {}, {}))

    def test_shrink_rate(self):
        self.assertAlmostEqual(b.shrink_rate(2500, 2200, 9), 0.01410, places=5)
        self.assertAlmostEqual(b.shrink_rate(100, 50, 1), 0.5)
        self.assertEqual(b.shrink_rate(100, 0, 3), 1.0)
        self.assertEqual(b.shrink_rate(100, 100, 3), 0.0)
        self.assertEqual(b.shrink_rate(1, 0, 2), 1.0)   # a start of 1 is measurable
        self.assertLess(b.shrink_rate(100, 200, 1), 0)
        for args in ((0, 10, 1), (-5, 10, 1), (10, -1, 1), (10, 5, 0)):
            self.assertIsNone(b.shrink_rate(*args), args)

    def test_windows_shortest_first_full_span_last(self):
        rows = b.own_windows(JULY_OWN)
        self.assertEqual([n for n, _, _ in rows], [3, 6, 9])
        rates = {n: (round(mine, 4), round(theirs, 4)) for n, mine, theirs in rows}
        self.assertEqual(rates[3], (0.0, round(1 - (9300 / 10200) ** (1 / 3), 4)))
        self.assertEqual(rates[6][0], round(1 - (2200 / 2300) ** (1 / 6), 4))
        self.assertEqual(rates[9], (0.0141, 0.0572))

    def test_short_span_gives_only_the_full_window(self):
        for span in (1, 3):
            own = b.OwnCohort("x", {"2026-01": 100.0, b.month_label(
                b.month_index("2026-01") + span): 90.0}, {})
            own = own._replace(ahead={p: 1000.0 for p in own.waiting})
            self.assertEqual([n for n, _, _ in b.own_windows(own)], [span])

    def test_window_with_a_missing_start_month_is_skipped(self):
        waiting = {p: 100.0 for p in PERIODS if p != "2026-07"}
        own = b.OwnCohort("x", waiting, dict(waiting))
        self.assertEqual([n for n, _, _ in b.own_windows(own)], [6, 9])

    def test_too_little_data_has_no_windows(self):
        self.assertEqual(b.own_windows(b.OwnCohort("x", {}, {})), [])
        self.assertEqual(b.own_windows(b.OwnCohort("x", {"2026-01": 5.0},
                                                   {"2026-01": 9.0})), [])

    def test_rate_cell(self):
        self.assertEqual(b.rate_cell(None), ("n/a", ()))
        self.assertEqual(b.rate_cell(-0.2), ("grew", ("yellow",)))
        self.assertEqual(b.rate_cell(0.0141), ("1.4%", ()))

    def test_report_section(self):
        out = run_analyze(JULY_ENTERED, own=JULY_OWN)
        self.assertIn("Your own cohort (2025-07)", out)
        self.assertIn("2,500 in 2026-01 -> 2,200 in 2026-10", out)
        row = [ln.split() for ln in out.splitlines() if "last 9 months" in ln]
        self.assertEqual(row, [["last", "9", "months", "1.4%", "5.7%"]])
        # It sits between the look-back summary and the look-ahead.
        self.assertLess(out.index("Across the 9 months"), out.index("Your own"))
        self.assertLess(out.index("Your own"), out.index("Looking ahead"))

    def test_absent_without_a_cohort(self):
        self.assertNotIn("Your own cohort", run_analyze(JULY_ENTERED))

    def test_fallback_note_without_the_next_cohort(self):
        own = b.own_cohort({"2025-07": JULY_COHORTS["2025-07"]}, "2025-07")
        out = run_analyze(JULY_ENTERED, own=own)
        self.assertIn("No 2025-08 figures for the same months", out)
        self.assertNotIn("Still waiting", out)

    def test_fits_and_strips_cleanly_at_every_width(self):
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                out = run_analyze(JULY_ENTERED, width, JULY_OWN)
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                with unittest.mock.patch.object(b, "COLOR", True):
                    colored = run_analyze(JULY_ENTERED, width, JULY_OWN)
                self.assertEqual(ANSI.sub("", colored), out)
                rates = ("0.0%", "3.0%", "0.7%", "3.8%", "1.4%", "5.7%")
                section = out[out.index("Your own"):out.index("Looking ahead")]
                for rate in rates:
                    self.assertIn(rate, section)

    # The tests below pin the block's exact text; they were added to kill the
    # mutants (text, alignment, padding) the looser assertions above let by.
    # 13 months, so the windows are 3, 6 and 12: labels of different lengths.
    PERIODS13 = [b.month_label(b.month_index("2025-10") + k) for k in range(13)]
    OWN13 = b.OwnCohort("2025-07", dict(zip(PERIODS13, [
        2600.0, 2550, 2500, 2500, 2450, 2400, 2400, 2350, 2300, 2200, 2250, 2250,
        2300])), dict(zip(PERIODS13, [
            20000.0, 19000, 18000, 17000, 16000, 15000, 14000, 13000, 12000, 11000,
            10000, 9500, 9000])))

    def own_block(self, own: "b.OwnCohort", width: int) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_own_cohort(own, width)
        return buf.getvalue()

    def test_block_text_wide(self):
        self.assertEqual(self.own_block(self.OWN13, WIDE), """\
  Your own cohort (2025-07)
    Everyone from 2025-07 still waiting is 2025-08's people ahead minus yours.
    If files within a month are pulled in random order, the share of them
    cleared each month is your chance, however much older files are put first.
    Still waiting:  2,600 in 2025-10 -> 2,300 in 2026-10
    per month       your cohort  people ahead
    last 3 months          grew          6.5%
    last 6 months          0.7%          7.1%
    last 12 months         1.0%          6.4%
    IRCC rounds these counts to the nearest 100, so a single month of one
    cohort is mostly noise; the longer windows are steadier.
""")

    def test_block_text_narrow(self):
        self.assertEqual(self.own_block(self.OWN13, STACKED), """\
  Your own cohort (2025-07)
    Everyone from 2025-07 still waiting
    is 2025-08's people ahead minus
    yours. If files within a month are
    pulled in random order, the share of
    them cleared each month is your
    chance, however much older files are
    put first.
    Still waiting: 2,600 in 2025-10 ->
      2,300 in 2026-10
    /mo    cohort   ahead
    3 mo     grew    6.5%
    6 mo     0.7%    7.1%
    12 mo    1.0%    6.4%
    IRCC rounds these counts to the
    nearest 100, so a single month of
    one cohort is mostly noise; the
    longer windows are steadier.
""")

    def test_fallback_note_text(self):
        self.assertEqual(self.own_block(b.OwnCohort("2025-07", {}, {}), WIDE), """\
  Your own cohort (2025-07)
    No 2025-08 figures for the same months, so there's no own-cohort rate; the
    chances above count everyone ahead of you.
""")

    def test_spelled_out_headings_exactly_when_they_fit(self):
        # "    last 12 months  your cohort  people ahead" is 45 wide.
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                out = self.own_block(self.OWN13, width)
                self.assertEqual("your cohort" in out, width >= 45)

    def test_two_month_window_is_plural(self):
        own = b.OwnCohort("2025-12", {"2026-01": 100.0, "2026-03": 90.0},
                          {"2026-01": 1000.0, "2026-03": 900.0})
        self.assertIn("    last 2 months  ", self.own_block(own, WIDE))


SAMPLE = ("2026-07,2025-07,10500\n2026-08,2025-07,10200\n2026-09,2025-07,9700\n"
          "2026-09,2025-08,11900\n2026-08,2025-08,12500\n"
          "2026-09,2025-09,14400\n")


class MainWithHistory(unittest.TestCase):
    def _run(self, typed: str, body: str = SAMPLE) -> str:
        with tempfile.TemporaryDirectory() as d:
            return run_main(typed, write_history(d, body))

    def test_menu_lists_cohorts_with_months_of_data(self):
        out = self._run("")
        self.assertIn("1) 2025-07  3 months of data (2026-07 to 2026-09)", out)
        self.assertIn("2) 2025-08  2 months of data (2026-08 to 2026-09)", out)
        self.assertIn("1 more with only one month", out)
        self.assertNotIn("1) 2025-09", out)

    def test_empty_line_uses_the_default_cohort(self):
        out = self._run("")
        self.assertIn("Using cohort 2025-07", out)
        self.assertNotIn("Using the default", out)
        self.assertIn("10,500 -> 10,200 -> 9,700", out)

    def test_pick_by_number(self):
        out = self._run("2")
        self.assertIn("Using cohort 2025-08", out)
        self.assertIn("12,500 -> 11,900", out)

    def test_pasted_numbers_still_work(self):
        out = self._run("500, 900, 1400")
        self.assertNotIn("Using cohort", out)
        self.assertIn("1,400 -> 900 -> 500", out)
        self.assertNotIn("Your own cohort", out)

    def test_cohort_pick_adds_its_own_rates(self):
        out = self._run("2025-07")
        # 2025-08 has 2026-08 and 2026-09: 2,300 -> 2,200 of 2025-07 waiting.
        self.assertIn("2,300 in 2026-08 -> 2,200 in 2026-09", out)
        row = [ln.split() for ln in out.splitlines()
               if ln.strip().startswith("last month")]
        self.assertEqual(row, [["last", "month", "4.3%", "4.9%"]])

    def test_cohort_without_a_later_one_falls_back(self):
        out = self._run("2")   # 2025-08; 2025-09 has a single month
        self.assertIn("No 2025-09 figures for the same months", out)

    def test_gap_is_flagged(self):
        out = self._run("2025-07", "2026-09,2025-07,9700\n2026-07,2025-07,10500\n")
        self.assertIn("No data for 2026-08", out)

    def test_default_cohort_absent_falls_back_to_default_input(self):
        out = self._run("", "2026-09,2025-08,11900\n2026-08,2025-08,12500\n")
        self.assertIn("Using the default", out)

    def test_rejected_rows_are_flagged(self):
        out = self._run("", SAMPLE + "Sept,2025-07,9000\n2026-06,2025-07,nan\n")
        self.assertIn("Skipped unreadable rows in flpt_history.csv (lines 8, 9)", out)
        self.assertIn("Using cohort 2025-07", out)
        self.assertNotIn("Skipped", self._run(""))
        self.assertIn("(line 8).", self._run("", SAMPLE + "Sept,2025-07,9000\n"))

    def test_paired_looking_cohort_drops_its_own_rates(self):
        # Newest first that's 10, 5000, 20, 6000: a two-column paste to
        # extract_backlog, so what's analyzed is no longer the cohort's series.
        body = "".join(f"{p},{c},{n}\n" for c, counts in
                       (("2025-07", (6000, 20, 5000, 10)),
                        ("2025-08", (9000, 8000, 7000, 6000)))
                       for p, n in zip(("2026-06", "2026-07", "2026-08", "2026-09"),
                                       counts))
        out = self._run("2025-07", body)
        self.assertIn("Input mode: two-column", out)
        self.assertNotIn("Your own cohort", out)

    def test_rejected_rows_warning_wraps_and_is_yellow(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.warn_rejected([3, 14, 15, 92, 65, 358, 979], STACKED)
        self.assertEqual(buf.getvalue(), "⚠  Skipped unreadable rows in\n"
                                         "    flpt_history.csv (lines 3, 14, 15,\n"
                                         "    92, 65, 358, 979).\n")
        with unittest.mock.patch.object(b, "COLOR", True), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            b.warn_rejected([3], WIDE)
        self.assertTrue(buf.getvalue().startswith("\033[33m⚠  Skipped"))

    def test_no_analyzable_cohort_keeps_the_plain_prompt(self):
        out = self._run("", "2026-09,2025-08,11900\n")
        self.assertNotIn("Cohorts tracked", out)
        self.assertIn("Using the default", out)


ANSI = re.compile(r"\033\[[0-9;]*m")


def run_summary(cohorts: dict, width: int = WIDE) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        b.print_summary(cohorts, width)
    return buf.getvalue()


class Summary(unittest.TestCase):
    COHORTS = {
        "2024-12": [("2026-02", 900.0), ("2026-01", 1000.0)],   # before 2025-01
        **JULY_COHORTS,
        "2025-09": [("2026-10", 14000.0)],                      # one month only
        "2026-01": [("2026-10", 500.0), ("2026-09", 500.0)],    # stalled
    }

    def test_rows_from_2025_01_with_two_months(self):
        rows = b.summary_rows(self.COHORTS)
        self.assertEqual([r[0] for r in rows], ["2025-07", "2025-08", "2026-01"])
        cohort, current, cleared, recent, median = rows[0]
        self.assertEqual((current, cleared, recent, median), (9300, 0, 300, 400))
        self.assertEqual([r[2] for r in rows], [0, None, None])
        self.assertEqual([r[0] for r in b.summary_rows(self.COHORTS, "2025-08")],
                         ["2025-08", "2026-01"])

    def test_matches_the_full_analysis(self):
        # Same Recent/Median rows analyze() prints for 2025-07: 15.5 / 31.0
        # months at the 3-month pace, 11.6 / 23.2 at the median.
        out = run_summary(self.COHORTS)
        row = [ln.split() for ln in out.splitlines() if ln.startswith("  2025-07")]
        self.assertEqual(row, [["2025-07", "9,300", "0", "300", "15.5", "31.0",
                                "400", "11.6", "23.2"]])
        full = run_analyze(JULY_ENTERED)
        recent_row = next(ln for ln in full.splitlines() if "Recent (3 mo pace)" in ln)
        median_row = next(ln for ln in full.splitlines() if ln.strip().startswith("Median "))
        for months in ("15.5 mo", "31.0 mo"):
            self.assertIn(months, recent_row)
        for months in ("11.6 mo", "23.2 mo"):
            self.assertIn(months, median_row)

    def test_recent_pace(self):
        self.assertEqual(b.recent_pace([100.0, 500, 100, 400, 400]), (300, 3))
        # Fewer months than RECENT_MONTHS: average what there is.
        self.assertEqual(b.recent_pace([100.0, 500]), (300, 2))
        self.assertEqual(b.recent_pace([-200.0]), (-200, 1))

    def test_pace_labels_sit_over_their_columns(self):
        lines = run_summary(self.COHORTS).splitlines()
        at = next(i for i, ln in enumerate(lines) if ln.startswith("  cohort"))
        labels, heads = lines[at - 1], lines[at]
        groups = [m.start() for m in re.finditer("pace/mo", heads)]
        spans = [(groups[0], groups[1] - 3), (groups[1], len(heads))]
        for name, (start, end) in zip(b.SUMMARY_PACES, spans):
            middle = labels.index(name) + len(name) / 2
            self.assertLessEqual(abs(middle - (start + end) / 2), 1, name)

    def test_stalled_cohort_never_clears(self):
        out = run_summary(self.COHORTS)
        row = [ln.split() for ln in out.splitlines() if ln.startswith("  2026-01")]
        self.assertEqual(row[0][1:], ["500", "n/a", "0", "never", "never",
                                      "0", "never", "never"])

    def test_stacked_when_the_table_wont_fit(self):
        out = run_summary(self.COHORTS, STACKED)
        lines = out.splitlines()
        at = lines.index("  2025-07  9,300 ahead, 0 cleared")
        self.assertEqual(lines[at + 1].split(), ["recent", "300", "15.5", "31.0"])
        self.assertEqual(lines[at + 2].split(), ["median", "400", "11.6", "23.2"])
        self.assertIn("  2025-08  11,500 ahead, n/a cleared", lines)

    def test_fits_and_strips_cleanly_at_every_width(self):
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                out = run_summary(self.COHORTS, width)
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                self.assertFalse([ln for ln in out.splitlines() if ln != ln.rstrip()])
                with unittest.mock.patch.object(b, "COLOR", True):
                    colored = run_summary(self.COHORTS, width)
                self.assertEqual(ANSI.sub("", colored), out)
                for figure in ("300", "31.0", "400", "23.2", "never", "n/a",
                               "pace/mo"):
                    self.assertIn(figure, out)

    def test_default_cohort_and_never_are_painted(self):
        with unittest.mock.patch.object(b, "COLOR", True):
            out = run_summary(self.COHORTS)
        july = next(ln for ln in out.splitlines() if "2025-07" in ln)
        self.assertTrue(july.startswith("\033[1m"))
        self.assertRegex(out, "\033\\[31m *never")

    def test_own_last_month(self):
        self.assertEqual(b.own_last_month(JULY_COHORTS, "2025-07"), 0)
        # Give 2025-08 a 2025-09 behind it: 2025-08's own still-waiting count
        # goes 14,400-11,900 = 2,500 in September to 13,900-11,500 = 2,400.
        cohorts = {**JULY_COHORTS, "2025-09": [("2026-10", 13900.0),
                                               ("2026-09", 14400.0),
                                               ("2026-08", 14200.0)]}
        self.assertEqual(b.own_last_month(cohorts, "2025-08"),
                         (14400 - 11900) - (13900 - 11500))

    def test_own_last_month_needs_the_latest_two_months(self):
        july = JULY_COHORTS["2025-07"]
        cases = {
            "no next cohort": {"2025-07": july},
            "next lacks the latest month": {"2025-07": july, "2025-08": [
                (p, n) for p, n in JULY_COHORTS["2025-08"] if p != "2026-10"]},
            "next lacks the month before": {"2025-07": july, "2025-08": [
                (p, n) for p, n in JULY_COHORTS["2025-08"] if p != "2026-09"]},
        }
        for name, cohorts in cases.items():
            self.assertIsNone(b.own_last_month(cohorts, "2025-07"), name)

    def test_negative_cleared_is_shown(self):
        # The count grew (rounding, or IRCC revising): show it, don't hide it.
        cohorts = {"2025-07": JULY_COHORTS["2025-07"],
                   "2025-08": [("2026-10", 11600.0), ("2026-09", 11900.0)]}
        row = b.summary_rows(cohorts)[0]
        self.assertEqual(row[2], (11900 - 9700) - (11600 - 9300))
        self.assertIn(" -100 ", run_summary(cohorts))

    def test_nothing_to_summarize(self):
        out = run_summary({"2024-06": JULY_COHORTS["2025-07"]})
        self.assertIn("No cohort from 2025-01 on has two months", out)

    def test_gaps_are_flagged(self):
        out = run_summary({"2025-03": [("2026-09", 900.0), ("2026-07", 1000.0)]})
        self.assertIn("Months missing for 2025-03", out)
        self.assertNotIn("Months missing", run_summary(self.COHORTS))

    def test_time_to_clear(self):
        self.assertEqual(b.time_to_clear(9300, 722, 0.5), 9300 * 0.5 / 722)
        # Any positive pace clears eventually, however slow; none never does.
        self.assertEqual(b.time_to_clear(10, 0.1), 100)
        for rate in (0, -5):
            self.assertEqual(b.time_to_clear(10, rate), float("inf"))

    def test_cells_stay_inside_their_column(self):
        for value in (0, 7, 9999999, 10000000, -9999999, 1e300, -1e300, 0.4):
            self.assertLessEqual(len(b.fmt_count(value)), b.SUMMARY_CELL, value)
        for months in (0, 1.25, 99999.9, 9999.94, 1e7, 1e300, float("inf")):
            self.assertLessEqual(len(b.fmt_months_short(months)), b.SUMMARY_CELL,
                                 months)
        self.assertEqual(b.fmt_count(9300), "9,300")
        self.assertEqual(b.fmt_count(999999), "999,999")   # exactly SUMMARY_CELL
        self.assertEqual(b.fmt_months_short(12.94), "12.9")
        # Each fallback kicks in only once the roomier form overflows.
        self.assertEqual(b.fmt_months_short(99999.94), "99999.9")
        self.assertEqual(b.fmt_months_short(99999.96), "100,000")
        self.assertEqual(b.fmt_months_short(1234567), "1e+06")
        self.assertEqual(b.fmt_months_short(float("inf")), "never")

    # The tests below pin the summary's exact text and widths; they were added
    # to kill the mutants (text, alignment, padding) the looser ones let by.
    def test_table_text(self):
        self.assertEqual(run_summary(self.COHORTS), """
============================================================
COHORT SUMMARY
============================================================
Months until 50% and 100% of the people ahead of each cohort from 2025-01 have
cleared, at its recent pace (the average of its last 3 months) and its median
monthly pace. Cleared is how many of the cohort's own applicants were
processed last month (the gap to the next cohort, so rounded to the nearest
100; n/a without it).

                                 recent (3 mo)              median
  cohort    ahead  cleared   pace/mo    50%   100%   pace/mo    50%   100%
  2025-07   9,300        0       300   15.5   31.0       400   11.6   23.2
  2025-08  11,500      n/a       300   19.2   38.3       500   11.5   23.0
  2026-01     500      n/a         0  never  never         0  never  never
""" + "=" * WIDE + "\n\n")

    def test_stacked_text(self):
        out = run_summary(self.COHORTS, STACKED)
        self.assertEqual(out[out.index("\n\n") + 2:], """\
            pace/mo    50%   100%
  2025-07  9,300 ahead, 0 cleared
    recent      300   15.5   31.0
    median      400   11.6   23.2
  2025-08  11,500 ahead, n/a cleared
    recent      300   19.2   38.3
    median      500   11.5   23.0
  2026-01  500 ahead, n/a cleared
    recent        0  never  never
    median        0  never  never
""" + "=" * STACKED + "\n\n")

    def test_stacked_cohort_line_wraps_under_itself(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_summary_stacked(
                [("2025-07", "999,999", "999,999", ("1", "2", "3"), ("4", "5", "6"))],
                b.SummaryWidths(7, 5), STACKED)
        self.assertEqual(buf.getvalue().splitlines()[1:3],
                         ["  2025-07  999,999 ahead, 999,999", "    cleared"])

    def test_gaps_and_nothing_to_summarize_text(self):
        gappy = {"2025-03": [("2026-09", 900.0), ("2026-07", 1000.0)],
                 "2025-04": [("2026-09", 800.0), ("2026-06", 900.0)]}
        out = run_summary(gappy, 60)
        self.assertEqual(out[out.index("\n\n⚠"):], """

⚠  Months missing for 2025-03, 2025-04: their rates treat
    the recorded months as consecutive.
""" + "=" * 60 + "\n\n")
        self.assertEqual(run_summary({}, 60), """
============================================================
COHORT SUMMARY
============================================================
No cohort from 2025-01 on has two months of data in
flpt_history.csv yet.
""")
        with unittest.mock.patch.object(b, "COLOR", True):
            self.assertIn("\033[33m⚠  Months missing", run_summary(gappy, 60))

    def test_table_width_is_what_it_renders(self):
        cells, w = b.summary_layout(b.summary_rows(self.COHORTS))
        lines = run_summary(self.COHORTS).splitlines()
        table = lines[lines.index(next(ln for ln in lines if ln.startswith("  cohort"))):-2]
        self.assertEqual({len(ln) for ln in table}, {w.table()})
        # ...and the table is used down to exactly that width.
        self.assertIn("  cohort", run_summary(self.COHORTS, w.table()))
        self.assertNotIn("  cohort", run_summary(self.COHORTS, w.table() - 1))

    def test_layout_widths(self):
        def widths(current, recent, median):
            return b.summary_layout([("2025-07", current, None, recent, median)])[1]
        # Floors: the "ahead" heading and "never" (wider than "100%").
        self.assertEqual(widths(500.0, 500.0, 250.0), b.SummaryWidths(5, 5))
        # The pace cell has its own column; it doesn't widen the time cells.
        self.assertEqual(widths(1000.0, 999999.0, 999999.0), b.SummaryWidths(5, 5))
        # The 50% time can be the wider one: "617,284" beside "1e+06".
        self.assertEqual(widths(1234567.0, 1.0, 1.0), b.SummaryWidths(5, 7))


class MainSummarize(unittest.TestCase):
    def test_summarizes_without_prompting(self):
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch("builtins.input") as typed:
            body = "".join(f"{p},{c},{n:.0f}\n"
                           for c, series in JULY_COHORTS.items() for p, n in series)
            out = run_main("", write_history(d, body + "Sept,2025-07,1\n"),
                           ("--summarize",))
        self.assertIn("COHORT SUMMARY", out)
        self.assertIn("2025-08", out)
        self.assertIn("Skipped unreadable rows", out)
        self.assertNotIn("BURN-DOWN ANALYSIS", out)
        self.assertNotIn("Pick a cohort", out)

    def test_help_describes_the_flag(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf, \
                self.assertRaises(SystemExit):
            b.parse_args(["--help"])
        text = " ".join(buf.getvalue().split())
        self.assertRegex(text, r"(^| )Estimate how long the queue ahead of you "
                               r"will take to clear\.( |$)")
        self.assertIn("--summarize no prompt: recent and median times to 50% and "
                      "100% for every cohort from 2025-01 in flpt_history.csv",
                      text)

    def test_unknown_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            run_main("", Path(d) / "none.csv", ("--sumarize",))


def run_fifo(cohorts: dict, width: int = WIDE, target: str = b.DEFAULT_COHORT) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        b.print_fifo(cohorts, width, target)
    return buf.getvalue()


# 2025-05 through 2025-08 as recorded for 2026-01..2026-10. 2025-08 has no
# 2025-09 behind it, so 2025-05..2025-07 are the settled cohorts.
FIFO_COHORTS = dict(sorted({
    **JULY_COHORTS,
    "2025-05": sorted(zip(PERIODS, [12000.0, 10700, 9300, 8100, 7700, 7200,
                                    7000, 6900, 6700, 6300]), reverse=True),
    "2025-06": sorted(zip(PERIODS, [13900.0, 12500, 11100, 9900, 9500, 8800,
                                    8600, 8400, 8100, 7800]), reverse=True),
}.items()))


# The same cohorts with nothing moving: every count stays at its January figure.
FLAT_COHORTS = {c: [(p, s[-1][1]) for p, _ in s] for c, s in FIFO_COHORTS.items()}


class Fifo(unittest.TestCase):
    def test_settled_cohorts(self):
        self.assertEqual(b.settled_cohorts(FIFO_COHORTS),
                         ["2025-05", "2025-06", "2025-07"])
        self.assertEqual(b.settled_cohorts(FIFO_COHORTS, "2025-06"),
                         ["2025-06", "2025-07"])
        self.assertEqual(b.settled_cohorts({}), [])
        # Exactly FIFO_SETTLED months before the latest period counts; one
        # month newer doesn't.
        late = {"2025-10": [("2026-10", 5.0)], "2025-11": [("2026-10", 9.0)],
                "2025-12": [("2026-10", 12.0)]}
        self.assertEqual(b.settled_cohorts(late), ["2025-10"])

    def test_window_rate(self):
        series = {"2026-04": 2300.0, "2026-10": 2200.0}
        self.assertAlmostEqual(b.window_rate(series, "2026-10", 6),
                               1 - (2200 / 2300) ** (1 / 6))
        self.assertIsNone(b.window_rate(series, "2026-10", 3))
        self.assertIsNone(b.window_rate(series, "2026-11", 7))

    def test_cohort_rates(self):
        rows = b.cohort_rates(FIFO_COHORTS, ["2025-05", "2025-06"], "2026-10")
        self.assertEqual([(r.label, r.waiting) for r in rows],
                         [("earlier", 6300), ("2025-05", 1500), ("2025-06", 1500)])
        self.assertAlmostEqual(rows[0].rates[1], 1 - (6300 / 8100) ** (1 / 6))
        self.assertAlmostEqual(rows[1].rates[0], 1 - (1500 / 1600) ** (1 / 3))
        self.assertEqual(len(rows[2].rates), len(b.OWN_WINDOWS))
        gap = b.cohort_rates(FIFO_COHORTS, ["2025-05"], "2026-11")
        self.assertEqual(gap[0], b.CohortRates("earlier", None, (None, None)))

    def test_background_rate_is_the_median_without_the_tail(self):
        rows = [b.CohortRates("earlier", 1.0, (0.9, 0.9)),
                b.CohortRates("2025-01", 1.0, (0.5, None)),
                b.CohortRates("2025-02", 1.0, (0.01, None)),
                b.CohortRates("2025-03", 1.0, (0.02, None))]
        self.assertEqual(b.background_rate(rows, 0), 0.02)
        self.assertIsNone(b.background_rate(rows, 1))
        self.assertIsNone(b.background_rate(rows[:1], 0))

    def test_order_score(self):
        fifo = b.OrderScore(6, 800.0, 400.0, 200.0, 0.0)
        self.assertEqual((fifo.observed(), fifo.random(), fifo.score()), (1, 0.8, 1))
        rand = b.OrderScore(6, 800.0, 80.0, 200.0, 20.0)
        self.assertAlmostEqual(rand.score(), 0)
        newest = b.OrderScore(6, 500.0, 0.0, 500.0, 50.0)
        self.assertEqual((newest.observed(), newest.score()), (0, -1))
        self.assertIsNone(b.OrderScore(6, 800.0, 0.0, 200.0, 0.0).score())
        self.assertIsNone(b.OrderScore(6, 800.0, 0.0, 200.0, 0.0).observed())
        self.assertIsNone(b.OrderScore(6, 800.0, 10.0, 0.0, 0.0).score())
        # Net growth on one side counts against the other.
        grew = b.OrderScore(6, 800.0, 300.0, 200.0, -100.0)
        self.assertEqual(grew.observed(), 1.5)

    def test_order_score_from_history(self):
        s = b.order_score(FIFO_COHORTS, "2025-07", ["2025-07"], "2026-10", 6)
        self.assertEqual(s, b.OrderScore(6, 11700, 2400, 2300, 100))
        self.assertAlmostEqual(s.score(), (2400 / 2500 - 11700 / 14000)
                               / (1 - 11700 / 14000))
        two = b.order_score(FIFO_COHORTS, "2025-06", ["2025-06", "2025-07"],
                            "2026-10", 3)
        self.assertEqual(two, b.OrderScore(3, 8600, 800, 1600 + 2200, 100))
        for args in ((["2025-07"], "2026-10", 12),     # starts before the data
                     ([], "2026-10", 3),
                     (["2025-08"], "2026-10", 3)):     # no own count
            self.assertIsNone(b.order_score(FIFO_COHORTS, "2025-07", *args), args)
        empty = {"2025-07": [("2026-10", 0.0), ("2026-07", 0.0)],
                 "2025-08": [("2026-10", 0.0), ("2026-07", 0.0)]}
        self.assertIsNone(b.order_score(empty, "2025-07", ["2025-07"], "2026-10", 3))

    def test_describe_score(self):
        cases = {None: "nothing cleared", 1.0: "close to oldest-first",
                 0.75: "close to oldest-first",
                 0.7499: "between random and oldest-first",
                 0.25: "between random and oldest-first",
                 0.2499: "close to random", -0.2499: "close to random",
                 -0.25: "newer files first"}
        for score, words in cases.items():
            self.assertEqual(b.describe_score(score), words, score)

    def test_sweep_chance(self):
        # Background alone compounds.
        self.assertAlmostEqual(b.sweep_chance(10, 0.02, 9300, 2200, 0),
                               1 - 0.98 ** 10)
        self.assertEqual(b.sweep_chance(0, 0.02, 9300, 2200, 300), 0)
        # Sweep alone: arrives after 7,000 * 0.7 / 700 = 7 months, then 700 a
        # month of 1,400 waiting, capped once STRAGGLERS are left.
        for months, chance in ((7, 0), (8, 0.5), (8.5, 0.7), (100, 0.7)):
            self.assertAlmostEqual(b.sweep_chance(months, 0, 7000, 1400, 700),
                                   chance, msg=months)
        # A shrinking queue's negative background is no help, and no harm.
        self.assertEqual(b.sweep_chance(12, -0.05, 7000, 1400, 0), 0)
        self.assertEqual(b.sweep_chance(12, 0, 7000, 0, 700), 0)
        self.assertAlmostEqual(b.sweep_chance(1, 0, -5, 700, 700), 0.7)

    def test_months_to_chance(self):
        self.assertAlmostEqual(b.months_to_chance(0.5, 0, 7000, 1400, 700), 8)
        self.assertAlmostEqual(b.months_to_chance(0.5, 0.02, 0, 0, 0),
                               math.log(0.5) / math.log(0.98))
        self.assertEqual(b.months_to_chance(0.75, 0, 7000, 1400, 700),
                         float("inf"))
        self.assertEqual(b.months_to_chance(0.5, 0, 7000, 1400, 0), float("inf"))
        self.assertAlmostEqual(b.months_to_chance(0, 0, 7000, 1400, 0), 0)

    def test_paces(self):
        self.assertEqual(b.best_pace([100.0, 500, 100, 400, 400]), 1000 / 3)
        self.assertEqual(b.best_pace([100.0, 500]), 300)
        self.assertEqual(b.best_pace([-200.0]), -200)
        self.assertEqual(b.fifo_paces(FIFO_COHORTS["2025-07"]), [
            ("no sweep", None), ("sweep, recent 3 mo", 300),
            ("sweep, median", 400), ("sweep, best 3 mo", 4100 / 3)])

    def test_fits_and_strips_cleanly_at_every_width(self):
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                out = run_fifo(FIFO_COHORTS, width)
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                self.assertFalse([ln for ln in out.splitlines() if ln != ln.rstrip()])
                with unittest.mock.patch.object(b, "COLOR", True):
                    colored = run_fifo(FIFO_COHORTS, width)
                self.assertEqual(ANSI.sub("", colored), out)
                for figure in ("6,300", "0.7%", "0.76", "1,367", "36.2"):
                    self.assertIn(figure, out)

    def test_target_grew_and_never_are_painted(self):
        # 2025-07's people ahead stuck at 9,300: its own count looks like it
        # shrank, 2025-06's like it grew.
        stalled = {**FIFO_COHORTS, "2025-07": [(p, 9300.0) for p, _
                                               in FIFO_COHORTS["2025-07"]]}
        with unittest.mock.patch.object(b, "COLOR", True):
            out = run_fifo(stalled)
            never = run_fifo(FLAT_COHORTS)
        self.assertRegex(out, "\n\033\\[1m  2025-07 ")
        self.assertRegex(out, "\n  2025-06    1,500  \033\\[33m     grew")
        self.assertRegex(never, "\033\\[31m *never")

    # Exact text, as for the summary, so wording and alignment changes show.
    def test_report_text(self):
        self.assertEqual(run_fifo(FIFO_COHORTS), """
============================================================
PROCESSING ORDER
============================================================
Each cohort's own applicants still waiting in 2026-10, and the share of them
cleared per month (compounded) over the last 3 and 6 months. 'earlier' is
everyone who applied before 2025-05. Cohorts newer than 2025-07 are left out:
late applications are still being added to them.

  cohort   waiting  last 3 mo  last 6 mo
  earlier    6,300       3.5%       4.1%
  2025-05    1,500       2.1%       3.0%
  2025-06    1,500       2.1%       3.0%
  2025-07    2,200       0.0%       0.7%

Typical cohort, last 6 mo: 3.0% a month

Oldest-first or random?
  Of the files cleared, the share that were ahead of 2025-07, against everyone
  from 2025-07. Strict oldest-first would make it 100%; random order would
  match their share of the files.
    Last 3 months: 900 ahead and 0 from 2025-07 cleared, 100% ahead against
      82% at random. Score 1.00 (0 random, 1 oldest-first): close to
      oldest-first.
    Last 6 months: 2,400 ahead and 100 from 2025-07 cleared, 96% ahead against
      84% at random. Score 0.76 (0 random, 1 oldest-first): close to
      oldest-first.

When will 2025-07 be processed?
Months until your chance reaches each column. Every cohort clears 3.0% a month
whatever its age (the typical rate above). A sweep of oldest files at the
row's pace reaches 2025-07 once all but 30% of the 9,300 ahead are gone, then
works through 2025-07's 2,200 until 30% are left. Past 70% you're waiting
among the stragglers, which only the background clears. 'No sweep' is the
current lull lasting.

                      pace/mo    25%    50%    75%    90%
  no sweep                  -    9.5   22.8   45.6   75.8
  sweep, recent 3 mo      300    9.5   21.9   25.1   36.2
  sweep, median           400    9.5   17.1   19.3   36.2
  sweep, best 3 mo      1,367    5.0    5.4    6.0   36.2
""" + "=" * WIDE + "\n\n")

    def test_stacked_text(self):
        out = run_fifo(FIFO_COHORTS, STACKED)
        self.assertEqual(out[out.index("    pace/mo"):], """\
    pace/mo    25%    50%    75%    90%
  no sweep
          -    9.5   22.8   45.6   75.8
  sweep, recent 3 mo
        300    9.5   21.9   25.1   36.2
  sweep, median
        400    9.5   17.1   19.3   36.2
  sweep, best 3 mo
      1,367    5.0    5.4    6.0   36.2
""" + "=" * STACKED + "\n\n")
        self.assertIn("\n  cohort   waiting  last 3 mo  last 6 mo\n", out)

    def test_short_headings_when_the_long_ones_dont_fit(self):
        buf = io.StringIO()
        rows = [b.CohortRates("2025-07", 2200.0, (0.0, None))]
        with contextlib.redirect_stdout(buf):
            b.print_fifo_table(rows, "2025-07", 39)
            b.print_fifo_table(rows, "2025-07", 40)
        self.assertEqual(buf.getvalue(), """\
  cohort   waiting  3 mo  6 mo
  2025-07    2,200  0.0%   n/a
  cohort   waiting  last 3 mo  last 6 mo
  2025-07    2,200       0.0%        n/a
""")

    def test_newer_settled_cohorts_join_the_comparison(self):
        out = run_fifo(FIFO_COHORTS, WIDE, "2025-06")
        self.assertIn("against everyone\n  from 2025-06 to 2025-07.", out)
        self.assertIn("Last 3 months: 800 ahead and 100 from 2025-06 to 2025-07", out)
        self.assertIn("When will 2025-06 be processed?", out)

    def test_order_with_nothing_cleared(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_order([b.OrderScore(3, 800.0, 0.0, 200.0, 0.0)], "2025-07",
                          ["2025-07"], WIDE)
        self.assertIn("    Last 3 months: 0 ahead and 0 from 2025-07 cleared, n/a "
                      "ahead against 80%\n      at random. Score n/a (0 random, 1 "
                      "oldest-first): nothing cleared.\n", buf.getvalue())

    def test_no_settled_cohort(self):
        self.assertEqual(run_fifo({"2026-09": JULY_COHORTS["2025-07"]}, 60), """
============================================================
PROCESSING ORDER
============================================================
No cohort from 2025-01 on is 12 months old with figures in
flpt_history.csv yet.
""")
        self.assertIn("No cohort", run_fifo({}, 60))

    def test_target_without_figures(self):
        for target in ("2025-08", "2027-01"):
            out = run_fifo(FIFO_COHORTS, 60, target)
            self.assertTrue(out.endswith(f"""
Typical cohort, last 6 mo: 3.0% a month

No {target} own-cohort figure for 2026-10, so there's no
order score or estimate for it.
""" + "=" * 60 + "\n\n"), target)

    def test_no_background(self):
        out = run_fifo(FLAT_COHORTS)
        self.assertIn("Typical cohort, last 6 mo: 0.0% a month", out)
        self.assertIn("  no sweep                  -  never  never  never  never", out)
        one = {"2025-07": [("2026-10", 9300.0)], "2025-08": [("2026-10", 11500.0)]}
        out = run_fifo(one)
        self.assertIn("Typical cohort, last 6 mo: n/a", out)
        self.assertIn("Every cohort clears 0.0% a month", out)
        # A single month has no pace, so only the no-sweep row is left.
        self.assertIn("  no sweep        -  never  never  never  never\n=", out)
        self.assertEqual(b.fifo_paces(one["2025-07"]), [("no sweep", None)])

    # The tests below were added to kill mutants the ones above let by.
    def test_latest_period_is_the_newest_anywhere(self):
        cohorts = {"2025-05": [("2026-08", 1.0)], "2025-06": [("2026-10", 1.0)],
                   "2025-07": [("2026-09", 1.0)]}
        self.assertEqual(b.latest_period(cohorts), "2026-10")

    def test_order_score_edges(self):
        # Any clearance at all is measurable, however small.
        self.assertEqual(b.OrderScore(3, 1.0, 1.0, 0.0, 0.0).observed(), 1)
        tiny = {"2025-07": [("2026-10", 0.0), ("2026-07", 1.0)],
                "2025-08": [("2026-10", 0.0), ("2026-07", 1.0)]}
        self.assertEqual(b.order_score(tiny, "2025-07", ["2025-07"], "2026-10", 3),
                         b.OrderScore(3, 1.0, 1.0, 0.0, 0.0))
        # Fewer files ahead than behind is still a score.
        few = {"2025-07": [("2026-10", 0.0), ("2026-07", 100.0)],
               "2025-08": [("2026-10", 500.0), ("2026-07", 600.0)]}
        self.assertEqual(b.order_score(few, "2025-07", ["2025-07"], "2026-10", 3),
                         b.OrderScore(3, 100.0, 100.0, 500.0, 0.0))

    def test_sweep_edges(self):
        # One a month through a cohort of one, starting now.
        self.assertAlmostEqual(b.sweep_chance(1, 0, 0, 1, 1), 1 - b.STRAGGLERS)
        self.assertAlmostEqual(b.sweep_chance(0.5, 0, -5, 1, 1), 0.5)
        # A row without a sweep never gets one.
        self.assertEqual(b.estimate_cells(0.0, 0.0, 1.0, None), ["never"] * 4)
        # ESTIMATE_CAP is where "never" starts: a 50% chance just inside it
        # is a figure, just past it isn't.
        for months, shown in ((1199.5, "1199.5"), (1200.5, "never")):
            background = 1 - 0.5 ** (1 / months)
            self.assertEqual(b.estimate_cells(background, 0.0, 1.0, None)[1],
                             shown, months)

    def test_estimate_rows(self):
        def render(paces, width):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                b.print_estimates(paces, 0.0, 7000.0, 1400.0, width)
            return buf.getvalue()
        paces = [("no sweep", None), ("shrinking", 0.0), ("growing", -50.0),
                 ("sweep, recent 3 mo", 700.0)]
        # Inline down to exactly the width it needs: 2 + 18 + 2 + 7 + 4 * 7.
        self.assertEqual(render(paces, 57), """\
                      pace/mo    25%    50%    75%    90%
  no sweep                  -  never  never  never  never
  shrinking                 0  never  never  never  never
  growing                   0  never  never  never  never
  sweep, recent 3 mo      700    7.5    8.0  never  never
""")
        self.assertEqual(render(paces, 56).splitlines()[:3], [
            "    pace/mo    25%    50%    75%    90%",
            "  no sweep", "          -  never  never  never  never"])

    def test_table_without_a_latest_figure(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_fifo_table([b.CohortRates("earlier", None, (None, None))],
                               "2025-07", WIDE)
        self.assertEqual(buf.getvalue().splitlines()[1],
                         "  earlier      n/a        n/a        n/a")

    def test_comparison_spans_every_newer_cohort(self):
        out = run_fifo(FIFO_COHORTS, WIDE, "2025-05")
        self.assertIn("against everyone\n  from 2025-05 to 2025-07.", out)

    def test_typical_rate_is_the_longest_window(self):
        with unittest.mock.patch.object(b, "OWN_WINDOWS", (1, 3, 6)):
            out = run_fifo(FIFO_COHORTS)
        self.assertIn("\nTypical cohort, last 6 mo: 3.0% a month\n", out)
        self.assertIn("  cohort   waiting  last 1 mo  last 3 mo  last 6 mo\n", out)

    def test_growing_cohorts_clear_nothing_in_the_model(self):
        # Every cohort's own count grows: the typical rate is negative, and
        # the estimates treat that as no background at all.
        grow = {c: [(p, n * (2 - k / 10)) for (p, n), k in zip(s, range(10))]
                for c, s in FIFO_COHORTS.items()}
        out = run_fifo(grow)
        self.assertRegex(out, r"Typical cohort, last 6 mo: -\d+\.\d% a month")
        self.assertIn("Every cohort clears 0.0% a month", " ".join(out.split()))
        self.assertIn("  no sweep                  -  never  never  never  never",
                      out)

    def test_no_figure_note_is_painted_cleanly(self):
        plain = run_fifo(FIFO_COHORTS, 60, "2025-08")
        with unittest.mock.patch.object(b, "COLOR", True):
            colored = run_fifo(FIFO_COHORTS, 60, "2025-08")
        self.assertEqual(ANSI.sub("", colored), plain)
        self.assertTrue(colored.endswith("\033[36m" + "=" * 60 + "\033[0m\n\n"))

class MainFifo(unittest.TestCase):
    def history(self, d: str) -> Path:
        return write_history(d, "".join(f"{p},{c},{n:.0f}\n"
                                        for c, series in FIFO_COHORTS.items()
                                        for p, n in series))

    def test_reports_without_prompting(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_main("", self.history(d), ("--fifo",))
            other = run_main("", self.history(d), ("--fifo", "2025/6"))
        self.assertIn("When will 2025-07 be processed?", out)
        self.assertNotIn("Pick a cohort", out)
        self.assertIn("When will 2025-06 be processed?", other)

    def test_bad_cohort_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            run_main("", Path(d) / "none.csv", ("--fifo", "July"))
        self.assertIn("argument --fifo: not a YYYY-MM month: 'July'", err.getvalue())

    def test_help_describes_the_flag(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf, \
                self.assertRaises(SystemExit):
            b.parse_args(["--help"])
        self.assertIn("--fifo [COHORT] no prompt: how close to oldest-first "
                      "processing is, and when COHORT (default 2025-07) is "
                      "likely to be processed", " ".join(buf.getvalue().split()))


def run_analyze_colored(entered: list[float], width: int = WIDE) -> str:
    with unittest.mock.patch.object(b, "COLOR", True):
        return run_analyze(entered, width)


class Color(unittest.TestCase):
    DATA = Widths.DATA

    def test_enabled_on_a_tty(self):
        tty, pipe = unittest.mock.Mock(), io.StringIO()
        tty.isatty.return_value = True
        self.assertTrue(b.color_enabled(tty, {}))
        self.assertFalse(b.color_enabled(pipe, {}))
        self.assertFalse(b.color_enabled(object(), {}))

    def test_defaults_to_stdout(self):
        # Not a Mock: it would answer to any attribute name, not just isatty.
        tty = types.SimpleNamespace(isatty=lambda: True)
        with unittest.mock.patch.object(b.sys, "stdout", tty):
            self.assertTrue(b.color_enabled(environ={}))

    def test_env_overrides(self):
        tty = unittest.mock.Mock()
        tty.isatty.return_value = True
        pipe = io.StringIO()
        self.assertFalse(b.color_enabled(tty, {"NO_COLOR": "1"}))
        self.assertFalse(b.color_enabled(tty, {"TERM": "dumb"}))
        self.assertTrue(b.color_enabled(tty, {"NO_COLOR": ""}))
        self.assertTrue(b.color_enabled(pipe, {"FORCE_COLOR": "1"}))
        self.assertFalse(b.color_enabled(pipe, {"FORCE_COLOR": "0"}))
        self.assertFalse(b.color_enabled(pipe, {"FORCE_COLOR": ""}))
        self.assertTrue(b.color_enabled(pipe, {"FORCE_COLOR": "1",
                                               "TERM": "dumb"}))
        self.assertFalse(b.color_enabled(pipe, {"FORCE_COLOR": "1",
                                                "NO_COLOR": "1"}))

    def test_paint_is_a_no_op_when_off(self):
        self.assertEqual(b.paint("x", "bold"), "x")

    def test_paint_styles_each_line_and_skips_blank_ones(self):
        with unittest.mock.patch.object(b, "COLOR", True):
            self.assertEqual(b.paint("a\n\nb", "bold", "red"),
                             "\033[1;31ma\033[0m\n\n\033[1;31mb\033[0m")
            self.assertEqual(b.paint("a"), "a")
            self.assertEqual(b.paint("", "bold"), "")

    def test_stat_line_paints_only_the_value(self):
        plain = b.stat_line("Label", "value", 1000)
        with unittest.mock.patch.object(b, "COLOR", True):
            line = b.stat_line("Label", "value", 1000)
            self.assertEqual(ANSI.sub("", line), plain)
            self.assertTrue(line.startswith("Label:"))
            self.assertIn("\033[1mvalue\033[0m", line)
            tight = b.stat_line("Label", "  value", len("Label: value"))
            self.assertEqual(tight, "Label: \033[1mvalue\033[0m")

    def test_styling_never_changes_the_text(self):
        # Colour is painted over a finished layout, so stripping it has to give
        # back exactly the plain report — at every width, in every layout.
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                colored = run_analyze_colored(self.DATA, width)
                self.assertIn("\033[", colored)
                self.assertEqual(ANSI.sub("", colored),
                                 run_analyze(self.DATA, width))

    def test_no_style_bleeds_past_its_line(self):
        for width in (WIDE, COMPACT, STACKED):
            for line in run_analyze_colored(self.DATA, width).splitlines():
                codes = ANSI.findall(line)
                if codes:
                    self.assertEqual(codes[-1], "\033[0m", repr(line))

    def test_main_honours_force_and_no_color(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "none.csv"
            plain = run_main("", missing)
            forced = run_main("", missing, FORCE_COLOR="1")
            self.assertNotIn("\033[", plain)
            self.assertIn("\033[", forced)
            self.assertEqual(ANSI.sub("", forced), plain)
            self.assertNotIn("\033[", run_main("", missing, FORCE_COLOR="1",
                                               NO_COLOR="1"))
        self.assertFalse(b.COLOR)


class FuzzPipeline(unittest.TestCase):
    """
    Random inputs through the whole pipeline.

    These counts come from a public feed and get pasted by hand, so the
    interesting failures are shapes nobody wrote a case for: queues that grow,
    stall, clear entirely, or arrive as a mangled two-column paste.
    """

    CASES = int(os.environ.get("BURNDOWN_FUZZ_CASES", "300"))

    def _random_backlog(self, rng: random.Random) -> list[float]:
        n = rng.randint(2, 14)
        shape = rng.choice(["shrinking", "growing", "flat", "noisy", "clears"])
        value = float(rng.randint(0, 60000))
        chrono = [value]
        for _ in range(n - 1):
            if shape == "shrinking":
                value -= rng.randint(0, 3000)
            elif shape == "growing":
                value += rng.randint(0, 3000)
            elif shape == "flat":
                pass
            elif shape == "clears":
                value -= rng.randint(0, int(value) + 1)
            else:
                value += rng.randint(-3000, 3000)
            chrono.append(float(value))
        chrono.reverse()  # analyze() wants newest first
        return chrono

    def test_analyze_never_raises(self):
        rng = random.Random(20260829)
        for i in range(self.CASES):
            entered = self._random_backlog(rng)
            width = rng.randint(b.MIN_WIDTH, b.MAX_WIDTH)
            with self.subTest(case=i, entered=entered, width=width):
                out = run_analyze(entered, width)
                self.assertTrue(out.strip())
                # A NaN or an unformatted inf leaking into the report means the
                # arithmetic went somewhere the formatters don't handle.
                self.assertNotIn("nan", out.lower())
                self.assertNotIn("inf", out.lower())
                # Whatever the numbers, the report has to stay inside the
                # terminal — that's what the narrow layouts are for.
                longest = max(len(line) for line in out.splitlines())
                self.assertLessEqual(longest, width)
                # And colour is only ever paint on that same text.
                colored = run_analyze_colored(entered, width)
                self.assertEqual(ANSI.sub("", colored), out)

    def test_own_cohort_never_breaks_the_report(self):
        # Own-cohort counts are differences of two rounded public figures, so
        # they can be zero, flat, growing or even negative; none of that may
        # crash, overflow or leak a NaN.
        rng = random.Random(20261015)
        for i in range(self.CASES):
            entered = self._random_backlog(rng)
            start = rng.randint(2020 * 12, 2027 * 12)
            periods = [b.month_label(start + k) for k in range(len(entered))]
            periods = [p for p in periods if rng.random() < 0.85] or periods[:1]
            waiting = {p: float(rng.randint(-200, 5000)) for p in periods}
            ahead = {p: float(rng.randint(0, 60000)) for p in periods}
            own = b.OwnCohort(b.month_label(start - 12), waiting, ahead)
            width = rng.randint(b.MIN_WIDTH, b.MAX_WIDTH)
            with self.subTest(case=i, own=own, width=width):
                out = run_analyze(entered, width, own)
                self.assertIn("Your own cohort", out)
                self.assertNotIn("nan", out.lower())
                self.assertNotIn("inf", out.lower())
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                for n, mine, theirs in b.own_windows(own):
                    self.assertGreaterEqual(n, 1)
                    for rate in (mine, theirs):
                        self.assertTrue(rate is None or rate <= 1.0)

    def test_summary_never_breaks(self):
        # Every cohort from the history, however odd its counts, gets a row
        # that fits the terminal; nothing raises or leaks a NaN.
        rng = random.Random(20261017)
        for i in range(self.CASES):
            cohorts = {}
            for _ in range(rng.randint(0, 8)):
                cohort = b.month_label(rng.randint(2024 * 12, 2027 * 12))
                start = rng.randint(2025 * 12, 2027 * 12)
                counts = self._random_backlog(rng)
                if rng.random() < 0.1:
                    counts = [c * 10 ** rng.randint(3, 300) for c in counts]
                cohorts[cohort] = sorted(
                    ((b.month_label(start + k), c) for k, c in enumerate(counts)
                     if rng.random() < 0.9), reverse=True)
            width = rng.randint(b.MIN_WIDTH, b.MAX_WIDTH)
            with self.subTest(case=i, cohorts=cohorts, width=width):
                out = run_summary(cohorts, width)
                self.assertIn("COHORT SUMMARY", out)
                self.assertNotIn("nan", out.lower())
                self.assertNotIn("inf", out.lower())
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                for row in b.summary_rows(cohorts):
                    self.assertIn(row[0], out)

    def test_shrink_rate_compounds_back_to_the_end_count(self):
        # Applying the rate for `months` months has to land on `end` again.
        rng = random.Random(20261016)
        for i in range(self.CASES):
            start = float(rng.randint(1, 60000))
            end = float(rng.randint(0, 60000))
            months = rng.randint(1, 60)
            with self.subTest(case=i, start=start, end=end, months=months):
                rate = b.shrink_rate(start, end, months)
                self.assertLessEqual(rate, 1.0)
                self.assertEqual(rate > 0, end < start)
                self.assertAlmostEqual(start * (1 - rate) ** months, end,
                                       delta=1e-6 * max(start, end, 1))

    def test_odds_invariants(self):
        rng = random.Random(20260830)
        for i in range(self.CASES):
            entered = self._random_backlog(rng)
            chrono = list(reversed(entered))
            processed = chrono_to_processed(chrono)
            with self.subTest(case=i, chrono=chrono):
                odds = b.monthly_odds(chrono, processed)
                self.assertEqual(len(odds), len(processed))
                for chance in odds:
                    if chance is not None:
                        self.assertGreater(chance, 0.0)
                        self.assertLessEqual(chance, 1.0)

    def test_done_by_is_monotonic_and_bounded(self):
        rng = random.Random(20260831)
        for i in range(self.CASES):
            current = float(rng.randint(0, 60000))
            rate = float(rng.randint(-2000, 5000))
            with self.subTest(case=i, current=current, rate=rate):
                previous = 0.0
                for months in range(0, 60):
                    p = b.done_by(current, rate, months)
                    if p is None:
                        self.assertTrue(rate <= 0 or current <= 0)
                        continue
                    self.assertGreaterEqual(p, previous)
                    self.assertGreaterEqual(p, 0.0)
                    self.assertLessEqual(p, 1.0)
                    previous = p
                    if rate * months >= current:
                        self.assertEqual(p, 1.0)

    def test_load_cohorts_against_hand_edited_history(self):
        """
        Random history files, each row rendered the way a person might type
        it (unpadded months, slashes, thousands separators, stray spaces) or
        mangled outright. Checked against the ground truth the rows were made
        from: last row wins per (cohort, period), series newest first, every
        mangled row reported by line, and nothing ever raises.
        """
        rng = random.Random(20261007)

        def month(year: int, mon: int) -> str:
            sep = rng.choice("-/" if rng.random() < 0.3 else "-")
            pad = f"{mon:02d}" if rng.random() < 0.7 else str(mon)
            return rng.choice(["", " "]) + f"{year}{sep}{pad}" + rng.choice(["", " "])

        bad_months = ["Sept", "2026-13", "2026-0", "26-09", "2026-09-01", "?"]
        bad_counts = ["nan", "inf", "-inf", "-5", "abc", "9.7k"]
        for i in range(self.CASES):
            rows, expected, rejected = [], {}, []
            for line in range(2, rng.randint(2, 40)):
                year, mon = rng.randint(2024, 2028), rng.randint(1, 12)
                cyear, cmon = rng.randint(2020, 2026), rng.randint(1, 12)
                count = rng.randint(0, 200000)
                period, cohort = month(year, mon), month(cyear, cmon)
                text = rng.choice([str(count), f"{count:,}", f"{count}.0", f" {count} "])
                roll = rng.random()
                if roll < 0.1:
                    text = ""
                elif roll < 0.2:
                    text = rng.choice(bad_counts)
                elif roll < 0.25:
                    period = rng.choice(bad_months)
                elif roll < 0.3:
                    cohort = rng.choice(bad_months)
                rows.append([period, cohort, text])
                if not text:
                    continue
                if roll < 0.3:
                    rejected.append(line)
                    continue
                expected.setdefault(f"{cyear}-{cmon:02d}", {})[
                    f"{year}-{mon:02d}"] = float(count)
            with tempfile.TemporaryDirectory() as d, self.subTest(case=i, rows=rows):
                path = Path(d) / "h.csv"
                with path.open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["period", "cohort", "people_ahead"])
                    writer.writerows(rows)
                got_rejected: list[int] = []
                got = b.load_cohorts(path, got_rejected)
                self.assertEqual(got_rejected, rejected)
                self.assertEqual(
                    got, {c: sorted(p.items(), reverse=True)
                          for c, p in sorted(expected.items())})
                for series in got.values():
                    b.missing_months([p for p, _ in series])

    def test_parse_round_trips_what_it_formats(self):
        rng = random.Random(20260901)
        for i in range(self.CASES):
            values = [float(rng.randint(0, 60000)) for _ in range(rng.randint(1, 12))]
            raw = ", ".join(str(v) for v in values)
            with self.subTest(case=i, raw=raw):
                self.assertEqual(b.parse_numbers(raw), values)


class FuzzFifo(unittest.TestCase):
    """Property checks on the --fifo model and report."""

    CASES = FuzzPipeline.CASES

    def test_chance_is_a_rising_probability(self):
        rng = random.Random(20261024)
        for i in range(self.CASES):
            args = (rng.uniform(-0.1, 0.2), rng.uniform(-1000, 60000),
                    rng.uniform(-100, 5000), rng.uniform(-500, 3000))
            months = sorted(rng.uniform(0, 300) for _ in range(5))
            with self.subTest(case=i, args=args, months=months):
                chances = [b.sweep_chance(m, *args) for m in months]
                self.assertEqual(chances, sorted(chances))
                for c in chances:
                    self.assertTrue(0 <= c <= 1)

    def test_months_to_chance_inverts_sweep_chance(self):
        rng = random.Random(20261025)
        for i in range(self.CASES):
            args = (rng.choice([0.0, rng.uniform(0.001, 0.2)]),
                    rng.uniform(0, 60000), rng.uniform(1, 5000),
                    rng.choice([0.0, rng.uniform(1, 3000)]))
            q = rng.choice(b.ESTIMATE_ODDS)
            with self.subTest(case=i, args=args, q=q):
                m = b.months_to_chance(q, *args)
                if m == float("inf"):
                    self.assertLess(b.sweep_chance(b.ESTIMATE_CAP, *args), q)
                else:
                    self.assertGreaterEqual(b.sweep_chance(m, *args), q)
                    self.assertLess(b.sweep_chance(max(m - 1e-6, 0), *args), q + 1e-9)

    def test_score_is_one_for_fifo_and_zero_for_random(self):
        rng = random.Random(20261026)
        for i in range(self.CASES):
            ahead, newer = rng.uniform(1, 60000), rng.uniform(1, 60000)
            share = rng.uniform(0.001, 1)
            with self.subTest(case=i):
                fifo = b.OrderScore(3, ahead, ahead * share, newer, 0.0)
                self.assertAlmostEqual(fifo.score(), 1)
                rand = b.OrderScore(3, ahead, ahead * share, newer, newer * share)
                self.assertAlmostEqual(rand.score(), 0)

    def test_report_never_breaks(self):
        rng = random.Random(20261027)
        fuzz = FuzzPipeline()
        estimated = 0
        for i in range(self.CASES):
            # Mostly consecutive months, so most cases get past "nothing
            # settled" and through the scores and estimates.
            cohorts = {}
            first = rng.randint(2024 * 12, 2025 * 12 + 6)
            for k in range(rng.randint(0, 10)):
                if rng.random() < 0.1:
                    continue
                cohort = b.month_label(first + k)
                counts = fuzz._random_backlog(rng)[:rng.choice([1, 14])]
                start = 2026 * 12 + 9 - len(counts) + 1
                cohorts[cohort] = sorted(
                    ((b.month_label(start + k), c) for k, c in enumerate(counts)
                     if rng.random() < 0.9), reverse=True)
            cohorts = {c: s for c, s in sorted(cohorts.items()) if s}
            width = rng.randint(b.MIN_WIDTH, b.MAX_WIDTH)
            target = rng.choice(list(cohorts) or [b.DEFAULT_COHORT])
            with self.subTest(case=i, cohorts=cohorts, width=width, target=target):
                out = run_fifo(cohorts, width, target)
                self.assertIn("PROCESSING ORDER", out)
                self.assertNotIn("nan", out.lower())
                self.assertNotIn("inf", out.lower())
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                estimated += "be processed?" in out
        # The generator has to reach the estimates, not just the early exits.
        self.assertGreater(estimated, self.CASES // 8)


if __name__ == "__main__":
    unittest.main()
