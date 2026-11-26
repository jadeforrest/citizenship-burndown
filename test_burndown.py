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
import json
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


# Pin a width for the assertions that read the report as text: the layout is
# terminal-dependent now, and a test that renders at whatever size the runner's
# terminal happens to be is a test that fails on someone else's machine.
WIDE = b.MAX_WIDTH
COMPACT = 80
STACKED = b.MIN_WIDTH


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


class Formatting(unittest.TestCase):
    def test_fmt_pct(self):
        self.assertEqual(b.fmt_pct(0.0294), "2.9%")
        self.assertEqual(b.fmt_pct(1.0), "100.0%")


class Widths(unittest.TestCase):
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
        # Without the override, the terminal's own width, clamped the same way.
        with unittest.mock.patch.dict(os.environ, {"BURNDOWN_WIDTH": ""}), \
                unittest.mock.patch.object(b.shutil, "get_terminal_size",
                                           return_value=os.terminal_size((90, 24))):
            self.assertEqual(b.output_width(), 90)

    def test_stat_line_wraps_when_even_the_tight_form_wont_fit(self):
        line = b.stat_line("A long label here", "1.4% a month", 20)
        self.assertEqual(line, "A long label here:\n  1.4% a month")


def run_main(history: Path, argv: tuple[str, ...] = (), **env: str) -> str:
    """Run main() with `history` as the watcher CSV."""
    buf = io.StringIO()
    # Blank NO_COLOR/FORCE_COLOR so the developer's own shell can't switch
    # colour on; patching COLOR restores it after main() sets it.
    env = {"BURNDOWN_WIDTH": "100", "NO_COLOR": "", "FORCE_COLOR": "", **env}
    with unittest.mock.patch.object(b, "HISTORY_PATH", history), \
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


    def test_rate_cell(self):
        self.assertEqual(b.rate_cell(None), ("n/a", ()))
        self.assertEqual(b.rate_cell(-0.2), ("grew", ("yellow",)))
        self.assertEqual(b.rate_cell(0.0141), ("1.4%", ()))


    # The tests below pin the block's exact text; they were added to kill the
    # mutants (text, alignment, padding) the looser assertions above let by.
    # 13 months, so the windows are 3, 6 and 12: labels of different lengths.
    PERIODS13 = [b.month_label(b.month_index("2025-10") + k) for k in range(13)]
    OWN13 = b.OwnCohort("2025-07", dict(zip(PERIODS13, [
        2600.0, 2550, 2500, 2500, 2450, 2400, 2400, 2350, 2300, 2200, 2250, 2250,
        2300])), dict(zip(PERIODS13, [
            20000.0, 19000, 18000, 17000, 16000, 15000, 14000, 13000, 12000, 11000,
            10000, 9500, 9000])))


SAMPLE = ("2026-07,2025-07,10500\n2026-08,2025-07,10200\n2026-09,2025-07,9700\n"
          "2026-09,2025-08,11900\n2026-08,2025-08,12500\n"
          "2026-09,2025-09,14400\n")


ANSI = re.compile(r"\033\[[0-9;]*m")


