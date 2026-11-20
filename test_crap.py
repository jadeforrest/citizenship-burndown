"""Tests for crap.py. Needs lizard, so ./check.sh runs it under .venv."""
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import crap

SOURCE = '''def simple():
    return 1


def branchy(x):
    if x:
        return 1
    return 2
'''


class Crap(unittest.TestCase):
    def test_score_formula(self):
        self.assertEqual(crap.Score("f", 4, 1.0, True).crap(), 4)
        self.assertEqual(crap.Score("f", 4, 0.0, True).crap(), 20)
        self.assertAlmostEqual(crap.Score("f", 3, 0.5, True).crap(), 3 + 9 / 8)

    def test_changed_lines(self):
        diff = ("@@ -1,2 +1,3 @@\n+a\n@@ -10 +12 @@\n-b\n+c\n"
                "@@ -20,4 +30,0 @@\n-gone\n")
        known = unittest.mock.Mock(returncode=0)
        done = unittest.mock.Mock(stdout=diff)
        with unittest.mock.patch.object(crap.subprocess, "run",
                                        side_effect=[known, done]) as run:
            self.assertEqual(crap.changed_lines("HEAD", "x.py"), {1, 2, 3, 12})
        self.assertEqual(run.call_args.args[0],
                         ["git", "diff", "-U0", "HEAD", "--", "x.py"])

    def test_a_file_new_since_ref_is_all_changed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "new.py"
            path.write_text(SOURCE)
            self.assertEqual(crap.changed_lines("HEAD", str(path)), set(range(1, 9)))

    def test_scores(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "m.py")
            Path(path).write_text(SOURCE)
            report = {"files": {path: {"executed_lines": [1, 2, 5, 6, 7],
                                       "missing_lines": [8]}}}
            got = crap.scores(path, report, {7})
            self.assertEqual(got, [crap.Score("simple", 1, 1.0, False),
                                   crap.Score("branchy", 2, 0.75, True)])
            # A file the report doesn't cover counts as unrun.
            self.assertEqual(crap.scores(path, {"files": {}}, set())[0].coverage, 0)

    def run_main(self, results: dict, argv: list[str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "c.json"
            report.write_text(json.dumps({"files": {}}))
            buf = io.StringIO()
            with unittest.mock.patch.object(crap, "changed_lines", return_value=set()), \
                    unittest.mock.patch.object(crap, "scores",
                                               side_effect=lambda p, *_: results[p]), \
                    unittest.mock.patch("sys.argv", ["crap.py", str(report), *argv]), \
                    contextlib.redirect_stdout(buf):
                return crap.main(), buf.getvalue()

    def test_main_passes_and_fails(self):
        ok = {p: [crap.Score("fine", 2, 1.0, True), crap.Score("old", 9, 0.0, False)]
              for p in crap.SOURCES}
        code, out = self.run_main(ok, [])
        self.assertEqual(code, 0)
        self.assertIn("burndown.py:fine", out)
        self.assertNotIn(":old", out)
        self.assertIn("every touched function is at or under 12", out)
        code, out = self.run_main(ok, ["--all"])
        self.assertRegex(out, r":old .*crap= 90\.0  \(untouched\)  OVER")
        bad = {**ok, "flpt_watch.py": [crap.Score("big", 5, 0.0, True)]}
        code, out = self.run_main(bad, [])
        self.assertEqual(code, 1)
        self.assertIn("CRAP over 12: flpt_watch.py:big", out)


if __name__ == "__main__":
    unittest.main()
