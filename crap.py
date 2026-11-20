"""
CRAP scores for the functions a change touches, from coverage.py's JSON report
and lizard's complexity. Run by ./check.sh under .venv (it imports lizard).

    CRAP = complexity^2 * (1 - coverage)^3 + complexity

Coverage is the share of a function's measured lines that ran. A function is
touched when `git diff REF` changes any line inside it (REF defaults to HEAD,
so uncommitted work; pass a branch or commit to score a whole change). Exits 1
if any touched function scores over the threshold.
"""
import argparse
import json
import re
import subprocess
import sys
from typing import NamedTuple

import lizard

THRESHOLD = 12.0
SOURCES = ("burndown.py", "flpt_watch.py", "crap.py")


def changed_lines(ref: str, path: str) -> set[int]:
    """
    Line numbers in the working copy of `path` that differ from `ref`: every
    line when `ref` doesn't have the file (new, or not yet committed).
    """
    known = subprocess.run(["git", "cat-file", "-e", f"{ref}:{path}"],
                           capture_output=True)
    if known.returncode:
        with open(path) as f:
            return set(range(1, sum(1 for _ in f) + 1))
    diff = subprocess.run(["git", "diff", "-U0", ref, "--", path],
                          capture_output=True, text=True, check=True).stdout
    lines: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start, count = int(m[1]), int(m[2] or 1)
        lines.update(range(start, start + count))
    return lines


class Score(NamedTuple):
    name: str
    complexity: int
    coverage: float
    touched: bool

    def crap(self) -> float:
        return self.complexity ** 2 * (1 - self.coverage) ** 3 + self.complexity


def scores(path: str, report: dict, changed: set[int]) -> list[Score]:
    """A Score for each function lizard finds in `path`."""
    data = report["files"].get(path, {"executed_lines": [], "missing_lines": []})
    ran, missed = set(data["executed_lines"]), set(data["missing_lines"])
    result = []
    for fn in lizard.analyze_file(path).function_list:
        span = set(range(fn.start_line, fn.end_line + 1))
        measured = span & (ran | missed)
        coverage = len(measured & ran) / len(measured) if measured else 0.0
        result.append(Score(fn.name, fn.cyclomatic_complexity, coverage,
                            bool(span & changed)))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", help="coverage json output")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--all", action="store_true",
                        help="list every function, not just touched ones")
    args = parser.parse_args()
    with open(args.report) as f:
        report = json.load(f)
    over = []
    for path in SOURCES:
        for s in scores(path, report, changed_lines(args.ref, path)):
            if s.touched or args.all:
                note = ("" if s.touched else "  (untouched)") + (
                    "  OVER" if s.crap() > THRESHOLD else "")
                print(f"  {path}:{s.name:30} cc={s.complexity:2}  "
                      f"cov={s.coverage:4.0%}  crap={s.crap():5.1f}{note}")
            if s.touched and s.crap() > THRESHOLD:
                over.append(f"{path}:{s.name}")
    if over:
        print(f"CRAP over {THRESHOLD:g}: {', '.join(over)}")
        return 1
    print(f"CRAP: every touched function is at or under {THRESHOLD:g}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
