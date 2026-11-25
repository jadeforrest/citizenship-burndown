#!/usr/bin/env python3
"""
Tests for flpt_watch.py's history recording.

Stdlib-only (unittest), like test_burndown.py. Nothing here touches the
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

import burndown
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
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(w.feed_period("September 3, 2026", NOW), "2026-09")
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(w.feed_period("Sept. 3, 2026", NOW), "2026-10")
            self.assertEqual(w.feed_period(None, NOW), "2026-10")
        self.assertIn("can't read flpt_lastupdated 'Sept. 3, 2026'", err.getvalue())
        self.assertIn("recording it under 2026-10", err.getvalue())

    def test_feed_text(self):
        self.assertEqual(w.feed_text(None), "")
        self.assertEqual(w.feed_text(9700), "9700")
        self.assertEqual(w.feed_text("About 1"), "About 1")

    def test_section_ignores_non_objects(self):
        self.assertEqual(w.section({"a": None, "b": [1], "c": {"x": 1}}, "a"), {})
        self.assertEqual(w.section({"b": [1]}, "b"), {})
        self.assertEqual(w.section({"c": {"x": 1}}, "c"), {"x": 1})
        self.assertEqual(w.section({}, "missing"), {})


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
        self.assertEqual(row["people_ahead_text"], "About 9,700 people ahead of you")
        self.assertEqual(row["wait_time_text"], "2 months left")
        self.assertEqual(set(row), set(w.HISTORY_FIELDS))

    def test_cohort_with_only_a_wait_time(self):
        rows = w.snapshot_rows(
            feed(ahead={}, waits={"citizen-proofs-2026/09": "33 months left"}), NOW)
        self.assertEqual(rows[0]["people_ahead"], None)
        self.assertEqual(rows[0]["people_ahead_text"], "")

    def test_cohort_with_only_a_count(self):
        rows = w.snapshot_rows(
            feed(waits={}, ahead={"citizen-proofs-2026/09": "About 10 people"}), NOW)
        self.assertEqual(rows[0]["wait_time_text"], "")
        self.assertIsNone(rows[0]["wait_months"])


    def test_null_and_non_string_values(self):
        data = feed(ahead={"citizen-proofs-2025/07": None,
                           "citizen-proofs-2025/08": 11900},
                    waits={"citizen-proofs-2025/07": None})
        rows = {r["cohort"]: r for r in w.snapshot_rows(data, NOW)}
        self.assertEqual(rows["2025-07"]["people_ahead_text"], "")
        self.assertIsNone(rows["2025-07"]["people_ahead"])
        self.assertEqual(rows["2025-07"]["wait_time_text"], "")
        self.assertEqual(rows["2025-08"]["people_ahead"], 11900)

    def test_malformed_sections(self):
        for data in ({"default-update": None, "people-ahead": None},
                     {"default-update": {"flpt_lastupdated": None},
                      "people-ahead": ["citizen-proofs-2025/07"]},
                     {}):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(w.snapshot_rows(data, NOW), [], data)


class Seed(unittest.TestCase):
    def test_live_period_replaces_the_seeded_one(self):
        rows = w.seed_rows(w.snapshot_rows(feed(), NOW))
        periods = [r["period"] for r in rows]
        self.assertNotIn("2026-09", periods)
        self.assertEqual(periods, sorted(periods))
        self.assertEqual(rows[-1]["people_ahead"], 10100)
        for row in rows:
            self.assertEqual([row[k] for k in ("wait_months", "people_ahead_text",
                                               "wait_time_text", "flpt_lastupdated")],
                             ["", "", "", ""])

    def test_skips_only_the_live_period(self):
        # A live period in the middle of the seeded run, not just the newest.
        live = w.snapshot_rows(feed("May 4, 2026"), NOW)
        periods = [r["period"] for r in w.seed_rows(live)]
        self.assertNotIn("2026-05", periods)
        self.assertIn("2026-09", periods)
        self.assertEqual(len(periods), len(w.SEED_HISTORY["2025-07"]) - 1)

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

    def test_null_values_dont_append_every_run(self):
        data = feed(ahead={"citizen-proofs-2025/07": None},
                    waits={"citizen-proofs-2025/07": None})
        w.record_history(data, NOW, self.path)
        self.assertEqual(w.record_history(data, NOW, self.path), [])

    def test_empty_file_is_treated_as_new(self):
        self.path.write_text("")
        w.record_history(feed(), NOW, self.path)
        self.assertEqual(self.path.read_text().splitlines()[0],
                         ",".join(w.HISTORY_FIELDS))

    def test_burndown_reads_the_seeded_series(self):
        w.record_history(feed(), NOW, self.path)
        series = burndown.load_cohorts(self.path)["2025-07"]
        self.assertEqual([c for _, c in series],
                         [9700, 10100, 10200, 10500, 11300, 11700, 13000, 14500, 15800])
        months = [burndown.month_index(p) for p, _ in series]
        self.assertEqual(months, list(range(months[0], months[0] - len(months), -1)))


def setUpModule():
    # check_once writes the widget; keep it off the real iCloud folder.
    patcher = unittest.mock.patch.object(
        burndown, "WIDGET_PATH", Path(tempfile.gettempdir()) / "no-such-dir-flpt" / "w.json")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class CheckOnce(unittest.TestCase):
    def _run(self, state: dict, data: dict | None = None,
             history_error: Exception | None = None):
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(w, "HISTORY_PATH", Path(d) / "h.csv"), \
                unittest.mock.patch.object(w, "STATE_PATH", Path(d) / "s.json"), \
                unittest.mock.patch.object(w, "fetch", return_value=feed()
                                           if data is None else data), \
                unittest.mock.patch.object(w, "beep") as beep, \
                unittest.mock.patch.object(w, "notify") as notify, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            # The state has to be on disk by the time anyone is told about
            # the update, or a crash after notifying would repeat it hourly.
            notify.side_effect = lambda *a: self.assertTrue(
                (Path(d) / "s.json").exists())
            if history_error:
                with unittest.mock.patch.object(w, "record_history",
                                                side_effect=history_error):
                    out = w.check_once(state)
            else:
                out = w.check_once(state)
                self.assertTrue((Path(d) / "h.csv").exists())
            self.assertEqual(w.load_state(), out)
        self.err = err.getvalue()
        return out, beep, notify

    def test_widget_failure_still_saves_state_and_notifies_once(self):
        with unittest.mock.patch.object(w, "write_widget",
                                        side_effect=OSError("full")):
            state, _, notify = self._run({"flpt_lastupdated": "August 1, 2026"})
        self.assertEqual(state["flpt_lastupdated"], "September 3, 2026")
        notify.assert_called_once()
        self.assertIn("widget: not written: OSError('full')", self.err)

    def test_widget_is_written_from_the_history(self):
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(burndown, "write_widget",
                                           return_value=True) as ww, \
                unittest.mock.patch.object(w, "HISTORY_PATH", Path(d) / "h.csv"):
            self.assertTrue(w.write_widget())
        ww.assert_called_once_with(Path(d) / "h.csv")

    def test_history_failure_still_saves_state_and_notifies_once(self):
        state, beep, notify = self._run({"flpt_lastupdated": "August 1, 2026"},
                                        history_error=TypeError("boom"))
        self.assertEqual(state["flpt_lastupdated"], "September 3, 2026")
        notify.assert_called_once()
        self.assertIn("history: not recorded: TypeError('boom')", self.err)

    def test_malformed_feed_does_not_raise(self):
        data = {"default-update": None,
                "people-ahead": {"citizen-proofs-2025/07": 9700},
                "wait-times": {"citizen-proofs-2025/07": None}}
        state, _, notify = self._run({"flpt_lastupdated": "August 1, 2026"}, data)
        self.assertIsNone(state["flpt_lastupdated"])
        notify.assert_called_once()

    def test_feed_update_beeps_and_notifies(self):
        state, beep, notify = self._run({"flpt_lastupdated": "August 1, 2026"})
        beep.assert_called_once()
        notify.assert_called_once_with(
            "IRCC processing times updated",
            "Last updated September 3, 2026 · people ahead: About 9,700 people "
            "ahead of you · wait: 2 months left")
        self.assertEqual(state["flpt_lastupdated"], "September 3, 2026")

    def test_state_records_the_watched_cohort(self):
        state, _, _ = self._run({})
        self.assertEqual(state["people_ahead"], "About 9,700 people ahead of you")
        self.assertEqual(state["wait_time"], "2 months left")
        self.assertEqual(datetime.fromisoformat(state["last_checked"]).tzinfo,
                         timezone.utc)

    def test_first_run_and_unchanged_stay_quiet(self):
        for state in ({}, {"flpt_lastupdated": "September 3, 2026"}):
            _, beep, notify = self._run(dict(state))
            beep.assert_not_called()
            notify.assert_not_called()


class Notifications(unittest.TestCase):
    def test_notify_passes_text_as_argv(self):
        # Pinned exactly: nothing in the suite runs osascript for real.
        with unittest.mock.patch.object(w.subprocess, "run") as run:
            w.notify('Title "q"', "msg \\ end")
        run.assert_called_once_with(
            ["osascript",
             "-e", "on run argv",
             "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
             "-e", "end run",
             'Title "q"', "msg \\ end"],
            check=False, timeout=10)

    def test_beep_plays_the_glass_sound(self):
        with unittest.mock.patch.object(w.subprocess, "run") as run:
            w.beep()
        run.assert_called_once_with(
            ["afplay", "/System/Library/Sounds/Glass.aiff"], check=False, timeout=10)

    def test_beep_falls_back_to_the_terminal_bell(self):
        with unittest.mock.patch.object(w.subprocess, "run",
                                        side_effect=FileNotFoundError), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            w.beep()
        self.assertEqual(out.getvalue(), "\a")

    def test_timeouts_and_missing_tools_are_swallowed(self):
        for error in (FileNotFoundError(),
                      w.subprocess.TimeoutExpired("x", 10)):
            with unittest.mock.patch.object(w.subprocess, "run",
                                            side_effect=error), \
                    contextlib.redirect_stdout(io.StringIO()):
                w.notify("t", "m")
                w.beep()


class Main(unittest.TestCase):
    """--once is what launchd runs: its exit status is the only failure signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.failures = Path(self.tmp.name) / "flpt_failures"

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, check_once) -> tuple[int | None, str]:
        """Run `flpt_watch.py --once`; return its exit code (None if it returned) and stderr."""
        code = None
        with unittest.mock.patch.object(w.sys, "argv", ["flpt_watch.py", "--once"]), \
                unittest.mock.patch.object(w, "load_state", return_value={"k": 1}), \
                unittest.mock.patch.object(w, "check_once", side_effect=check_once) as co, \
                unittest.mock.patch.object(w, "report_failure") as self.reported, \
                unittest.mock.patch.object(w, "FAILURES_PATH", self.failures), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                w.main()
            except SystemExit as e:
                code = e.code
        co.assert_called_once_with({"k": 1})
        return code, err.getvalue()

    def test_once_succeeds_quietly(self):
        self.assertEqual(self._main(lambda state: state), (None, ""))
        self.reported.assert_not_called()

    def test_once_failure_exits_1_and_logs(self):
        self.assertEqual(self._main(RuntimeError("curl failed")),
                         (1, "Error checking feed: curl failed\n"))
        self.reported.assert_called_once_with("check failed: RuntimeError: curl failed", notify=False)

    def test_notifies_only_from_the_second_consecutive_failure(self):
        self._main(RuntimeError("a"))
        self._main(RuntimeError("b"))
        self.assertEqual(self.reported.call_args.kwargs, {"notify": True})
        self.assertEqual(self.failures.read_text().strip(), "2")
        self._main(lambda state: state)  # success resets the streak
        self.assertFalse(self.failures.exists())
        self._main(RuntimeError("c"))
        self.assertEqual(self.reported.call_args.kwargs, {"notify": False})

    def test_corrupt_failure_count_restarts_at_one(self):
        self.failures.write_text("junk")
        self._main(RuntimeError("a"))
        self.assertEqual(self.failures.read_text().strip(), "1")

    def test_report_failure_calls_error_reporting(self):
        with unittest.mock.patch.object(w.subprocess, "run") as run:
            w.report_failure("boom")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], [w.REPORT_ERROR, "report", "--job=flpt-watch",
                                    "--severity=warning", "--reason=boom"])
        self.assertTrue(any(a.startswith("--log=") and a.endswith("flpt_watch.launchd.log") for a in argv))
        self.assertNotIn("--no-notify", argv)
        with unittest.mock.patch.object(w.subprocess, "run") as run:
            w.report_failure("boom", notify=False)
        self.assertEqual(run.call_args.args[0][-1], "--no-notify")

    def test_report_failure_never_raises(self):
        for error in (FileNotFoundError(), w.subprocess.TimeoutExpired("x", 30)):
            with unittest.mock.patch.object(w.subprocess, "run", side_effect=error):
                w.report_failure("boom")


