#!/usr/bin/env python3
"""
Tests for flpt_watch.py's history recording.

Stdlib-only (unittest), like test_burndown2.py. Nothing here touches the
network, the real flpt_history.csv, or makes a sound:

    python3 -m unittest test_flpt_watch -v

The random-snapshot case runs a small number of feed sequences by default;
raise FLPT_FUZZ_CASES for a longer campaign.
"""

import contextlib
import csv
import io
import os
import random
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import burndown2
import flpt_watch as w

NOW = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)


def feed(last_updated="September 3, 2026", ahead=None, waits=None) -> dict:
    ahead = {"citizen-proofs-2025/07": "About 9,700 people ahead of you",
             "citizen-proofs-2025/08": "About 11,900 people ahead of you",
             "citizen-proofs-2019/12": "Less than 100 people ahead of you",
             "citizen-proofs": "ignored"} if ahead is None else ahead
    waits = {"citizen-proofs-2025/07": "2 months left",
             "citizen-proofs-2025/08": "3 months left"} if waits is None else waits
    return {"default-update": {"flpt_lastupdated": last_updated},
            "people-ahead": ahead, "wait-times": waits}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


class Parsing(unittest.TestCase):
    def test_leading_number(self):
        self.assertEqual(w.leading_number("About 136,000 people ahead"), 136000)
        self.assertEqual(w.leading_number("Less than 100 people"), 100)
        self.assertEqual(w.leading_number("About 1 month left"), 1)
        self.assertIsNone(w.leading_number("We need more time"))
        self.assertIsNone(w.leading_number(None))

    def test_feed_period(self):
        self.assertEqual(w.feed_period("September 3, 2026", NOW), "2026-09")
        self.assertEqual(w.feed_period("garbled", NOW), "2026-10")
        self.assertEqual(w.feed_period(None, NOW), "2026-10")


class Snapshot(unittest.TestCase):
    def test_only_202x_cohorts(self):
        rows = w.snapshot_rows(feed(), NOW)
        self.assertEqual([r["cohort"] for r in rows], ["2025-07", "2025-08"])

    def test_fields(self):
        row = w.snapshot_rows(feed(), NOW)[0]
        self.assertEqual(row["period"], "2026-09")
        self.assertEqual(row["people_ahead"], 9700)
        self.assertEqual(row["wait_months"], 2)
        self.assertEqual(row["flpt_lastupdated"], "September 3, 2026")
        self.assertEqual(set(row), set(w.HISTORY_FIELDS))

    def test_cohort_with_only_a_wait_time(self):
        rows = w.snapshot_rows(
            feed(ahead={}, waits={"citizen-proofs-2026/09": "33 months left"}), NOW)
        self.assertEqual(rows[0]["people_ahead"], None)
        self.assertEqual(rows[0]["people_ahead_text"], "")


class Seed(unittest.TestCase):
    def test_live_period_replaces_the_seeded_one(self):
        rows = w.seed_rows(w.snapshot_rows(feed(), NOW))
        periods = [r["period"] for r in rows]
        self.assertNotIn("2026-09", periods)
        self.assertEqual(periods, sorted(periods))
        self.assertEqual(rows[-1]["people_ahead"], 10100)

    def test_live_period_without_a_count_keeps_the_seed(self):
        live = w.snapshot_rows(feed(ahead={
            "citizen-proofs-2025/07": "No data available"}), NOW)
        periods = [r["period"] for r in w.seed_rows(live)]
        self.assertIn("2026-09", periods)

    def test_seeded_in_full_when_live_is_later(self):
        rows = w.seed_rows(w.snapshot_rows(feed("October 2, 2026"), NOW))
        self.assertEqual(len(rows), len(w.SEED_HISTORY["2025-07"]))


class ChangedRows(unittest.TestCase):
    def setUp(self):
        self.live = w.snapshot_rows(feed(), NOW)

    def test_new_cohort_is_a_change(self):
        self.assertEqual(w.changed_rows([], self.live), self.live)

    def test_identical_is_not(self):
        history = [{k: str(v) for k, v in r.items()} for r in self.live]
        self.assertEqual(w.changed_rows(history, self.live), [])

    def test_compares_against_the_latest_row(self):
        old = dict(self.live[0], wait_time_text="9 months left")
        history = [{k: str(v) for k, v in r.items()} for r in self.live] + [old]
        self.assertEqual(w.changed_rows(history, self.live), [self.live[0]])

    def test_new_feed_date_with_flat_figures_is_a_change(self):
        later = w.snapshot_rows(feed("October 2, 2026"), NOW)
        self.assertEqual(len(w.changed_rows(self.live, later)), len(later))


class RecordHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "h.csv"

    def tearDown(self):
        self.dir.cleanup()

    def test_first_run_seeds_then_records_live(self):
        written = w.record_history(feed(), NOW, self.path)
        rows = read_csv(self.path)
        self.assertEqual(len(rows), len(written))
        self.assertEqual(rows[0]["observed_at"], "seed")
        self.assertEqual(sum(r["observed_at"] == "seed" for r in rows),
                         len(w.SEED_HISTORY["2025-07"]) - 1)

    def test_rerun_appends_nothing(self):
        w.record_history(feed(), NOW, self.path)
        before = self.path.read_text()
        self.assertEqual(w.record_history(feed(), NOW, self.path), [])
        self.assertEqual(self.path.read_text(), before)

    def test_empty_file_is_treated_as_new(self):
        self.path.write_text("")
        w.record_history(feed(), NOW, self.path)
        self.assertEqual(self.path.read_text().splitlines()[0],
                         ",".join(w.HISTORY_FIELDS))

    def test_burndown_reads_the_seeded_series(self):
        w.record_history(feed(), NOW, self.path)
        series = burndown2.load_cohorts(self.path)["2025-07"]
        self.assertEqual([c for _, c in series],
                         [9700, 10100, 10200, 10500, 11300, 11700, 13000, 14500, 15800])
        self.assertEqual(burndown2.missing_months([p for p, _ in series]), [])


class CheckOnce(unittest.TestCase):
    def _run(self, state: dict) -> tuple[dict, unittest.mock.Mock]:
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(w, "HISTORY_PATH", Path(d) / "h.csv"), \
                unittest.mock.patch.object(w, "STATE_PATH", Path(d) / "s.json"), \
                unittest.mock.patch.object(w, "fetch", return_value=feed()), \
                unittest.mock.patch.object(w, "beep") as beep, \
                unittest.mock.patch.object(w, "notify") as notify, \
                contextlib.redirect_stdout(io.StringIO()):
            out = w.check_once(state)
            self.assertTrue((Path(d) / "h.csv").exists())
        return out, beep, notify

    def test_feed_update_beeps_and_notifies(self):
        state, beep, notify = self._run({"flpt_lastupdated": "August 1, 2026"})
        beep.assert_called_once()
        notify.assert_called_once()
        self.assertEqual(state["flpt_lastupdated"], "September 3, 2026")

    def test_first_run_and_unchanged_stay_quiet(self):
        for state in ({}, {"flpt_lastupdated": "September 3, 2026"}):
            _, beep, notify = self._run(dict(state))
            beep.assert_not_called()
            notify.assert_not_called()


class FuzzSnapshots(unittest.TestCase):
    """
    Random feed sequences recorded one after another.

    Invariants: re-recording a snapshot is a no-op; the last row per cohort
    always matches the latest feed; and burndown2 reads back exactly one count
    per (cohort, period), equal to the last one the feed published for it.
    """

    CASES = int(os.environ.get("FLPT_FUZZ_CASES", "40"))

    def _snapshot(self, rng: random.Random, month: int) -> dict:
        cohorts = rng.sample([f"202{y}/{m:02d}" for y in range(6) for m in range(1, 13)],
                             rng.randint(0, 6))
        ahead = {f"citizen-proofs-{c}": rng.choice(
            [f"About {rng.randint(0, 200000):,} people ahead of you",
             "Less than 100 people ahead of you", "No data available"])
            for c in cohorts}
        waits = {f"citizen-proofs-{c}": rng.choice(
            ["2 months left", "About 1 month left", "No data available"])
            for c in cohorts if rng.random() < 0.8}
        date = datetime(2026, 1 + month % 12, rng.randint(1, 28))
        return feed(f"{date:%B} {date.day}, {date.year}", ahead, waits)

    def test_invariants(self):
        rng = random.Random(1234)
        for case in range(self.CASES):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "h.csv"
                expected: dict[tuple[str, str], int] = {}
                for seed_cohort, figures in w.SEED_HISTORY.items():
                    for period, count in figures:
                        expected[(seed_cohort, period)] = count
                month = 0
                for _ in range(rng.randint(1, 8)):
                    month += rng.choice([0, 1, 1, 2])
                    data = self._snapshot(rng, month)
                    w.record_history(data, NOW, path)
                    self.assertEqual(w.record_history(data, NOW, path), [], case)
                    live = w.snapshot_rows(data, NOW)
                    latest = {r["cohort"]: r for r in read_csv(path)}
                    for row in live:
                        got = latest[row["cohort"]]
                        self.assertEqual(got["people_ahead_text"],
                                         row["people_ahead_text"], case)
                        if row["people_ahead"] is not None:
                            expected[(row["cohort"], row["period"])] = row["people_ahead"]
                loaded = burndown2.load_cohorts(path)
                flat = {(c, p): n for c, s in loaded.items() for p, n in s}
                self.assertEqual(flat, expected, case)
                for series in loaded.values():
                    periods = [p for p, _ in series]
                    self.assertEqual(periods, sorted(set(periods), reverse=True))


if __name__ == "__main__":
    unittest.main()
