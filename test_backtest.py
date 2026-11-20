"""Tests for backtest.py (stdlib only)."""
import contextlib
import io
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
        # Settled cohorts need a cutoff 12 months on: only 2025-05 at 2026-05..09.
        self.assertEqual(sorted({(c.cut, c.cohort) for c in all_cases})[:2],
                         [("2026-05", "2025-05"), ("2026-06", "2025-05")])
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
                                    "own rate, 3 mo", "random order"})
        for value in got.values():
            self.assertTrue(0 <= value <= 1)
        pos = b.position(case.seen, "2025-05", "2026-07", 0.5)
        self.assertEqual(got["sweep, 50% left"],
                         b.sweep_chance(3, case.background, pos,
                                        bt.recent_pace(case, case.background)))
        own = b.own_cohort(case.seen, "2025-05").waiting
        rate = b.window_rate(own, "2026-07", 3)
        self.assertAlmostEqual(got["own rate, 3 mo"], 1 - (1 - rate) ** 3)
        # No people ahead: random order says you're done.
        empty = case._replace(seen={**case.seen, "2025-05": [("2026-07", 0.0)]})
        self.assertEqual(bt.predictions(empty)["random order"], 1.0)

    def test_recent_pace_nets_out_the_background(self):
        case = bt.Case("2026-10", 1, "2025-07", 0.0, FIFO_COHORTS, 0.0)
        self.assertEqual(bt.recent_pace(case, 0.0), 300)
        self.assertEqual(bt.recent_pace(case, 0.01), 300 - 93)
        self.assertEqual(bt.recent_pace(case, 1.0), 0)

    def test_score_and_report(self):
        cases = [bt.Case("2026-07", 1, "x", 0.5, {}, 0.0),
                 bt.Case("2026-08", 2, "x", 0.1, {}, 0.0)]
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
        self.assertIn("cases: cutoffs 2026-05 to 2026-09", out.getvalue())
        self.assertIn("No cutoff has a settled cohort", out.getvalue())


if __name__ == "__main__":
    unittest.main()
