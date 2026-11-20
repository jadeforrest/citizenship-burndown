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
from test_burndown import FIFO_COHORTS, write_history


class Backtest(unittest.TestCase):
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
        # 2025-05 is the cohort the sweep is in; 2025-06 waits behind its
        # 1,500 - 900 = 600 (2 months at 300); 2025-08 has no own count.
        self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-05", "2026-10", rates, 1))
        self.assertFalse(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 2))
        self.assertTrue(bt.reaches(FIFO_COHORTS, "2025-06", "2026-10", rates, 3))
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


if __name__ == "__main__":
    unittest.main()
