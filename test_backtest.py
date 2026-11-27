"""Tests for backtest.py (stdlib only)."""
import contextlib
import io
import random
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import backtest as bt
import burndown as b
from test_burndown import FIFO_COHORTS, FixtureModel, from_own, monthly, write_history


class Backtest(FixtureModel):
    def test_truncate(self):
        cut = bt.truncate({"2025-07": [("2026-10", 1.0), ("2026-08", 2.0)],
                           "2025-08": [("2026-10", 3.0)]}, "2026-09")
        self.assertEqual(cut, {"2025-07": [("2026-08", 2.0)]})

    def test_cases(self):
        all_cases = bt.cases(FIFO_COHORTS, "2026-01")
        # The fixture's own counts are steady from 2026-01, so every cohort
        # with a next one has settled by the first cutoff with three steady
        # months after SIZE_FROM's floor: 2026-04.
        self.assertEqual(sorted({(c.cut, c.cohort) for c in all_cases})[:3],
                         [("2026-04", "2025-05"), ("2026-04", "2025-06"),
                          ("2026-04", "2025-07")])
        one = next(c for c in all_cases
                   if (c.cut, c.horizon, c.cohort) == ("2026-07", 3, "2025-05"))
        # 2025-05's own waiting: 1,600 in 2026-07, 1,500 in 2026-10.
        self.assertAlmostEqual(one.actual, 1 - 1500 / 1600)
        self.assertEqual(b.latest_period(one.seen), "2026-07")
        self.assertTrue(all(c.cut < "2026-10" and c.horizon <= bt.MAX_HORIZON
                            for c in all_cases))
        self.assertEqual(bt.cases(FIFO_COHORTS, "2026-10"), [])

    def test_predictions(self):
        case = next(c for c in bt.cases(FIFO_COHORTS, "2026-01")
                    if (c.cut, c.horizon, c.cohort) == ("2026-07", 3, "2025-05"))
        got = bt.predictions(case)
        self.assertEqual(set(got), {*(f"sweep, {s:.0%} left" for s in bt.SHARES),
                                    "own rate, 3 mo", "random order", "gradient",
                                    "trickle only", "nothing"})
        for value in got.values():
            self.assertTrue(0 <= value <= 1)
        pos = b.position(case.seen, "2025-05", "2026-07", 0.5)
        self.assertEqual(got["sweep, 50% left"],
                         b.sweep_chance(3, case.background, pos, case.pace,
                                        case.leftover))
        self.assertEqual(got["trickle only"], 1 - (1 - case.background) ** 3)
        self.assertEqual(got["nothing"], 0.0)
        self.assertEqual(got["gradient"], bt.gradient_cleared(case))
        # One pace per cutoff: everyone's clearance net of the trickle.
        names = b.settled_cohorts(case.seen)
        self.assertEqual(case.background,
                         b.fifo_background(case.seen, names, "2026-07")[1])
        self.assertEqual(case.pace, max(b.sweep_pace(case.seen, names, "2026-07",
                                                     case.background), 0.0))
        self.assertEqual(case.leftover, b.model_rates(case.seen, names, "2026-07").leftover)
        own = b.own_cohort(case.seen, "2025-05").waiting
        rate = b.window_rate(own, "2026-07", 3)
        self.assertAlmostEqual(got["own rate, 3 mo"], 1 - (1 - rate) ** 3)
        # No people ahead: random order says you're done.
        empty = case._replace(seen={**case.seen, "2025-05": [("2026-07", 0.0)]})
        self.assertEqual(bt.predictions(empty)["random order"], 1.0)

    def test_reaches(self):
        rates = b.Rates(0.0, 0.0, 300.0)
        with unittest.mock.patch.object(bt, "SHARES", (0.5,)):
            # 2025-05 is the cohort the sweep is in; 2025-06 waits behind its
            # 1,500 - 900 = 600 (2 months at 300); 2025-08 has no own count.
            self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-05", "2026-10", rates, 1))
            self.assertFalse(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 2))
            self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 3))
        # At any of SHARES: leaving 60%, the sweep has less to take ahead of
        # 2025-06 and gets there within 2 months; STRAGGLERS doesn't matter.
        self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 2))
        with unittest.mock.patch.object(b, "STRAGGLERS", 0.2):
            self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 2))
        self.assertFalse(bt.reaches(FIFO_COHORTS, "2025-08", "2026-10", rates, 6))
        self.assertFalse(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10",
                                    rates._replace(pace=None), 6))
        self.assertEqual({c.reached for c in bt.cases(FIFO_COHORTS, "2026-01")},
                         {True, False})

    def test_isotonic_falling(self):
        self.assertEqual(bt.isotonic_falling([(0, 3, 1), (1, 2, 1), (2, 1, 1)]),
                         [(0, 3), (1, 2), (2, 1)])
        # A rise is pooled: 1 and 3 at equal weight become 2 and 2.
        self.assertEqual(bt.isotonic_falling([(0, 1, 1), (1, 3, 1)]), [(0, 2), (1, 2)])
        self.assertEqual(bt.isotonic_falling([(0, 1, 3), (1, 5, 1)]), [(0, 2), (1, 2)])
        self.assertEqual(bt.isotonic_falling([(0, 1, 0), (1, 5, 1)]), [(1, 5)])
        self.assertEqual(bt.isotonic_falling([]), [])

    def test_isotonic_properties(self):
        rng = random.Random(20261122)
        for i in range(300):
            points = [(rng.uniform(0, 100), rng.uniform(-1, 1), rng.uniform(0.1, 10))
                      for _ in range(rng.randint(1, 12))]
            with self.subTest(case=i, points=points):
                fit = bt.isotonic_falling(points)
                ys = [y for _, y in fit]
                self.assertEqual([x for x, _ in fit], sorted(x for x, _, _ in points))
                self.assertTrue(all(a >= b - 1e-12 for a, b in zip(ys, ys[1:])))
                # Pooling keeps the weighted mean.
                weights = [w for _, _, w in sorted(points)]
                self.assertAlmostEqual(sum(w * y for w, y in zip(weights, ys)),
                                       sum(w * y for _, y, w in points))

    def test_interpolate(self):
        fit = [(0.0, 0.1), (10.0, 0.05), (10.0, 0.04), (20.0, 0.01)]
        self.assertEqual(bt.interpolate(fit, -5), 0.1)
        self.assertAlmostEqual(bt.interpolate(fit, 5), 0.075)
        self.assertAlmostEqual(bt.interpolate(fit, 15), 0.025)
        self.assertEqual(bt.interpolate(fit, 99), 0.01)
        self.assertEqual(bt.interpolate([(0.0, 0.1), (0.0, 0.2)], 0.0), 0.1)

    def test_gradient(self):
        names = b.settled_cohorts(FIFO_COHORTS)
        queue = bt.groups(FIFO_COHORTS, names)
        self.assertEqual([n for n, _ in queue], ["", *names])
        self.assertEqual(queue[0][1], dict(FIFO_COHORTS["2025-05"]))
        fit = bt.gradient_fit(FIFO_COHORTS, names, "2026-10")
        # The tail (7,000 at 2026-07, middle 3,500) cleared fastest.
        self.assertEqual(fit[0], (3500, 1 - (6300 / 7000) ** (1 / 3)))
        self.assertEqual(len(fit), 4)
        self.assertTrue(all(a[1] >= b2[1] for a, b2 in zip(fit, fit[1:])))
        # A group without a figure when the window opens ends the queue.
        self.assertEqual(bt.gradient_fit(FIFO_COHORTS, names, "2026-03"), [])
        # A group with no figure at the cutoff has no rate, and no point.
        trimmed = {**FIFO_COHORTS, "2025-05": FIFO_COHORTS["2025-05"][1:]}
        self.assertEqual(len(bt.gradient_fit(trimmed, names, "2026-10")), 2)
        case = bt.Case("2026-10", 2, "2025-06", 0.0, FIFO_COHORTS, 0.0, 0.0)
        cleared = bt.gradient_cleared(case)
        self.assertTrue(0 < cleared < 1)
        # A month at the fitted rates, by hand: each group at its place.
        one = bt.gradient_cleared(case._replace(horizon=1))
        ahead = 6300 + 1500
        self.assertAlmostEqual(one, b.unit(bt.interpolate(fit, ahead + 1500 / 2)))
        self.assertEqual(bt.gradient_cleared(case._replace(cohort="2025-09")), 0.0)
        self.assertEqual(bt.gradient_cleared(case._replace(cut="2026-03")), 0.0)

    def test_ahead_pace_is_the_cohorts_own_people_ahead(self):
        # 2025-07's people ahead: 10,200 in 2026-07, 9,300 in 2026-10.
        case = bt.Case("2026-10", 1, "2025-07", 0.0, FIFO_COHORTS, 0.0, 0.0)
        self.assertEqual(bt.ahead_pace(case), 300)
        grew = case._replace(seen={"2025-07": [("2026-10", 900.0), ("2026-09", 500.0)]})
        self.assertEqual(bt.ahead_pace(grew), 0)
        one = case._replace(seen={"2025-07": [("2026-10", 900.0)]})
        self.assertEqual(bt.ahead_pace(one), 0)

    def test_score_and_report(self):
        cases = [bt.Case("2026-07", 1, "x", 0.5, {}, 0.0, 0.0),
                 bt.Case("2026-08", 2, "x", 0.1, {}, 0.0, 0.0)]
        with unittest.mock.patch.object(
                bt, "predictions", side_effect=[{"a": 0.6, "b": 0.5},
                                                {"a": 0.1, "b": 0.4}]):
            scores = bt.score(cases)
        self.assertAlmostEqual(scores["a"].error, 0.05)
        self.assertAlmostEqual(scores["a"].bias, 0.05)
        self.assertEqual(list(scores["b"].by_horizon), [1, 2])
        self.assertAlmostEqual(scores["b"].by_horizon[2], 0.3)
        text = bt.report({**scores, "c": bt.Score(0.2, -0.1, {1: 0.2})}, 2,
                         ["2026-07", "2026-08"])
        self.assertTrue(text.startswith("2 cases: cutoffs 2026-07 to 2026-08, "
                                        "horizons 1-2 months."))
        rows = text.splitlines()[-3:]
        self.assertEqual([r.split()[0] for r in rows], ["a", "b", "c"])
        self.assertEqual(rows[2], "  c                   20.0%  -10.0%  20.0%      -")

    def test_main(self):
        with tempfile.TemporaryDirectory() as d:
            body = "".join(f"{p},{c},{n:.0f}\n" for c, s in FIFO_COHORTS.items()
                           for p, n in s)
            path = write_history(d, body)
            with unittest.mock.patch.object(b, "HISTORY_PATH", path), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(bt.main(["--from", "2026-01"]), 0)
                self.assertEqual(bt.main(["--from", "2026/10"]), 1)
        self.assertIn("cases: cutoffs 2026-04 to 2026-09", out.getvalue())
        self.assertIn("\nWhere the sweep is in or reaches the cohort within the "
                      "horizon:\n", out.getvalue())
        self.assertIn("No cutoff has a settled cohort", out.getvalue())
        self.assertIn("\nLeftover share, measured (STRAGGLERS assumes 50%):\n",
                      out.getvalue())


