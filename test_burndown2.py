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
import io
import os
import random
import unittest

import burndown2 as b


def chrono_to_processed(chrono: list[float]) -> list[float]:
    """Same month-over-month diff analyze() does, for building test inputs."""
    return [chrono[i] - chrono[i + 1] for i in range(len(chrono) - 1)]


def run_analyze(entered: list[float]) -> str:
    """Call analyze() and capture what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        b.analyze(entered)
    return buf.getvalue()


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

    def test_quantile_header_has_a_cell_per_quantile(self):
        header = b.quantile_header("")
        self.assertEqual(
            len(header), len(b.QUANTILES) * b.CELL + (len(b.QUANTILES) - 1)
        )


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
            with self.subTest(case=i, entered=entered):
                out = run_analyze(entered)
                self.assertTrue(out.strip())
                # A NaN or an unformatted inf leaking into the report means the
                # arithmetic went somewhere the formatters don't handle.
                self.assertNotIn("nan", out.lower())
                self.assertNotIn("inf", out.lower())

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

    def test_parse_round_trips_what_it_formats(self):
        rng = random.Random(20260901)
        for i in range(self.CASES):
            values = [float(rng.randint(0, 60000)) for _ in range(rng.randint(1, 12))]
            raw = ", ".join(str(v) for v in values)
            with self.subTest(case=i, raw=raw):
                self.assertEqual(b.parse_numbers(raw), values)


if __name__ == "__main__":
    unittest.main()