class FuzzSnapshots(unittest.TestCase):
    """
    Random feed sequences recorded one after another.

    Invariants: re-recording a snapshot is a no-op; the last row per cohort
    always matches the latest feed; and burndown reads back exactly one count
    per (cohort, period), equal to the last one the feed published for it.
    """

    CASES = int(os.environ.get("FLPT_FUZZ_CASES", "40"))

    # Mostly well-formed, but the feed is someone else's JSON: nulls, bare
    # numbers and nested values are all things it could start sending.
    @staticmethod
    def _value(rng: random.Random, good: list[str]):
        return rng.choice(good * 4 + [None, rng.randint(0, 50000), [1], {"a": 1}])

    def _snapshot(self, rng: random.Random, month: int) -> dict:
        cohorts = rng.sample([f"202{y}/{m:02d}" for y in range(6) for m in range(1, 13)],
                             rng.randint(0, 6))
        ahead = {f"citizen-proofs-{c}": self._value(rng, [
            f"About {rng.randint(0, 200000):,} people ahead of you",
            "Less than 100 people ahead of you", "No data available"])
            for c in cohorts}
        waits = {f"citizen-proofs-{c}": self._value(rng, [
            "2 months left", "About 1 month left", "No data available"])
            for c in cohorts if rng.random() < 0.8}
        # Runs of up to ~30 months, so sequences cross into later years.
        date = datetime(2026 + month // 12, 1 + month % 12, rng.randint(1, 28))
        data = feed(f"{date:%B} {date.day}, {date.year}", ahead, waits)
        roll = rng.random()
        if roll < 0.04:
            data["default-update"] = None
        elif roll < 0.08:
            data["default-update"] = {"flpt_lastupdated": rng.choice(
                [None, 20260903, f"{date:%b}. {date.day}, {date.year}"])}
        elif roll < 0.1:
            data["people-ahead"] = None
        return data

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
                for _ in range(rng.randint(1, 16)):
                    month += rng.choice([0, 1, 1, 2, 3])
                    data = self._snapshot(rng, month)
                    with contextlib.redirect_stderr(io.StringIO()):
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
                loaded = burndown.load_cohorts(path)
                flat = {(c, p): n for c, s in loaded.items() for p, n in s}
                self.assertEqual(flat, expected, case)
                for series in loaded.values():
                    periods = [p for p, _ in series]
                    self.assertEqual(periods, sorted(set(periods), reverse=True))

    def test_check_once_survives_any_snapshot(self):
        rng = random.Random(4321)
        for case in range(self.CASES):
            data = self._snapshot(rng, rng.randint(0, 40))
            prev = {"flpt_lastupdated": rng.choice([None, "August 1, 2026"])}
            with tempfile.TemporaryDirectory() as d, \
                    unittest.mock.patch.object(w, "HISTORY_PATH", Path(d) / "h.csv"), \
                    unittest.mock.patch.object(w, "STATE_PATH", Path(d) / "s.json"), \
                    unittest.mock.patch.object(w, "fetch", return_value=data), \
                    unittest.mock.patch.object(w, "beep"), \
                    unittest.mock.patch.object(w, "notify"), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                state = w.check_once(dict(prev))
                self.assertEqual(w.load_state()["flpt_lastupdated"],
                                 state["flpt_lastupdated"], case)
                # Whatever the feed sent, history recording shouldn't fail.
                self.assertNotIn("not recorded", err.getvalue(), case)


if __name__ == "__main__":
    unittest.main()