class MeasuredShares(unittest.TestCase):
    # 2024-12's own counts from 2025-10, as the backfilled history has them.
    SHARP = [1200, 1100, 1200, 1200, 1100, 1000, 600, 400, 200, 200, 300, 200, 300, 200]

    def test_falls(self):
        s = monthly([1000, 760, 1000, 700, 400, 199, 300, 150])
        self.assertFalse(bt.falls(s, "2025-10", "2025-11"))   # 240, but 24%
        self.assertTrue(bt.falls(s, "2025-12", "2026-01"))    # 300, 30%
        self.assertTrue(bt.falls(s, "2026-02", "2026-03"))    # 201, 50%
        self.assertFalse(bt.falls(s, "2026-04", "2026-05"))   # 50%, but 150
        self.assertFalse(bt.falls(monthly([1000, 750]), "2025-10", "2025-11"))  # 25%
        self.assertFalse(bt.falls(monthly([400, 200]), "2025-10", "2025-11"))   # 200

    def test_levelled_off(self):
        s = monthly([600, 400, 200, 200, 300, 200])
        # Calm is 100 here (5% of 1,000 is less): 400 -> 200 isn't.
        self.assertEqual(bt.levelled_off(s, sorted(s), 1000), "2025-12")
        # A slow sweep, 100 a month: each month is calm, the run isn't.
        slow = monthly([900, 800, 700, 600, 500])
        self.assertIsNone(bt.levelled_off(slow, sorted(slow), 1000))
        # A big cohort's calm is 5% of it: 300 of 6,000.
        self.assertEqual(bt.levelled_off(slow, sorted(slow), 6000), "2025-10")
        self.assertIsNone(bt.levelled_off(slow, sorted(slow), 5999))
        # Too few months to tell.
        self.assertIsNone(bt.levelled_off(s, sorted(s)[-3:], 1000))
        # In all only 100, but a month of 200: not calm.
        bump = monthly([400, 200, 300, 300])
        self.assertIsNone(bt.levelled_off(bump, sorted(bump), 1000))
        # Four counts over four months, one missing: not three calm months.
        gap = {"2025-10": 300, "2025-11": 300, "2026-01": 300, "2026-02": 300}
        self.assertIsNone(bt.levelled_off(gap, sorted(gap), 1000))

    def test_sweep_pass(self):
        found = bt.sweep_pass("2024-12", monthly(self.SHARP))
        self.assertEqual(found, bt.Pass("2024-12", "2026-01", 1200, "2026-06", 200))
        self.assertAlmostEqual(found.share(), 1 / 6)
        # A slow sweep: the fall (900 -> 500 by 2026-09) counts from the
        # highest of the PASS_MONTHS before it (the latest of a tie), and it's
        # still falling.
        slow = bt.sweep_pass("2025-01", monthly(
            [1200, 1200, 1100, 1200, 1100, 1100, 1000, 900, 900, 800, 700, 500, 500]))
        self.assertEqual(slow, bt.Pass("2025-01", "2026-03", 1100, None, 500))
        tie = bt.sweep_pass("x", monthly([1000, 1000, 1000, 1000, 600, 300]))
        self.assertEqual(tie.reached, "2026-01")
        # Flat months after the high and before the fall aren't its floor.
        flat_first = bt.sweep_pass("x", monthly([1000, 950, 950, 950, 950, 600, 300,
                                                 300, 300, 300]))
        self.assertEqual(flat_first, bt.Pass("x", "2025-10", 1000, "2026-04", 300))
        # The floor can start where the fall is seen (2024-07's did).
        at_once = bt.sweep_pass("x", monthly([800, 700, 300, 200, 200, 100, 100]))
        self.assertEqual(at_once, bt.Pass("x", "2025-10", 800, "2026-01", 200))
        # The trickle and rounding alone: no pass.
        self.assertIsNone(bt.sweep_pass("x", monthly([1900, 1900, 1800, 1700, 1600,
                                                      1600, 1600, 1500, 1500])))
        self.assertIsNone(bt.sweep_pass("x", monthly([1000, 100])))
        # A fall over four months because one is missing isn't PASS_MONTHS'.
        gap = {"2025-10": 1000, "2025-11": 1000, "2025-12": 1000, "2026-02": 700}
        self.assertIsNone(bt.sweep_pass("x", gap))
        self.assertIsNotNone(bt.sweep_pass("x", {**gap, "2026-01": 1000}))

    def test_measured_passes(self):
        flat = [900.0] * len(self.SHARP)
        small = [400, 400, 400, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
        bulge = [2000, 9000] + [2000.0] * 12
        cohorts = from_own({"2024-11": small, "2024-12": self.SHARP,
                            "2025-01": flat, "2025-02": flat, "2025-03": bulge})
        self.assertEqual(b.own_cohort(cohorts, "2024-12").waiting["2026-06"], 200)
        # 2024-11 is under MIN_BEFORE, 2025-01 and 2025-02 haven't been
        # reached, and 2025-03's fall after 2025-11 is its bulge: it's
        # SETTLE_MONTHS old only in 2025-12.
        self.assertEqual([p.cohort for p in bt.measured_passes(cohorts)], ["2024-12"])
        with unittest.mock.patch.object(bt, "SETTLE_MONTHS", 8):
            self.assertEqual([p.cohort for p in bt.measured_passes(cohorts)],
                             ["2024-12", "2025-03"])
        with unittest.mock.patch.object(bt, "MIN_BEFORE", 1200):
            self.assertEqual(len(bt.measured_passes(cohorts)), 1)

    def test_measured_passes_edges(self):
        # 2025-01 is SETTLE_MONTHS old in 2025-10, its first count, so its
        # fall is measured from there; 2025-02 is exactly MIN_BEFORE (and, read
        # from 2025-11, has too few months after its fall to have levelled).
        cohorts = from_own({"2025-01": [1000, 900, 600, 300, 300, 300, 300],
                            "2025-02": [500, 500, 500, 200, 200, 200, 200]})
        self.assertEqual(bt.measured_passes(cohorts),
                         [bt.Pass("2025-01", "2025-10", 1000, "2026-01", 300),
                          bt.Pass("2025-02", "2025-12", 500, None, 200)])

    def test_pooled_and_bounds(self):
        one = bt.Pass("a", "2026-01", 1000, "2026-05", 200)
        e = 2 * bt.ROUNDING_SD
        self.assertAlmostEqual(e, 81.65, places=2)
        self.assertEqual(one.bounds(), ((200 - e) / (1000 + e), (200 + e) / (1000 - e)))
        both = bt.pooled([one, bt.Pass("b", "2026-02", 800, "2026-06", 100)])
        self.assertEqual(both, bt.Pass("2 pooled", "", 1800, "", 300, 2))
        self.assertAlmostEqual(both.bounds()[1], (300 + e * 2 ** 0.5) / (1800 - e * 2 ** 0.5))
        self.assertEqual(bt.Pass("c", "", 1000, "", 0).bounds()[0], 0)

    def test_bounds_hold_the_share(self):
        rng = random.Random(20261127)
        for i in range(300):
            before = rng.uniform(bt.MIN_BEFORE, 50000)
            p = bt.Pass("x", "", before, "", rng.uniform(0, before), rng.randint(1, 20))
            with self.subTest(case=i, p=p):
                low, high = p.bounds()
                self.assertLessEqual(low, p.share())
                self.assertLessEqual(p.share(), high)

    def test_sweep_pass_properties(self):
        rng = random.Random(20261128)
        for i in range(300):
            counts = [rng.choice(range(0, 3000, 100)) for _ in range(rng.randint(0, 16))]
            series = monthly(counts)
            with self.subTest(case=i, counts=counts):
                found = bt.sweep_pass("x", series)
                periods = sorted(series)
                if found is None:
                    # No fall beyond the trickle and rounding anywhere.
                    self.assertFalse(any(
                        bt.falls(series, a, z) for a, z in
                        zip(periods, periods[bt.PASS_MONTHS:])))
                    continue
                self.assertEqual(found.before, series[found.reached])
                self.assertEqual(found.left, series[found.ended or periods[-1]])
                if found.ended:
                    self.assertGreater(found.ended, found.reached)
        # A trickle never makes a pass: under 2% a month, rounded to 100.
        for i in range(300):
            start, rate = rng.uniform(1000, 30000), rng.uniform(0, 0.02)
            counts = [round(start * (1 - rate) ** k, -2) for k in range(16)]
            with self.subTest(trickle=i, counts=counts):
                self.assertIsNone(bt.sweep_pass("x", monthly(counts)))

    def test_pass_report(self):
        passes = [bt.Pass("2024-11", "2025-11", 800, "2026-04", 200),
                  bt.Pass("2024-12", "2026-01", 1200, "2026-06", 200),
                  bt.Pass("2025-01", "2026-04", 1100, None, 500)]
        self.assertEqual(bt.pass_report(passes, 0.5), "\n".join([
            "Leftover share, measured (STRAGGLERS assumes 50%):",
            "A cohort's own count before the sweep and where it levelled off. "
            "The range allows",
            "for the rounding only (two standard deviations); '<' while still "
            "falling.",
            "",
            "  cohort      reached  before  levelled   left  share     range",
            "  2024-11     2025-11     800   2026-04    200    25%   13%-39%",
            "  2024-12     2026-01   1,200   2026-06    200    17%    9%-25%",
            "  2025-01     2026-04   1,100   falling    500   <45%   35%-57%",
            "  2 pooled              2,000              400    20%   13%-27%"]))
        # One finished pass isn't pooled; none at all says so.
        self.assertNotIn("pooled", bt.pass_report(passes[1:], 0.5))
        self.assertNotIn("none yet", bt.pass_report(passes[2:], 0.5))
        self.assertTrue(bt.pass_report([], 0.3).endswith(
            "(STRAGGLERS assumes 30%):\n"
            "A cohort's own count before the sweep and where it levelled off. "
            "The range allows\n"
            "for the rounding only (two standard deviations); '<' while still "
            "falling.\n"
            "\n  cohort      reached  before  levelled   left  share     range\n"
            "  none yet."))


if __name__ == "__main__":
    unittest.main()