class Cells(unittest.TestCase):

    def test_cells_stay_inside_their_column(self):
        for value in (0, 7, 9999999, 10000000, -9999999, 1e300, -1e300, 0.4):
            self.assertLessEqual(len(b.fmt_count(value)), b.SUMMARY_CELL, value)
        for months in (0, 1.25, 99999.9, 9999.94, 1e7, 1e300, float("inf")):
            self.assertLessEqual(len(b.fmt_duration(months)), b.SUMMARY_CELL,
                                 months)
        self.assertEqual(b.fmt_count(9300), "9,300")
        self.assertEqual(b.fmt_count(999999), "999,999")   # exactly SUMMARY_CELL
        # Whole months, as years and months: the inputs are too rough for tenths.
        self.assertEqual(b.fmt_duration(11.4), "11m")
        self.assertEqual(b.fmt_duration(11.6), "1y")
        self.assertEqual(b.fmt_duration(12.94), "1y 1m")
        self.assertEqual(b.fmt_duration(17.2), "1y 5m")
        self.assertEqual(b.fmt_duration(1), "1m")
        self.assertEqual(b.fmt_duration(0.99), "<1m")
        self.assertEqual(b.fmt_duration(0), "<1m")
        # Months dropped past 99y 11m, e-notation once the years overflow.
        self.assertEqual(b.fmt_duration(1199), "99y 11m")
        self.assertEqual(b.fmt_duration(1201), "100y 1m")
        self.assertEqual(b.fmt_duration(12001), "1,000y")
        self.assertEqual(b.fmt_duration(99999.4), "8,333y")
        self.assertEqual(b.fmt_duration(12e6), "1e+06y")
        self.assertEqual(b.fmt_duration(float("inf")), "never")

    def test_fmt_when(self):
        inf = float("inf")
        self.assertEqual(b.fmt_when(17.2), "1y 5m")     # --durations
        with unittest.mock.patch.object(b, "DATES_FROM", "2026-11"):
            self.assertEqual(b.fmt_when(17.2), "2028-04")
            self.assertEqual(b.fmt_when(1.5), "2027-01")
            self.assertEqual(b.fmt_when(0.3), "2026-11")
            self.assertEqual(b.fmt_when(-2.0), "2026-11")
            self.assertEqual(b.fmt_when(inf), "never")
            self.assertEqual(b.fmt_when(96000.0), "8,000y")   # past year 9999
            self.assertEqual(b.fmt_when(1e300), "8e+298y")


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
        self.assertEqual(b.settled_cohorts({"2025-07": []}), [])
        self.assertEqual(b.latest_period({"2025-06": [], "2025-07": [("2026-10", 1.0)]}),
                         "2026-10")
        # A run of consecutive settled cohorts: 2025-11 is still bulging, so
        # it and everything after it are left out, settled or not.
        months = ["2026-07", "2026-08", "2026-09", "2026-10"]
        def chain(*owns):
            ahead, out = [1000.0] * 4, {}
            for k, own in enumerate(owns):
                out[f"2025-{9 + k:02d}"] = list(zip(months, ahead))
                ahead = [a + o for a, o in zip(ahead, own)]
            out[f"2025-{9 + len(owns):02d}"] = list(zip(months, ahead))
            return {c: sorted(v, reverse=True) for c, v in out.items()}
        steady, bulge = [500.0, 490, 480, 470], [500.0, 5000, 9000, 800]
        self.assertEqual(b.settled_cohorts(chain(steady, steady)),
                         ["2025-09", "2025-10"])
        self.assertEqual(b.settled_cohorts(chain(steady, bulge, steady)),
                         ["2025-09"])
        self.assertEqual(b.settled_cohorts(chain(bulge, steady)), [])

    def test_settled_from(self):
        months = [f"2026-{m:02d}" for m in range(1, 9)]
        def waiting(*counts):
            return dict(zip(months, counts))
        # Settles at the start of its first BULGE_MONTHS steady months.
        self.assertEqual(b.settled_from(waiting(2000, 29800, 3300, 3200, 3200,
                                                3100)), "2026-03")
        self.assertIsNone(b.settled_from(waiting(2000, 29800, 3300, 3200, 3200)))
        # A rise of BULGE_RISE is rounding; one more isn't.
        self.assertEqual(b.settled_from(waiting(1000, 1200, 1400, 1600)), "2026-01")
        self.assertIsNone(b.settled_from(waiting(1000, 1201, 1401, 1601)))
        # A fall of BULGE_DROP is still steady; more isn't.
        self.assertEqual(b.settled_from(waiting(1000, 850, 722.5, 614.125)),
                         "2026-01")
        self.assertIsNone(b.settled_from(waiting(1000, 849, 722, 614)))
        # The months have to be consecutive.
        self.assertIsNone(b.settled_from({"2026-01": 9.0, "2026-02": 9.0,
                                          "2026-03": 9.0, "2026-05": 9.0}))
        self.assertEqual(b.settled_waiting(waiting(2000, 29800, 3300, 3200, 3200,
                                                   3100)),
                         {"2026-03": 3300, "2026-04": 3200, "2026-05": 3200,
                          "2026-06": 3100})
        self.assertEqual(b.settled_waiting(waiting(2000, 29800)), {})
        self.assertTrue(b.steady(1000, 1200) and b.steady(1000, 850))
        self.assertFalse(b.steady(1000, 1201) or b.steady(1000, 849))

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

    def test_trickle_pools_the_cohorts_behind_the_sweep(self):
        names = ["2025-05", "2025-06", "2025-07"]
        # 2025-05 is at the front (no work ahead), so only 2025-06 and
        # 2025-07 count: 1,600 + 2,200 in 2026-07, 1,500 + 2,200 in 2026-10.
        self.assertAlmostEqual(b.trickle_rate(FIFO_COHORTS, names, "2026-10"),
                               1 - (3700 / 3800) ** (1 / 3))
        self.assertAlmostEqual(b.trickle_rate(FIFO_COHORTS, names, "2026-10", 6),
                               1 - (3700 / (1800 + 2300)) ** (1 / 6))
        # Nothing behind the front, or no window to measure in.
        self.assertIsNone(b.trickle_rate(FIFO_COHORTS, names[:1], "2026-10"))
        self.assertIsNone(b.trickle_rate(FIFO_COHORTS, names, "2026-10", 12))
        self.assertEqual(b.fifo_background(FIFO_COHORTS, names[:1], "2026-10"),
                         (None, 0.0))
        self.assertEqual(b.window_totals([{"2026-07": 1.0, "2026-10": 2.0},
                                          {"2026-10": 5.0}], "2026-10", 3),
                         (1.0, 2.0))
        self.assertIsNone(b.window_totals([], "2026-10", 3))

    def test_sweep_pace_is_one_pace_net_of_the_trickle(self):
        names = ["2025-05", "2025-06", "2025-07"]
        # By 2026-07 the sweep had left the tail (2025-05 was 1,600, 200
        # under its 1,800), so the tail's 7,000 -> 6,300 is the leftovers',
        # and the sweep's is the three cohorts' 5,400 -> 5,200.
        self.assertAlmostEqual(b.sweep_pace(FIFO_COHORTS, names, "2026-10", 0.0),
                               200 / 3)
        self.assertAlmostEqual(b.sweep_pace(FIFO_COHORTS, names, "2026-10", 0.01),
                               (200 - 5400 * (1 - 0.99 ** 3)) / 3)
        self.assertAlmostEqual(b.sweep_pace(FIFO_COHORTS, names, "2026-10", -0.5),
                               200 / 3)
        self.assertAlmostEqual(b.sweep_pace(FIFO_COHORTS, names, "2026-10", 2.0),
                               (200 - 5400) / 3)
        # In 2026-04 it was still in the tail (2025-05 at its 1,800), so the
        # tail counts too: 8,100 + 5,900 -> 7,000 + 5,400.
        self.assertAlmostEqual(b.sweep_pace(FIFO_COHORTS, names, "2026-07", 0.0),
                               1600 / 3)
        self.assertIsNone(b.sweep_pace(FIFO_COHORTS, names, "2026-03", 0.0))

    def test_leftovers_are_who_the_sweep_had_passed(self):
        names = ["2025-05", "2025-06", "2025-07"]
        # The longest window, up to LEFTOVER_MONTHS, opening once the tail
        # was passed (2026-06): 7,200 -> 6,300 over 4 months.
        self.assertEqual(b.leftover_months(FIFO_COHORTS, names, "2026-10"), 4)
        self.assertAlmostEqual(b.leftover_rate(FIFO_COHORTS, names, "2026-10"),
                               1 - (6300 / 7200) ** (1 / 4))
        self.assertAlmostEqual(b.leftover_rate(FIFO_COHORTS, names, "2026-10", 3),
                               1 - (6300 / 7000) ** (1 / 3))
        self.assertEqual(b.leftover_months(FIFO_COHORTS, names, "2026-09"), 3)
        with unittest.mock.patch.object(b, "LEFTOVER_MONTHS", 3):
            self.assertEqual(b.leftover_months(FIFO_COHORTS, names, "2026-10"), 3)
        self.assertIsNone(b.leftover_months(FIFO_COHORTS, names, "2026-07"))
        self.assertIsNone(b.leftover_rate(FIFO_COHORTS, names, "2026-07"))
        # The tail is passed once names[0] is more than rounding under its size.
        self.assertTrue(b.tail_passed(FIFO_COHORTS, names, "2026-06"))
        self.assertFalse(b.tail_passed(FIFO_COHORTS, names, "2026-05"))
        self.assertFalse(b.tail_passed(FIFO_COHORTS, names, "2025-12"))
        self.assertFalse(b.tail_passed(FIFO_COHORTS, ["2025-08"], "2026-07"))
        # A cohort before SUMMARY_FROM has no take: passed.
        old = {"2024-11": [("2026-10", 100.0), ("2026-03", 400.0)],
               "2024-12": [("2026-10", 600.0), ("2026-03", 900.0)]}
        self.assertTrue(b.passed_by(old, "2024-11", "2026-10"))
        self.assertFalse(b.passed_by(old, "2024-12", "2026-10"))   # no own count
        self.assertFalse(b.passed_by(FIFO_COHORTS, "2025-05", "2026-10"))
        # A passed cohort joins the leftovers, out of the sweep's share.
        with unittest.mock.patch.object(b, "passed_by",
                                        side_effect=lambda c, n, p: n == "2025-05"):
            left, rest = b.window_groups(FIFO_COHORTS, names, "2026-07")
        self.assertEqual([len(left), len(rest)], [2, 2])
        self.assertEqual(left[1]["2026-07"], 1600)
        left, rest = b.window_groups(FIFO_COHORTS, names, "2026-04")
        self.assertEqual([len(left), len(rest)], [0, 4])
        self.assertEqual(rest[0], dict(FIFO_COHORTS["2025-05"]))

    def test_model_rates(self):
        names = ["2025-05", "2025-06", "2025-07"]
        rates = b.model_rates(FIFO_COHORTS, names, "2026-10")
        _, background = b.fifo_background(FIFO_COHORTS, names, "2026-10")
        self.assertEqual(rates, b.Rates(
            background, b.leftover_rate(FIFO_COHORTS, names, "2026-10"),
            b.sweep_pace(FIFO_COHORTS, names, "2026-10", background), 4))
        # No leftovers measured: they clear at the trickle, never slower.
        early = b.model_rates(FIFO_COHORTS, names, "2026-07")
        self.assertEqual(early.leftover, early.background)
        with unittest.mock.patch.object(b, "leftover_rate", return_value=-0.5):
            self.assertEqual(b.model_rates(FIFO_COHORTS, names, "2026-10").leftover,
                             background)

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
        self.assertEqual(s, b.OrderScore(6, 11700, 2400, 2300, 100, "2025-07"))
        self.assertAlmostEqual(s.score(), (2400 / 2500 - 11700 / 14000)
                               / (1 - 11700 / 14000))
        two = b.order_score(FIFO_COHORTS, "2025-05", ["2025-06", "2025-07"],
                            "2026-10", 3)
        self.assertEqual(two, b.OrderScore(3, 7000, 700, 1600 + 2200, 100, "2025-07"))
        for args in ((["2025-07"], "2026-10", 12),     # starts before the data
                     ([], "2026-10", 3),
                     (["2025-08"], "2026-10", 3)):     # no own count
            self.assertIsNone(b.order_score(FIFO_COHORTS, "2025-07", *args), args)
        quiet = [(f"2026-{m:02d}", 0.0) for m in range(10, 6, -1)]
        empty = {"2025-07": quiet, "2025-08": quiet}
        self.assertEqual(b.settled_from(b.own_cohort(empty, "2025-07").waiting),
                         "2026-07")
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
        far = b.Position(9300, 2200, 1540)
        self.assertAlmostEqual(b.sweep_chance(10, 0.02, far, 0), 1 - 0.98 ** 10)
        self.assertEqual(b.sweep_chance(0, 0.02, far, 300), 0)
        # Sweep alone: arrives after 4,900 / 700 = 7 months, then 700 a month
        # of 1,400 waiting, capped once it has taken 980.
        pos = b.Position(4900, 1400, 980)
        for months, chance in ((7, 0), (8, 0.5), (8.4, 0.7), (100, 0.7)):
            self.assertAlmostEqual(b.sweep_chance(months, 0, pos, 700),
                                   chance, msg=months)
        # A shrinking queue's negative background is no help, and no harm.
        self.assertEqual(b.sweep_chance(12, -0.05, pos, 0), 0)
        self.assertEqual(b.sweep_chance(12, 0, b.Position(4900, 0, 0), 700), 0)
        self.assertAlmostEqual(b.sweep_chance(1, 0, b.Position(-5, 700, 490), 700),
                               0.7)
        # A cohort the sweep has passed gets nothing from it; a take beyond
        # what's waiting, or below nothing, is held to the cohort.
        self.assertEqual(b.sweep_chance(50, 0, b.Position(0, 700, 0), 700), 0)
        self.assertEqual(b.sweep_chance(50, 0, b.Position(0, 700, -5), 700), 0)
        self.assertEqual(b.sweep_chance(50, 0, b.Position(0, 700, 9000), 700), 1)

    def test_leftovers_clear_at_their_own_rate(self):
        # A passed cohort is all leftovers, sweep or no sweep.
        passed = b.Position(0, 700, 0)
        for pace in (700, 0):
            self.assertAlmostEqual(b.sweep_chance(10, 0.01, passed, pace, 0.05),
                                   1 - 0.95 ** 10)
        self.assertAlmostEqual(b.sweep_chance(10, 0.01, passed, 700),
                               1 - 0.99 ** 10)
        # Never slower than the trickle.
        self.assertAlmostEqual(b.sweep_chance(10, 0.01, passed, 700, 0.001),
                               1 - 0.99 ** 10)
        # Arrives at 7, done at 7 + 980 / 700 = 8.4; the 30% left then clear
        # at the leftover rate, and nothing before that changes.
        pos = b.Position(4900, 1400, 980)
        self.assertAlmostEqual(b.sweep_chance(10, 0, pos, 700, 0.1),
                               1 - 0.3 * 0.9 ** 1.6)
        self.assertAlmostEqual(b.sweep_chance(8, 0, pos, 700, 0.1), 0.5)
        self.assertAlmostEqual(b.months_to_chance(0.75, 0, pos, 700, 0.1),
                               8.4 + math.log(0.25 / 0.3) / math.log(0.9))

    def test_arrival_counts_the_trickle_on_the_work_ahead(self):
        pos = b.Position(4900, 1400, 980)
        rate = -math.log(0.98)
        self.assertAlmostEqual(b.arrival(0.02, pos, 700),
                               math.log(1 + 4900 * rate / 700) / rate)
        self.assertLess(b.arrival(0.02, pos, 700), 7)
        self.assertEqual(b.arrival(0.0, pos, 700), 7)
        self.assertEqual(b.arrival(-0.1, pos, 700), 7)
        self.assertEqual(b.arrival(0.02, pos, 0), float("inf"))
        self.assertEqual(b.arrival(0.02, b.Position(-5, 1400, 980), 0), 0)
        self.assertEqual(b.arrival(1.0, pos, 0), 0)
        # A month after it arrives, the sweep has taken 700 of whoever the
        # trickle left by then, up to the 70% cap.
        arrive = b.arrival(0.02, pos, 700)
        there = 1400 * 0.98 ** arrive
        self.assertAlmostEqual(b.sweep_chance(arrive + 0.5, 0.02, pos, 700),
                               1 - 0.98 ** (arrive + 0.5) * (1 - 350 / there))
        self.assertAlmostEqual(b.sweep_chance(arrive + 5, 0.02, pos, 700),
                               1 - 0.98 ** (arrive + 5) * 0.3)
        self.assertEqual(b.sweep_chance(3, 1.0, pos, 700), 1)

    def test_months_to_chance(self):
        pos = b.Position(4900, 1400, 980)
        self.assertAlmostEqual(b.months_to_chance(0.5, 0, pos, 700), 8)
        self.assertAlmostEqual(b.months_to_chance(0.5, 0.02, b.Position(0, 0, 0), 0),
                               math.log(0.5) / math.log(0.98))
        self.assertEqual(b.months_to_chance(0.75, 0, pos, 700), float("inf"))
        self.assertEqual(b.months_to_chance(0.5, 0, pos, 0), float("inf"))
        self.assertAlmostEqual(b.months_to_chance(0, 0, pos, 0), 0)

    def test_sweep_level(self):
        months = [f"2026-{m:02d}" for m in range(1, 8)]
        ahead = dict(zip(months, (1200, 1100, 1100, 1000, 900, 500, 500)))
        own = dict(zip(months, (1400, 1400, 1400, 1400, 1300, 1300, 1000)))
        # The first cohort: share of its size (largest count from SIZE_FROM).
        self.assertEqual(b.sweep_level(ahead, None, None, "2026-07", 0.5), 550)
        # The next: share of its count the month before the one ahead was
        # first down to its level (2026-06), so 2026-05's 1,300.
        self.assertEqual(b.sweep_level(own, ahead, 550, "2026-07", 0.5), 650)
        self.assertEqual(b.sweep_level(own, ahead, 550, "2026-07", 0.3), 390)
        # Not reached by `last`, or no level ahead: not reached at all.
        self.assertIsNone(b.sweep_level(own, ahead, 550, "2026-05", 0.5))
        self.assertIsNone(b.sweep_level(own, ahead, None, "2026-07", 0.5))
        # Reached at the first counted period: no count before it, so its size.
        self.assertEqual(b.sweep_level(own, ahead, 2000, "2026-07", 0.5), 700)
        self.assertIsNone(b.sweep_level({}, None, None, "2026-07", 0.5))
        # One rule either side of arrival: behind the sweep in 2026-05 it
        # would take 650; in it in 2026-06, with the count unchanged, the
        # same 650.
        self.assertEqual(b.sweep_left(own, "2026-05", 0.5, behind=True), 650)
        self.assertEqual(b.sweep_left(own, "2026-06", 0.5, level=650), 650)
        self.assertEqual(b.sweep_left(own, "2026-07", 0.5, level=650), 350)

    def test_passed_needs_two_months(self):
        def at(figures):
            return lambda cohorts, name, period: figures.get(period)
        done, left = b.Position(0, 500, 0), b.Position(0, 700, 150)
        for figures, passed in (({"2026-10": done, "2026-09": left}, False),
                                ({"2026-10": done, "2026-09": done}, True),
                                ({"2026-10": done}, True),
                                ({"2026-09": done}, False),
                                ({"2026-10": left, "2026-09": done}, False)):
            with unittest.mock.patch.object(b, "position", side_effect=at(figures)):
                self.assertEqual(b.passed_by({}, "2025-01", "2026-10"), passed,
                                 figures)
        # The label follows passed_by() when given it.
        self.assertEqual(b.arrival_text(0.0, done, 100.0, False), "now")
        self.assertEqual(b.arrival_text(0.0, done, 100.0, True), "passed")
        self.assertEqual(b.arrival_text(0.0, done, 100.0), "passed")

    def test_cohort_size_and_sweep_left(self):
        # 2025-11 as recorded: the C-3 jump, then steady from 2026-04.
        waiting = {"2026-01": 12600.0, "2026-02": 14100.0, "2026-03": 4200.0,
                   "2026-04": 2900.0, "2026-05": 2700.0, "2026-06": 2600.0,
                   "2026-07": 2600.0, "2026-11": 2400.0}
        # From when its bulge settled: the 4,200 in 2026-03 doesn't count.
        self.assertEqual(b.cohort_size(waiting), 2900)
        self.assertEqual(b.sweep_left(waiting, "2026-11"),
                         2400 - b.STRAGGLERS * 2900)
        self.assertEqual(b.sweep_left(waiting, "2026-11", 0.3), 2400 - 0.3 * 2900)
        self.assertEqual(b.sweep_left(waiting, "2026-11", 0.0), 2400)
        # Settled before SIZE_FROM: SIZE_FROM is the floor.
        early = {"2026-01": 9000.0, "2026-02": 8900.0, "2026-03": 2000.0,
                 "2026-04": 1900.0, "2026-05": 1900.0, "2026-06": 1900.0}
        self.assertEqual(b.settled_from({k: v for k, v in early.items()
                                         if k < "2026-03"} | {"2026-03": 8800.0,
                                                               "2026-04": 8700.0}),
                         "2026-01")
        settled_early = {"2026-01": 9000.0, "2026-02": 8900.0, "2026-03": 8800.0,
                         "2026-04": 8700.0, "2026-05": 8600.0}
        self.assertEqual(b.cohort_size(settled_early), 8800)
        self.assertIsNone(b.cohort_size({"2026-02": 5.0}))
        # Settled, but every count is from before SIZE_FROM.
        before = {"2025-06": 900.0, "2025-07": 900.0, "2025-08": 900.0,
                  "2025-09": 900.0}
        self.assertIsNone(b.cohort_size(before))
        self.assertEqual(b.sweep_left(before, "2025-09"), 0)
        self.assertEqual(b.sweep_left({"2026-02": 5.0}, "2026-02"), 0)
        self.assertEqual(b.sweep_left(waiting, "2026-12"), 0)
        # Already down to its stragglers: nothing left to sweep.
        down = {"2026-03": 2000.0, "2026-04": 1900.0, "2026-05": 1800.0,
                "2026-06": 1700.0, "2026-11": 500.0}
        self.assertEqual(b.sweep_left(down, "2026-11", 0.3), 0)

    def test_position(self):
        # The sweep is in 2025-05: it leaves 30% of its 1,800.
        self.assertEqual(b.position(FIFO_COHORTS, "2025-05", "2026-10", 0.3),
                         b.Position(0, 1500, 1500 - 0.3 * 1800))
        # 2025-06 waits behind 2025-05's unswept 960, and the sweep will take
        # 70% of whoever is waiting in it when it arrives; 2025-07 behind both.
        self.assertEqual(b.position(FIFO_COHORTS, "2025-06", "2026-10", 0.3),
                         b.Position(960, 1500, 0.7 * 1500))
        self.assertEqual(b.position(FIFO_COHORTS, "2025-07", "2026-10", 0.3),
                         b.Position(960 + 0.7 * 1500, 2200, 0.7 * 2200))
        # The default share is STRAGGLERS, for the work ahead and the take.
        self.assertEqual(b.position(FIFO_COHORTS, "2025-07", "2026-10"),
                         b.Position((1500 - 900) + 0.5 * 1500, 2200, 0.5 * 2200))
        self.assertIsNone(b.position(FIFO_COHORTS, "2025-08", "2026-10"))
        self.assertIsNone(b.position(FIFO_COHORTS, "2025-07", "2026-11"))
        # Before SUMMARY_FROM everyone is a straggler: no work, no take.
        old = {"2024-11": [("2026-10", 100.0), ("2026-03", 400.0)],
               "2024-12": [("2026-10", 600.0), ("2026-03", 900.0)],
               "2025-01": [("2026-10", 1000.0), ("2026-03", 1500.0)]}
        self.assertEqual(b.position(old, "2024-12", "2026-10"),
                         b.Position(0, 400, 0))
        self.assertEqual(b.position(old, "2024-11", "2026-10"),
                         b.Position(0, 500, 0))

    def test_paces(self):
        names = ["2025-05", "2025-06", "2025-07"]
        paces = b.fifo_paces(FIFO_COHORTS, names, "2026-10", 0.0)
        self.assertEqual(paces[0], ("no sweep", None))
        self.assertEqual(paces[1][0], "sweep, recent 3 mo")
        self.assertAlmostEqual(paces[1][1], 200 / 3)
        self.assertEqual(len(paces), 2)
        # 2026-01 to 2026-04: everyone went 18,300 -> 14,000, less the
        # trickle measured on 2025-06 and 2025-07 then.
        trickle = 1 - (4100 / 4400) ** (1 / 3)
        early = b.fifo_paces(FIFO_COHORTS, names, "2026-04", trickle)
        self.assertAlmostEqual(early[1][1],
                               (4300 - 18300 * (1 - (1 - trickle) ** 3)) / 3)
        self.assertEqual(b.fifo_paces(FIFO_COHORTS, names, "2026-03", 0.0),
                         [("no sweep", None)])

    def test_leftover_percent(self):
        cases = {(0, 100, 50): "50%", (0, 100, 0): "100%", (0, 100, 100): "0%",
                 (0, 0, 0): "n/a", (0, 100, 150): "0%", (0, 100, -5): "100%"}
        for (work, waiting, take), text in cases.items():
            self.assertEqual(b.leftover_percent(b.Position(work, waiting, take)),
                             text)

    def test_fits_and_strips_cleanly_at_every_width(self):
        for width in range(b.MIN_WIDTH, b.MAX_WIDTH + 1):
            with self.subTest(width=width):
                out = run_fifo(FIFO_COHORTS, width)
                self.assertLessEqual(max(map(len, out.splitlines())), width)
                self.assertFalse([ln for ln in out.splitlines() if ln != ln.rstrip()])
                with unittest.mock.patch.object(b, "COLOR", True):
                    colored = run_fifo(FIFO_COHORTS, width)
                self.assertEqual(ANSI.sub("", colored), out)
                for figure in ("6,300", "0.7%", "0.9%", "3.3%", "4y 6m", "9y 6m", "1,350"):
                    self.assertIn(figure, out)

    def test_target_grew_and_never_are_painted(self):
        # 2025-06's own count creeping up by 100 a month (within rounding, so
        # still settled): its rates show as grew.
        june = dict(FIFO_COHORTS["2025-06"])
        creep = {p: june[p] + 1500 + 100 * k
                 for k, p in enumerate(sorted(june))}
        stalled = {**FIFO_COHORTS,
                   "2025-07": sorted(creep.items(), reverse=True),
                   "2025-08": sorted(((p, creep[p] + 2200)
                                      for p in creep), reverse=True)}
        with unittest.mock.patch.object(b, "COLOR", True):
            out = run_fifo(stalled)
            never = run_fifo(FLAT_COHORTS)
        self.assertRegex(out, "\n\033\\[1m  2025-07 ")
        self.assertRegex(out, "\n  2025-06    2,400  \033\\[33m     grew")
        self.assertRegex(never, "\033\\[31m *never")

    # Exact text, as for the summary, so wording and alignment changes show.
    def test_stacked_text(self):
        out = run_fifo(FIFO_COHORTS, STACKED)
        table = out[out.index("    arrives    50%"):out.index("Processing order")]
        self.assertEqual(table, """\
    arrives    50%    75%     90%
  no sweep
          -  6y 6m    13y  21y 7m
  sweep, recent 3 mo, 19 a month
      4y 6m  5y 3m  7y 3m   9y 6m

""")
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
        self.assertIn("against those behind it from 2025-07.", " ".join(out.split()))
        self.assertIn("Last 3 months: 800 ahead and 0 from 2025-07 cleared", out)
        self.assertIn("When will 2025-06 be processed?", out)

    def test_order_with_nothing_cleared(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_order([b.OrderScore(3, 800.0, 0.0, 200.0, 0.0)], "2025-06",
                          ["2025-07"], WIDE)
            b.print_order([], "2025-07", [], WIDE)
        self.assertIn("    Last 3 months: 0 ahead and 0 from 2025-07 cleared, n/a "
                      "ahead against 80%\n      at random. Score n/a (0 random, 1 "
                      "oldest-first): nothing cleared.\n", buf.getvalue())
        self.assertTrue(buf.getvalue().endswith(
            "  No settled cohort is newer than 2025-07, so there's nothing "
            "behind it to\n  compare with.\n"))

    def test_no_settled_cohort(self):
        self.assertIn("No cohort", run_fifo({"2026-09": JULY_COHORTS["2025-07"]}, 60))

    def test_target_without_figures(self):
        for target in ("2025-08", "2027-01"):
            out = run_fifo(FIFO_COHORTS, 60, target)
            self.assertIn(f"""
No {target} own-cohort figure for 2026-10, so there's no
order score or estimate for it.

Processing order
""", out, target)
            self.assertIn("""
Trickle, last 3 mo:   0.9% a month
Leftovers, last 4 mo: 3.3% a month

How the model works
""", out, target)
            self.assertNotIn("be processed?", out, target)

    def test_no_background(self):
        out = run_fifo(FLAT_COHORTS)
        self.assertIn("Trickle, last 3 mo:   0.0% a month", out)
        self.assertIn("Leftovers, last 3 mo: n/a", out)
        self.assertIn("  no sweep                  -        -  never  never  never",
                      out)
        self.assertIn("With no trickle and nothing it has passed clearing, its "
                      "leftovers never clear.", " ".join(out.split()))
        # Only the cohort at the front has settled: nothing to measure the
        # trickle on.
        front = {c: FIFO_COHORTS[c] for c in ("2025-05", "2025-06")}
        out = run_fifo(front, WIDE, "2025-05")
        self.assertIn("Trickle, last 3 mo:   n/a", out)
        self.assertIn("trickle clears 0.0% of every cohort", " ".join(out.split()))
        # A single month has no pace, so only the no-sweep row is left; and a
        # single month can't show a settled bulge.
        one = {"2025-07": [("2026-10", 9300.0)], "2025-08": [("2026-10", 11500.0)]}
        self.assertEqual(b.fifo_paces(one, ["2025-07"], "2026-10", 0.0),
                         [("no sweep", None)])
        self.assertIn("has settled after its bulge yet", run_fifo(one))

    # The tests below were added to kill mutants the ones above let by.
    def test_latest_period_is_the_newest_anywhere(self):
        cohorts = {"2025-05": [("2026-08", 1.0)], "2025-06": [("2026-10", 1.0)],
                   "2025-07": [("2026-09", 1.0)]}
        self.assertEqual(b.latest_period(cohorts), "2026-10")

    def test_order_score_edges(self):
        # Any clearance at all is measurable, however small.
        self.assertEqual(b.OrderScore(3, 1.0, 1.0, 0.0, 0.0).observed(), 1)
        months = ["2026-07", "2026-08", "2026-09", "2026-10"]
        def series(*counts):
            return sorted(zip(months, counts), reverse=True)
        tiny = {"2025-07": series(1.0, 1.0, 1.0, 0.0),
                "2025-08": series(1.0, 1.0, 1.0, 0.0)}
        self.assertEqual(b.order_score(tiny, "2025-07", ["2025-07"], "2026-10", 3),
                         b.OrderScore(3, 1.0, 1.0, 0.0, 0.0, "2025-07"))
        # Fewer files ahead than behind is still a score.
        few = {"2025-07": series(100.0, 70, 30, 0),
               "2025-08": series(600.0, 570, 530, 500)}
        self.assertEqual(b.order_score(few, "2025-07", ["2025-07"], "2026-10", 3),
                         b.OrderScore(3, 100.0, 100.0, 500.0, 0.0, "2025-07"))
        # A newer cohort still bulging when the window opened is left out.
        bulging = {**few, "2025-09": series(700.0, 5000, 2000, 900)}
        self.assertEqual(b.order_score(bulging, "2025-07", ["2025-07", "2025-08"],
                                       "2026-10", 3),
                         b.OrderScore(3, 100.0, 100.0, 500.0, 0.0, "2025-07"))

    def test_sweep_edges(self):
        # One a month through a cohort of one, starting now.
        self.assertAlmostEqual(b.sweep_chance(1, 0, b.Position(0, 1, 0.7), 1), 0.7)
        self.assertAlmostEqual(b.sweep_chance(0.5, 0, b.Position(-5, 1, 0.7), 1), 0.5)
        # A row without a sweep never gets one.
        self.assertEqual(b.estimate_cells(0.0, b.Position(0, 1, 0.7), None),
                         ["-", "never", "never", "never"])
        # ESTIMATE_CAP is where "never" starts: a 90% chance just inside it
        # is a figure, just past it isn't.
        for months, shown in ((1199.4, "99y 11m"), (1200.5, "never")):
            background = 1 - 0.1 ** (1 / months)
            self.assertEqual(b.estimate_cells(background, b.Position(0, 1, 0.7),
                                              None)[3],
                             shown, months)

    def test_estimate_cells(self):
        # Every chance reached, only the first, or none.
        pos = b.Position(4900, 1400, 980)
        self.assertEqual(b.estimate_cells(0.02, b.Position(0, 0, 0), None),
                         ["-", "2y 10m", "5y 9m", "9y 6m"])
        self.assertEqual(b.estimate_cells(0.0, pos, 700.0),
                         ["7m", "8m", "never", "never"])
        self.assertEqual(b.estimate_cells(0.0, pos, None),
                         ["-", "never", "never", "never"])
        # The leftovers' rate reaches the later chances.
        self.assertEqual(b.estimate_cells(0.0, pos, 700.0, 0.1),
                         ["7m", "8m", "10m", "1y 7m"])

    def test_arrival_text(self):
        cases = {(b.Position(0, 100, 0), 700.0): "passed",
                 (b.Position(0, 100, 50), 700.0): "now",
                 (b.Position(0, 100, 50), 0.0): "never",
                 (b.Position(700, 100, 50), -5.0): "never",
                 (b.Position(700, 100, 50), 100.0): "7m",
                 (b.Position(700, 100, 50), None): "-"}
        for (pos, pace), text in cases.items():
            self.assertEqual(b.arrival_text(0.0, pos, pace), text, (pos, pace))

    def test_estimate_rows(self):
        def render(paces, width):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                b.print_estimates(paces, 0.0, b.Position(4900, 1400, 980), width)
            return buf.getvalue()
        paces = [("no sweep", None), ("shrinking", 0.0), ("growing", -50.0),
                 ("sweep, recent 3 mo", 700.0)]
        # Inline down to exactly the width it needs: 2 + 18 + 2 + 7 + 9 + 3 * 7.
        self.assertEqual(render(paces, 59), """\
                      pace/mo  arrives    50%    75%    90%
  no sweep                  -        -  never  never  never
  shrinking                 0    never  never  never  never
  growing                   0    never  never  never  never
  sweep, recent 3 mo      700       7m     8m  never  never
""")
        # Narrower, each label and its pace go on a line above the cells.
        self.assertEqual(render(paces, 58), """\
    arrives    50%    75%    90%
  no sweep
          -  never  never  never
  shrinking, 0 a month
      never  never  never  never
  growing, 0 a month
      never  never  never  never
  sweep, recent 3 mo, 700 a month
         7m     8m  never  never
""")

    def test_table_without_a_latest_figure(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.print_fifo_table([b.CohortRates("earlier", None, (None, None))],
                               "2025-07", WIDE)
        self.assertEqual(buf.getvalue().splitlines()[1],
                         "  earlier      n/a        n/a        n/a")

    def test_comparison_spans_every_newer_cohort(self):
        out = run_fifo(FIFO_COHORTS, WIDE, "2025-05")
        self.assertIn("against those behind it from 2025-06 to 2025-07.",
                      " ".join(out.split()))

    def test_trickle_is_the_recent_window_whatever_the_table_shows(self):
        with unittest.mock.patch.object(b, "OWN_WINDOWS", (1, 3, 6)):
            out = run_fifo(FIFO_COHORTS)
        self.assertIn("\nTrickle, last 3 mo:   0.9% a month\n", out)
        self.assertIn("  cohort   waiting  last 1 mo  last 3 mo  last 6 mo\n", out)

    def test_growing_cohorts_clear_nothing_in_the_model(self):
        # Every cohort's own count grows: the typical rate is negative, and
        # the estimates treat that as no background at all.
        grow = {c: [(p, n * (2 - k / 10)) for (p, n), k in zip(s, range(10))]
                for c, s in FIFO_COHORTS.items()}
        out = run_fifo(grow)
        self.assertRegex(out, r"Trickle, last 3 mo: +-\d+\.\d% a month")
        self.assertIn("trickle clears 0.0% of every cohort", " ".join(out.split()))
        self.assertIn("  no sweep                  -        -  never  never  never",
                      out)

    def test_no_figure_note_is_painted_cleanly(self):
        plain = run_fifo(FIFO_COHORTS, 60, "2025-08")
        with unittest.mock.patch.object(b, "COLOR", True):
            colored = run_fifo(FIFO_COHORTS, 60, "2025-08")
        self.assertEqual(ANSI.sub("", colored), plain)
        self.assertTrue(colored.endswith("\033[36m" + "=" * 60 + "\033[0m\n\n"))

def checked_report(test: unittest.TestCase, cohorts: dict, width: int,
                   target: str = b.DEFAULT_COHORT) -> str:
    """run_fifo(), checked to fit `width` and leak no NaN or inf."""
    out = run_fifo(cohorts, width, target)
    test.assertIn("WHEN EACH COHORT IS PROCESSED", out)
    test.assertNotIn("nan", out.lower())
    test.assertNotIn("inf", out.lower())
    test.assertLessEqual(max(map(len, out.splitlines())), width)
    return out


class FifoSummary(unittest.TestCase):
    # Exact text, as for the other reports, so wording and alignment changes show.
    def test_report_text(self):
        self.assertEqual(run_fifo(FIFO_COHORTS), """
============================================================
WHEN EACH COHORT IS PROCESSED
============================================================
For each cohort from 2025-05, in 2026-10:
- Ahead: people ahead of the cohort. To sweep: those the sweep still has to
  clear before it gets to the cohort. Waiting: the cohort's own applicants
  still waiting.
- Leftover: the share of those waiting that the sweep will leave behind. Blank
  for cohorts it hasn't reached, where it's 50% by assumption.
- % fifo: how close to oldest-first processing has been over the last 3
  months, against the cohorts newer than it. 0% is random order, 100% strictly
  oldest first, below 0% the newer files went first. For the newest cohorts
  it's mostly rounding.
- Estimates: your chance of having been processed by when the sweep arrives
  ('now' if it's in the cohort, 'passed' if it's done with it), by when it
  ends (after that you're a leftover), and by when half the leftovers have
  been processed. 'never' means the sweep or the leftovers aren't moving, '-'
  that there's no estimate.
- All times are how long from 2026-10, in years and months, at the rates over
  the last 3 months.

  cohort   ahead  to sweep  waiting  leftover  % fifo
  2025-05  6,300         0    1,500       60%     64%
  2025-06  7,800       600    1,500              100%
  2025-07  9,300     1,350    2,200               n/a

Estimates:
  cohort      arrives   sweep ends  leftovers half
  2025-05         now   2y 7m  54%      4y 4m  77%
  2025-06  2y 3m  22%  4y 10m  70%      6y 7m  85%
  2025-07  4y 6m  38%   7y 6m  77%      9y 2m  89%

When will 2025-07 be processed?
- 50%, 75% and 90%: months until your chance of having been processed reaches
  that. The 50% is about when the sweep finishes with your cohort.

Of the 9,300 ahead, 1,350 are still to be swept, then the sweep takes 1,100 of
its 2,200 (fewer by then, as the trickle thins both). The rows: no sweep is
the trickle alone, recent the latest 3-month pace.

                      pace/mo  arrives    50%    75%     90%
  no sweep                  -        -  6y 6m    13y  21y 7m
  sweep, recent 3 mo       19    4y 6m  5y 3m  7y 3m   9y 6m

Processing order
Each cohort's own applicants still waiting in 2026-10, and the share of them
cleared per month (compounded) over the last 3 and 6 months. 'earlier' is
everyone who applied before 2025-05.

  cohort   waiting  last 3 mo  last 6 mo
  earlier    6,300       3.5%       4.1%
  2025-05    1,500       2.1%       3.0%
  2025-06    1,500       2.1%       3.0%
  2025-07    2,200       0.0%       0.7%

Trickle, last 3 mo:   0.9% a month
Leftovers, last 4 mo: 3.3% a month

Oldest-first or random?
  No settled cohort is newer than 2025-07, so there's nothing behind it to
  compare with.

How the model works
- A trickle clears 0.9% of every cohort a month, old or new: the pooled rate
  over the last 3 months of the cohorts the sweep hasn't reached.
- An oldest-first sweep clears 19 files a month (last 3 months) on top of the
  trickle. It works through one cohort at a time until 50% of those waiting
  when it got there are left, then moves on. Within a cohort, who it takes is
  random.
- For the cohort it's in now, that 50% is of its count the month before the
  cohort ahead got down to its leftovers (2025-05, the first cohort counted:
  of its largest count since 2026-03, after the C-3 change). A cohort counts
  as passed after 2 months in a row with nothing left for the sweep to take.
- The trickle thins the files ahead too, so the sweep arrives sooner than its
  pace alone suggests.
- Leftovers clear at 3.3% a month (pooled over the last 4 months: everyone
  before 2025-05, plus any cohort the sweep had passed when the window
  opened): half are still waiting after 21 months. So once the sweep has
  passed you, this rate, not your place in the queue, sets how long you wait.
- Cohorts newer than 2025-07 are left out because of the bulge: a new cohort's
  count swings up, sometimes many times over, then falls back over its first
  few months. A cohort is used once it has held steady for 3 months (no rise
  beyond rounding, no fall over 15%).
- The backtest only checks 1-6 months ahead, and the sweep reaches few cohorts
  that soon, so arrival times and everything after them are extrapolation.
""" + "=" * WIDE + "\n\n")

    def test_matches_the_fifo_report(self):
        # The same model and inputs: each cohort's newest arrival is the
        # --fifo recent row's.
        summary = run_fifo(FIFO_COHORTS)
        for target in ("2025-05", "2025-06", "2025-07"):
            fifo = run_fifo(FIFO_COHORTS, WIDE, target)
            line = next(ln for ln in fifo.splitlines()
                        if ln.startswith("  sweep, recent 3 mo "))
            arrive = re.split(r"\s{2,}", line.strip())[2]
            self.assertRegex(summary, f"\n  {target} +{arrive}  ")

    def test_stacked_text(self):
        # Too narrow for either table: a sentence per cohort, then a line
        # per estimate.
        out = run_fifo(FIFO_COHORTS, 46)
        out = out[:out.index("\nWhen will")]
        self.assertEqual(out[out.index("  2025-05  6,300 ahead"):], """\
  2025-05  6,300 ahead (0 to sweep), 1,500
    waiting, 60% leftover, 64% fifo
  2025-06  7,800 ahead (600 to sweep), 1,500
    waiting, 100% fifo
  2025-07  9,300 ahead (1,350 to sweep), 2,200
    waiting, n/a fifo

Estimates:
  2025-05
           arrives  now
        sweep ends  2y 7m  54%
    leftovers half  4y 4m  77%
  2025-06
           arrives  2y 3m  22%
        sweep ends  4y 10m  70%
    leftovers half  6y 7m  85%
  2025-07
           arrives  4y 6m  38%
        sweep ends  7y 6m  77%
    leftovers half  9y 2m  89%
""")

    def test_tables_down_to_exactly_their_width(self):
        counts, estimates = "  cohort   ahead", "  cohort      arrives"
        self.assertIn(counts, run_fifo(FIFO_COHORTS, 53))
        self.assertNotIn(counts, run_fifo(FIFO_COHORTS, 52))
        self.assertIn(estimates, run_fifo(FIFO_COHORTS, 50))
        self.assertNotIn(estimates, run_fifo(FIFO_COHORTS, 49))

    def test_target_bold_and_never_red(self):
        with unittest.mock.patch.object(b, "COLOR", True):
            out = run_fifo(FIFO_COHORTS, WIDE, "2025-06")
            never = run_fifo(FLAT_COHORTS)
        self.assertIn("\033[1m  2025-06\033[0m  \033[1m7,800\033[0m", out)
        self.assertNotIn("\033[1m  2025-07", out)
        self.assertRegex(never, "\033\\[31m *never")

    def test_no_settled_cohort(self):
        self.assertEqual(run_fifo({}, 60), """
============================================================
WHEN EACH COHORT IS PROCESSED
============================================================
No cohort from 2025-01 on in flpt_history.csv has settled
after its bulge yet.
""")

    def test_cohorts_without_a_latest_figure_are_skipped(self):
        # 2025-06 stopped a month early: no own count for 2026-10 for it or
        # 2025-05, so only 2025-07 is left.
        short = {**FIFO_COHORTS, "2025-06": FIFO_COHORTS["2025-06"][1:]}
        out = run_fifo(short)
        out = out[:out.index("Processing order")]  # its table lists every cohort
        self.assertIn("  2025-07 ", out)
        self.assertNotIn("  2025-06 ", out)
        self.assertNotIn("  2025-05 ", out)
        none = {**short, "2025-07": FIFO_COHORTS["2025-07"][1:]}
        self.assertIn("No settled cohort has figures for 2026-10.\n",
                      run_fifo(none))

    def test_sweep_split(self):
        inf = float("inf")
        # Swept from now: half of 1,000 at 100 a month is 5 months, 50% by
        # then; the leftovers then halve in ln 0.5 / ln 0.9 months.
        pos = b.Position(0, 1000, 500)
        split = b.sweep_split(0.0, pos, 100.0, 0.1)
        self.assertEqual(split[:2], (5.0, 0.5))
        self.assertAlmostEqual(split.half, 5 + math.log(0.5) / math.log(0.9))
        # Passed: it ends now, with no chance yet; leftovers from now.
        passed = b.sweep_split(0.0, b.Position(0, 500, 0), 100.0, 0.1)
        self.assertEqual(passed[:2], (0.0, 0.0))
        self.assertAlmostEqual(passed.half, math.log(0.5) / math.log(0.9))
        # No sweep never ends; nothing clearing leftovers never halves them;
        # leftovers all clearing at once halve as the sweep ends.
        self.assertEqual(b.sweep_split(0.01, pos, 0.0), (inf, 0.0, inf))
        self.assertEqual(b.sweep_split(0.0, b.Position(0, 0, 0), 100.0, 0.1)[0], 0.0)
        self.assertEqual(b.sweep_split(0.0, b.Position(100, 0, 0), 100.0),
                         (inf, 0.0, inf))
        self.assertEqual(b.sweep_split(0.0, pos, 100.0).half, inf)
        self.assertEqual(b.sweep_split(0.0, pos, 100.0, 1.0).half, 5.0)

    def test_split_cells(self):
        O = b.Odds
        pos, gone = b.Position(0, 1000, 500), b.Position(0, 500, 0)
        fast, slow = b.Rates(0.0, 0.1, 100.0), b.Rates(0.0, 0.05, 50.0)
        self.assertEqual(b.split_cells(fast, pos),
                         [O("now", ""), O("5m", "50%"), O("1y", "75%")])
        self.assertEqual(b.split_cells(slow, pos),
                         [O("now", ""), O("10m", "50%"), O("2y", "75%")])
        self.assertEqual(b.split_cells(fast, gone),
                         [O("passed", ""), O("-", ""), O("7m", "50%")])
        self.assertEqual(b.split_cells(fast, gone, False)[:2],
                         [O("now", ""), O("<1m", "0%")])
        self.assertEqual(b.split_cells(b.Rates(0.0, 0.1, None), pos), [O("-", "")] * 3)
        self.assertEqual(b.split_cells(b.Rates(0.0, 0.1, 0.0), b.Position(9, 1, 1)),
                         [O("never", ""), O("never", ""), O("-", "")])
        # Leftovers that never clear have no chance to read at "never".
        self.assertEqual(b.split_cells(b.Rates(0.0, 0.0, 100.0), gone)[2],
                         O("never", ""))
        # Nobody of its own left to take: the sweep never ends with it.
        self.assertEqual(b.split_cells(fast, b.Position(100, 0, 0))[1:],
                         [O("never", ""), O("never", "")])
        behind = b.Position(1400, 1000, 500)
        arrive = b.arrival_months(fast, behind)
        self.assertEqual(b.split_cells(fast, behind)[0],
                         O(b.fmt_duration(arrive),
                           b.fmt_share(b.sweep_chance(arrive, 0.0, behind, 100.0, 0.1))))
        self.assertEqual(b.fmt_share(0.614), "61%")

    def test_chances_rise_along_the_row(self):
        # Each time is later than the last, so the chance by it never falls.
        names = b.settled_cohorts(FIFO_COHORTS)
        last = b.latest_period(FIFO_COHORTS)
        for row in b.fifo_summary_rows(FIFO_COHORTS, names, last,
                                       b.model_rates(FIFO_COHORTS, names, last)):
            shown = [int(o.chance[:-1]) for o in row.split if o.chance]
            self.assertEqual(shown, sorted(shown), row.cohort)

    def test_odds_text(self):
        self.assertEqual(b.odds_text(b.Odds("2028-04", "22%")), "2028-04  22%")
        self.assertEqual(b.odds_text(b.Odds("2028-04", "5%")), "2028-04   5%")
        self.assertEqual(b.odds_text(b.Odds("now", "")), "now")

    def test_arrival_months(self):
        inf = float("inf")
        behind = b.Position(1400, 1000, 500)
        fast, slow = b.Rates(0.01, 0.04, 200.0), b.Rates(0.01, 0.02, 100.0)
        self.assertEqual(b.arrival_months(fast, behind), b.arrival(0.01, behind, 200))
        self.assertEqual(b.arrival_months(b.Rates(0.01, 0.04, None), behind), inf)
        self.assertEqual(b.arrival_months(b.Rates(0.01, 0.04, -5.0), behind), inf)

    def test_estimate_items(self):
        self.assertTrue(b.estimate_items((0.5, 0.75, 0.9))[0].startswith(
            "50%, 75% and 90%: months"))
        self.assertTrue(b.estimate_items((0.5, 0.9))[0].startswith("50% and 90%: months"))
        self.assertTrue(b.estimate_items((0.5,))[0].startswith("50%: months"))
        self.assertEqual(b.odds_heads((0.5, 0.9)), ("arrives", "50%", "90%"))

    def test_leftover_prose(self):
        self.assertIn("never clear", b.leftover_prose(0.0, 0.0, "2025-01"))
        self.assertIn("clear at the trickle (they haven't lately cleared faster): "
                      "half are still waiting after 69 months",
                      b.leftover_prose(0.01, 0.01, "2025-01"))
        self.assertIn("4.1% a month (pooled over the last 3 months: everyone "
                      "before 2025-01", b.leftover_prose(0.014, 0.041, "2025-01"))
        self.assertIn("over the last 5 months:",
                      b.leftover_prose(0.014, 0.041, "2025-01", 5))

    def test_bullets(self):
        self.assertEqual(b.bullets(["one two three", "four"], 9),
                         "- one two\n  three\n- four")

    def test_fifo_percent(self):
        cases = {None: "n/a", 0.76: "76%", -0.2: "-20%", 0.0: "0%",
                 9.99: "999%", 50.0: "999%", -50.0: "-999%"}
        for score, text in cases.items():
            self.assertEqual(b.fifo_percent(score), text, score)

    def test_unmeasurable_order_is_na(self):
        names = ["2025-05", "2025-06", "2025-07"]
        rows = b.fifo_summary_rows(FLAT_COHORTS, names, "2026-10",
                                   b.model_rates(FLAT_COHORTS, names, "2026-10"))
        self.assertEqual([r.fifo for r in rows], ["n/a"] * 3)
        self.assertEqual([o.when for o in rows[1].split], ["never", "never", "-"])
        # Leftover shows only where the sweep is in or past the cohort.
        self.assertEqual([r.leftover for r in rows], ["50%", "", ""])


class Widget(unittest.TestCase):
    def test_feed_matches_the_summary_row(self):
        names = b.settled_cohorts(FIFO_COHORTS)
        last = b.latest_period(FIFO_COHORTS)
        target = names[1]
        with b.dates_from(last):
            row = next(r for r in b.fifo_summary_rows(
                FIFO_COHORTS, names, last,
                b.model_rates(FIFO_COHORTS, names, last))
                if r.cohort == target)
        feed = b.widget_feed(FIFO_COHORTS, target)
        self.assertEqual(feed["version"], 3)
        self.assertEqual(feed["title"], f"Citizenship {target}")
        self.assertEqual(feed["period"], last)
        self.assertEqual([r["label"] for r in feed["rows"]],
                         ["Sweep arrives", "Sweep done", "Leftovers half"])
        self.assertEqual([o.chance for o in row.split],
                         [r["value"] for r in feed["rows"]])
        self.assertEqual([f"by {b.short_month(o.when)}" for o in row.split],
                         [r["detail"] for r in feed["rows"]])
        self.assertRegex(feed["rows"][1]["detail"], r"^by \d{1,2}'\d{2}$")
        self.assertEqual(set(feed["rows"][0]), {"label", "value", "detail"})
        self.assertIsNone(b.DATES_FROM)

    def test_widget_row(self):
        self.assertEqual(b.widget_row("Sweep done", b.Odds("2028-09", "64%")),
                         {"label": "Sweep done", "value": "64%", "detail": "by 9'28"})
        self.assertEqual(b.widget_row("Sweep arrives", b.Odds("now", "")),
                         {"label": "Sweep arrives", "value": "now", "detail": ""})

    def test_short_month(self):
        for text, want in (("2028-01", "1'28"), ("2030-12", "12'30"),
                           ("2100-04", "4'00"), ("64%", "64%"), ("now", "now"),
                           ("-", "-"), ("1y 5m", "1y 5m"), ("12345-01", "12345-01")):
            self.assertEqual(b.short_month(text), want, text)

    def test_dates_from_restores_the_previous_value(self):
        with b.dates_from("2025-01"):
            with b.dates_from(None):
                self.assertIsNone(b.DATES_FROM)
            self.assertEqual(b.DATES_FROM, "2025-01")
            with self.assertRaises(ValueError), b.dates_from("2026-01"):
                raise ValueError
            self.assertEqual(b.DATES_FROM, "2025-01")
        self.assertIsNone(b.DATES_FROM)

    def test_no_feed_for_an_unsettled_or_unlisted_cohort(self):
        self.assertIsNone(b.widget_feed(FIFO_COHORTS, "2019-01"))
        self.assertIsNone(b.widget_feed({}, "2025-07"))
        first = b.settled_cohorts(FIFO_COHORTS)[0]
        with unittest.mock.patch.object(b, "fifo_summary_rows", return_value=[]):
            self.assertIsNone(b.widget_feed(FIFO_COHORTS, first))

    def test_write_widget_writes_or_skips(self):
        target = b.settled_cohorts(FIFO_COHORTS)[0]
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(b, "load_cohorts", return_value=FIFO_COHORTS):
            out = Path(d) / "w.json"
            self.assertTrue(b.write_widget(Path(d) / "h.csv", out, target))
            self.assertEqual(json.loads(out.read_text()),
                             b.widget_feed(FIFO_COHORTS, target))
            self.assertEqual([p.name for p in Path(d).iterdir()], ["w.json"])
            self.assertFalse(b.write_widget(Path(d) / "h.csv",
                                            Path(d) / "missing" / "w.json", target))
            self.assertFalse(b.write_widget(Path(d) / "h.csv", out, "2019-01"))
            with unittest.mock.patch.object(b, "WIDGET_PATH", Path(d) / "x" / "w.json"):
                self.assertFalse(b.write_widget(Path(d) / "h.csv", cohort=target))


class EstimatesFeed(unittest.TestCase):
    def test_feed_is_the_short_table_as_data(self):
        feed = b.estimates_feed(FIFO_COHORTS, "2025-06")
        self.assertEqual(feed, {
            "version": 2, "period": "2026-10", "updated": None, "cohort": "2025-06",
            "model": {"trickle": 0.9, "leftover": 3.3, "leftover_half": 21,
                      "leftover_months": 4, "window_months": 3, "sweep_share": 50,
                      "fifo_score": 1.0, "since": "2026-01"},
            "heads": ["arrives", "sweep ends", "leftovers half"],
            "rows": [
                {"cohort": "2025-05", "cells": [
                    {"when": "now", "chance": None},
                    {"when": "2029-05", "chance": 54},
                    {"when": "2031-02", "chance": 77}]},
                {"cohort": "2025-06", "cells": [
                    {"when": "2029-01", "chance": 22},
                    {"when": "2031-08", "chance": 70},
                    {"when": "2033-05", "chance": 85}]},
                {"cohort": "2025-07", "cells": [
                    {"when": "2031-04", "chance": 38},
                    {"when": "2034-04", "chance": 77},
                    {"when": "2035-12", "chance": 89}]}]})
        self.assertIsNone(b.DATES_FROM)

    def test_cells_read_back_as_the_short_cells(self):
        # Agreement with the table it stands for: each cell, drawn as
        # odds_text() draws it, is the --short cell.
        with b.dates_from(b.latest_period(FIFO_COHORTS)):
            want = [(r.cohort, [b.odds_text(o) for o in r.split]) for r in
                    b.fifo_summary_rows(FIFO_COHORTS, b.settled_cohorts(FIFO_COHORTS),
                                        "2026-10", b.model_rates(
                                            FIFO_COHORTS, b.settled_cohorts(FIFO_COHORTS),
                                            "2026-10"))]
        got = [(r["cohort"], [b.odds_text(b.Odds(c["when"],
                                                  "" if c["chance"] is None
                                                  else f"{c['chance']}%"))
                              for c in r["cells"]])
               for r in b.estimates_feed(FIFO_COHORTS)["rows"]]
        self.assertEqual(got, want)

    def test_model_figures_match_the_report(self):
        # The page quotes these, so they must be the report's own figures.
        names = b.settled_cohorts(FIFO_COHORTS)
        rates = b.model_rates(FIFO_COHORTS, names, "2026-10")
        model = b.estimates_feed(FIFO_COHORTS, "2025-06")["model"]
        self.assertEqual(f"{model['trickle']:.1f}%", b.fmt_pct(rates.background))
        self.assertEqual(f"{model['leftover']:.1f}%", b.fmt_pct(rates.leftover))
        self.assertEqual(f"{model['leftover_half']} months", b.half_life(rates.leftover))
        score = b.order_score(FIFO_COHORTS, "2025-06", ["2025-07"], "2026-10", 3)
        self.assertEqual(b.fifo_percent(model["fifo_score"]),
                         b.fifo_percent(score.score()))

    def test_model_with_nothing_clearing_or_no_score(self):
        names = b.settled_cohorts(FIFO_COHORTS)
        with unittest.mock.patch.object(b, "model_rates",
                                        return_value=b.Rates(0.0, 0.0, None)):
            model = b.model_feed(FIFO_COHORTS, names, "2026-10", "2025-07")
        self.assertIsNone(model["leftover_half"])
        self.assertEqual(model["leftover_months"], b.RECENT_MONTHS)
        self.assertIsNone(model["fifo_score"])   # nothing newer to compare
        self.assertIsNone(b.estimates_feed({})["model"])

    def test_feed_without_history_or_settled_cohorts(self):
        self.assertEqual(b.estimates_feed({})["period"], None)
        self.assertEqual(b.estimates_feed({})["rows"], [])
        early = {c: s for c, s in FIFO_COHORTS.items() if c < "2025-05"}
        self.assertEqual(b.estimates_feed(early)["rows"], [])

    def test_main_json(self):
        with tempfile.TemporaryDirectory() as d:
            history = MainReport().history(d, "bad,row,x\n")
            out = run_main(history, ("--json", "2025/6"))
        self.assertEqual(json.loads(out), b.estimates_feed(FIFO_COHORTS, "2025-06"))
        self.assertNotIn("Skipped", out)

    def test_feed_updated_is_the_last_date_recorded(self):
        head = ("period,cohort,people_ahead,wait_months,people_ahead_text,"
                "wait_time_text,flpt_lastupdated,observed_at\n")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.csv"
            self.assertIsNone(b.feed_updated(path))
            path.write_text(head + "2026-01,2025-07,15800,4,,,,seed\n")
            self.assertIsNone(b.feed_updated(path))
            path.write_text(head + "2026-10,2025-07,1,,,,\"October 9, 2026\",x\n"
                            "2026-11,2025-07,1,,,,\"November 17, 2026\",x\n"
                            "2026-11,2025-08,1,,,, ,x\n")
            self.assertEqual(b.feed_updated(path), "November 17, 2026")
            self.assertEqual(json.loads(run_main(path, ("--json",)))["updated"],
                             "November 17, 2026")


class MainReport(unittest.TestCase):
    def history(self, d: str, extra: str = "") -> Path:
        return write_history(d, "".join(f"{p},{c},{n:.0f}\n"
                                        for c, series in FIFO_COHORTS.items()
                                        for p, n in series) + extra)

    def test_reports_the_default_or_named_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_main(self.history(d))
            other = run_main(self.history(d), ("2025/6",))
        self.assertIn("WHEN EACH COHORT IS PROCESSED", out)
        self.assertIn("When will 2025-07 be processed?", out)
        self.assertIn("When will 2025-06 be processed?", other)

    def test_short_is_only_the_estimates_table(self):
        with tempfile.TemporaryDirectory() as d:
            short = run_main(self.history(d), ("--short",))
            full = run_main(self.history(d))
        self.assertEqual(short, """\
  cohort        arrives    sweep ends  leftovers half
  2025-05           now  2029-05  54%    2031-02  77%
  2025-06  2029-01  22%  2031-08  70%    2033-05  85%
  2025-07  2031-04  38%  2034-04  77%    2035-12  89%
""")
        self.assertIn("Estimates:\n" + short, full)

    def test_short_without_figures_says_so(self):
        self.assertIn("No cohort from 2025-01 on", run_short({}))
        short = {**FIFO_COHORTS, "2025-06": FIFO_COHORTS["2025-06"][1:],
                 "2025-07": FIFO_COHORTS["2025-07"][1:]}
        self.assertEqual(run_short(short),
                         "No settled cohort has figures for 2026-10.\n")

    def test_short_bolds_the_cohort(self):
        with unittest.mock.patch.object(b, "COLOR", True):
            out = run_short(FIFO_COHORTS, "2025-06")
        bold = [ln for ln in out.splitlines() if "\033[1m" in ln]
        self.assertEqual(len(bold), 1)
        self.assertIn("2025-06", bold[0])

    def test_dates_by_default_durations_on_request(self):
        def durations(text):
            """Every "1y 5m", "2y" or "7m" cell, in months."""
            return [12 * int(y or 0) + int(m or n or 0) for y, m, n in re.findall(
                r"(?<![\w-])(?:(\d+)y(?: (\d+)m)?|(\d+)m)\b", text)]
        with tempfile.TemporaryDirectory() as d:
            for flags, table in (((), "pace/mo"), (("--short",), "leftovers half")):
                dates = run_main(self.history(d), flags)
                spans = run_main(self.history(d), flags + ("--durations",))
                # Every duration cell is that many months after 2026-10.
                months = durations(spans.split(table)[-1])
                self.assertTrue(months, flags)
                self.assertFalse(durations(dates.split(table)[-1]), flags)
                for n in months:
                    self.assertIn(b.month_label(b.month_index("2026-10") + n),
                                  dates, (flags, n))
            full = " ".join(run_main(self.history(d)).split())
            self.assertIn("as the month (YYYY-MM) they come to, counted from "
                          "2026-10", full)
            self.assertIn("All times are how long from 2026-10, in years and "
                          "months", " ".join(run_main(self.history(d),
                                                       ("--durations",)).split()))
        self.assertIsNone(b.DATES_FROM)     # reset once the report is out

    def test_rejected_rows_are_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            clean = run_main(self.history(d))
            body = self.history(d, "Sept,2025-07,9000\n2026-06,2025-07,nan\n")
            lines = len(body.read_text().splitlines())
            out = run_main(body)
        self.assertNotIn("Skipped", clean)
        self.assertIn(f"Skipped unreadable rows in flpt_history.csv (lines "
                      f"{lines - 1}, {lines})", out)
        self.assertIn("WHEN EACH COHORT IS PROCESSED", out)

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

    def test_bad_arguments_are_rejected(self):
        for argv, message in ((("July",), "argument cohort: not a YYYY-MM month: 'July'"),
                              (("--fifo",), "unrecognized arguments: --fifo"),
                              (("--summarize",), "unrecognized arguments: --summarize"),
                              (("--interactive",), "unrecognized arguments: --interactive")):
            with tempfile.TemporaryDirectory() as d, \
                    contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit):
                run_main(Path(d) / "none.csv", argv)
            self.assertIn(message, err.getvalue(), argv)

    def test_help_describes_the_flags(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf, \
                self.assertRaises(SystemExit):
            b.parse_args(["--help"])
        text = " ".join(buf.getvalue().split())
        self.assertRegex(text, r"(^| )When each cohort in the processing-times "
                               r"history is likely to be processed\.( |$)")
        self.assertIn("cohort the cohort to show in detail (default 2025-07)", text)
        self.assertIn("--short only the estimates table", text)


def run_short(cohorts: dict, target: str = b.DEFAULT_COHORT) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        b.print_short(cohorts, WIDE, target)
    return buf.getvalue()



class Color(unittest.TestCase):
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


    def test_main_honours_force_and_no_color(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "none.csv"
            plain = run_main(missing)
            forced = run_main(missing, FORCE_COLOR="1")
            self.assertNotIn("\033[", plain)
            self.assertIn("\033[", forced)
            self.assertEqual(ANSI.sub("", forced), plain)
            self.assertNotIn("\033[", run_main(missing, FORCE_COLOR="1",
                                               NO_COLOR="1"))
        self.assertFalse(b.COLOR)


class FuzzPipeline(unittest.TestCase):
    """
    Random histories through the report.

    These counts come from a public feed and hand-edited history, so the
    interesting failures are shapes nobody wrote a case for: queues that grow,
    stall or clear entirely.
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
        chrono.reverse()  # newest first, as load_cohorts() gives a series
        return chrono


    def test_summary_never_breaks(self):
        # Every settled cohort with a latest figure, however odd its counts,
        # gets a row that fits the terminal; nothing raises or leaks a NaN.
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
                out = checked_report(self, cohorts, width)
                last = b.latest_period(cohorts) if any(cohorts.values()) else None
                for cohort in b.settled_cohorts(cohorts):
                    if b.position(cohorts, cohort, last):
                        self.assertIn(f"  {cohort} ", out)

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


class FuzzFifo(unittest.TestCase):
    """Property checks on the --fifo model and report."""

    CASES = FuzzPipeline.CASES

    def test_chance_is_a_rising_probability(self):
        rng = random.Random(20261024)
        for i in range(self.CASES):
            args = (rng.uniform(-0.1, 0.2),
                    b.Position(rng.uniform(-1000, 60000), rng.uniform(-100, 5000),
                               rng.uniform(-500, 6000)),
                    rng.uniform(-500, 3000),
                    rng.choice([None, rng.uniform(-0.1, 0.3)]))
            months = sorted(rng.uniform(0, 300) for _ in range(5))
            with self.subTest(case=i, args=args, months=months):
                chances = [b.sweep_chance(m, *args) for m in months]
                self.assertEqual(chances, sorted(chances))
                for c in chances:
                    self.assertTrue(0 <= c <= 1)
                # Leftovers clearing faster can only help.
                for m, c in zip(months, chances):
                    self.assertGreaterEqual(c + 1e-12, b.sweep_chance(m, *args[:3]))

    def test_split_is_the_chance_at_the_sweeps_end(self):
        # The split is read off sweep_chance(): the chance at the sweep's end,
        # and half of the rest cleared by `half`.
        rng = random.Random(20261122)
        for i in range(self.CASES):
            waiting = rng.uniform(1, 5000)
            args = (rng.choice([0.0, rng.uniform(0.001, 0.1)]),
                    b.Position(rng.choice([0.0, rng.uniform(0, 20000)]), waiting,
                               rng.uniform(0, waiting)),
                    rng.uniform(1, 2000), rng.choice([None, rng.uniform(0, 0.2)]))
            with self.subTest(case=i, args=args):
                split = b.sweep_split(*args)
                self.assertGreaterEqual(split.ends, 0)
                self.assertTrue(0 <= split.done <= 1)
                self.assertAlmostEqual(split.done, b.sweep_chance(split.ends, *args))
                if math.isfinite(split.half):
                    self.assertGreaterEqual(split.half, split.ends)
                    self.assertAlmostEqual(b.sweep_chance(split.half, *args),
                                           1 - (1 - split.done) / 2, places=9)

    def test_durations_read_back_as_the_rounded_months(self):
        rng = random.Random(20261125)
        for i in range(self.CASES):
            months = rng.choice([rng.uniform(1, 60), rng.uniform(1, 1300),
                                 rng.uniform(1, 2e6)])
            with self.subTest(case=i, months=months):
                text = b.fmt_duration(months)
                self.assertLessEqual(len(text), b.SUMMARY_CELL)
                parts = re.fullmatch(r"(?:([\d,]+)y)? ?(?:(\d+)m)?", text)
                if parts is None:                 # e-notation years
                    self.assertRegex(text, r"^\de\+\d+y$")
                    continue
                years = int((parts[1] or "0").replace(",", ""))
                total = 12 * years + int(parts[2] or 0)
                if parts[2] is None and years >= 100:   # months dropped
                    self.assertEqual(total // 12, round(months) // 12)
                else:
                    self.assertEqual(total, round(months))
                    self.assertLess(int(parts[2] or 0), 12)

    def test_dates_count_the_months_from_the_period(self):
        rng = random.Random(20261124)
        for i in range(self.CASES):
            start = b.month_label(rng.randint(2000 * 12, 2100 * 12))
            months = rng.choice([float("inf"), -rng.uniform(0, 5),
                                 rng.uniform(0, 2000), rng.uniform(0, 1e6)])
            with self.subTest(case=i, start=start, months=months), \
                    unittest.mock.patch.object(b, "DATES_FROM", start):
                text = b.fmt_when(months)
                self.assertLessEqual(len(text), b.SUMMARY_CELL)
                if b.month_key(text) == text:
                    self.assertEqual(b.month_index(text) - b.month_index(start),
                                     round(max(months, 0.0)))
                else:
                    self.assertEqual(text, b.fmt_duration(months))

    def test_months_to_chance_inverts_sweep_chance(self):
        rng = random.Random(20261025)
        for i in range(self.CASES):
            waiting = rng.uniform(1, 5000)
            args = (rng.choice([0.0, rng.uniform(0.001, 0.2)]),
                    b.Position(rng.uniform(0, 60000), waiting,
                               rng.uniform(0, waiting)),
                    rng.choice([0.0, rng.uniform(1, 3000)]),
                    rng.choice([None, rng.uniform(0, 0.3)]))
            q = rng.choice(b.ESTIMATE_ODDS)
            with self.subTest(case=i, args=args, q=q):
                m = b.months_to_chance(q, *args)
                if m == float("inf"):
                    self.assertLess(b.sweep_chance(b.ESTIMATE_CAP, *args), q)
                else:
                    self.assertGreaterEqual(b.sweep_chance(m, *args), q)
                    self.assertLess(b.sweep_chance(max(m - 1e-6, 0), *args), q + 1e-9)

    def test_arrival_is_when_the_thinned_work_runs_out(self):
        # The work ahead shrinks by the trickle and the pace each month; at
        # arrival() none is left, and the trickle can only bring it sooner.
        rng = random.Random(20261120)
        for i in range(self.CASES):
            work, pace = rng.uniform(1, 60000), rng.uniform(1, 3000)
            background = rng.uniform(0.0005, 0.2)
            pos = b.Position(work, 1000.0, 500.0)
            with self.subTest(case=i, work=work, pace=pace, background=background):
                t = b.arrival(background, pos, pace)
                self.assertLessEqual(t, work / pace * (1 + 1e-12))
                self.assertAlmostEqual(b.arrival(0.0, pos, pace), work / pace)
                rate = -math.log1p(-background)
                left = work * math.exp(-rate * t) - pace * -math.expm1(-rate * t) / rate
                self.assertAlmostEqual(left / work, 0, places=9)

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

    @staticmethod
    def _chained(rng: random.Random) -> dict:
        """
        Consecutive cohorts built the way the feed is: each one's people
        ahead is the last one's plus that cohort's own count, which is
        mostly steady (so the bulge settles) but sometimes bulges or jumps.
        """
        periods = [b.month_label(2026 * 12 + k) for k in range(rng.randint(2, 11))]
        ahead = [rng.uniform(0, 20000)] * len(periods)
        ahead = [a * (1 - rng.uniform(0, 0.05)) ** k for k, a in enumerate(ahead)]
        cohorts = {}
        first = rng.randint(2024 * 12 + 6, 2025 * 12 + 6)
        for k in range(rng.randint(1, 8)):
            cohorts[b.month_label(first + k)] = sorted(zip(periods, ahead),
                                                       reverse=True)
            own, step = rng.uniform(0, 5000), rng.uniform(-0.2, 0.02)
            owns = [own * (1 + step) ** t for t in range(len(periods))]
            if rng.random() < 0.2:
                owns[rng.randrange(len(owns))] *= rng.uniform(2, 30)
            ahead = [a + o for a, o in zip(ahead, owns)]
        cohorts[b.month_label(first + len(cohorts))] = sorted(zip(periods, ahead),
                                                              reverse=True)
        return cohorts

    def test_report_never_breaks(self):
        rng = random.Random(20261027)
        fuzz = FuzzPipeline()
        estimated = 0
        for i in range(self.CASES):
            if rng.random() < 0.5:
                cohorts = self._chained(rng)
                width = rng.randint(b.MIN_WIDTH, b.MAX_WIDTH)
                target = rng.choice(list(cohorts))
                with self.subTest(case=i, cohorts=cohorts, width=width,
                                  target=target):
                    estimated += self._check_reports(cohorts, width, target)
                continue
            # Mostly consecutive months, with counts that rarely settle.
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
                estimated += self._check_reports(cohorts, width, target)
        # The generator has to reach the estimates, not just the early exits.
        self.assertGreater(estimated, self.CASES // 8)

    def _check_reports(self, cohorts: dict, width: int, target: str) -> bool:
        """The report fits, leaks no NaN or inf; True if it got to the estimates."""
        return "be processed?" in checked_report(self, cohorts, width, target)

if __name__ == "__main__":
    unittest.main()
