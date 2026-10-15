#!/usr/bin/env python3
"""
Tests for burndown2.py.

Stdlib-only (unittest), to match the rest of the repo — no dependencies to
install, just:

    python3 -m unittest test_burndown2 -v

The fuzz cases at the bottom run a small number of random inputs by default so
this stays a fast pre-commit check. Crank it for a longer campaign with:

    BURNDOWN_FUZZ_CASES=20000 python3 -m unittest test_burndown2.FuzzPipeline
"""

import contextlib
import csv
import io
import os
import random
import re
import tempfile
import types
import unittest
import unittest.mock

from pathlib import Path

import burndown2 as b


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


def run_main(typed: str, history: Path, **env: str) -> str:
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
        b.main()
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

    def test_no_analyzable_cohort_keeps_the_plain_prompt(self):
        out = self._run("", "2026-09,2025-08,11900\n")
        self.assertNotIn("Cohorts tracked", out)
        self.assertIn("Using the default", out)


ANSI = re.compile(r"\033\[[0-9;]*m")


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


if __name__ == "__main__":
    unittest.main()
