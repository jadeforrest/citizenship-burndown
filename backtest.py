"""
Backtest the --fifo model against months it didn't see.

For each cutoff period, the history is cut off there, each settled cohort's
share of its own applicants cleared over the next 1..MAX_HORIZON months is
predicted, and the prediction is compared with what flpt_history.csv went on
to show. Models: the sweep at each leftover share (STRAGGLERS candidates), the
cohort's own recent clearance rate carried forward, and random order (the
people ahead clearing at their pace, with you among them at random).

    python3 backtest.py                 # every usable cutoff
    python3 backtest.py --from 2026-07  # only cutoffs from 2026-07 on

Stdlib only, like burndown.py. Own counts are rounded to the nearest 100, so
differences of a point or two between models are noise.
"""
import argparse
import statistics
import sys
from collections import defaultdict
from typing import NamedTuple

import burndown as b

SHARES = (0.2, 0.3, 0.4, 0.5, 0.6)
MAX_HORIZON = 6


class Case(NamedTuple):
    cut: str          # the last period the models may see
    horizon: int      # months after it
    cohort: str
    actual: float     # share of the cohort's own waiting at `cut` cleared by then
    seen: dict        # the history up to `cut`
    background: float
    pace: float       # the shared sweep pace at `cut`, never below 0


def truncate(cohorts: dict[str, list[tuple[str, float]]], cut: str) -> dict:
    """Every cohort's series up to and including `cut`; cohorts left empty go."""
    kept = {c: [(p, n) for p, n in s if p <= cut] for c, s in cohorts.items()}
    return {c: s for c, s in kept.items() if s}


def cases(cohorts: dict[str, list[tuple[str, float]]], start: str) -> list[Case]:
    """A Case for each cutoff from `start`, horizon and settled cohort with figures."""
    last = b.latest_period(cohorts)
    periods = sorted({p for s in cohorts.values() for p, _ in s
                      if max(start, b.SIZE_FROM) <= p < last})
    out = []
    for cut in periods:
        seen = truncate(cohorts, cut)
        names = b.settled_cohorts(seen)
        if not names:
            continue
        _, background = b.fifo_background(seen, names, cut)
        pace = max(b.sweep_pace(seen, names, cut, background) or 0.0, 0.0)
        for horizon in range(1, MAX_HORIZON + 1):
            end = b.month_label(b.month_index(cut) + horizon)
            for name in names:
                now = b.own_cohort(seen, name).waiting.get(cut)
                later = b.own_cohort(cohorts, name).waiting.get(end)
                if now and later is not None:
                    out.append(Case(cut, horizon, name, 1 - later / now, seen,
                                    background, pace))
    return out


def ahead_pace(case: Case) -> float:
    """The recent drop in the cohort's own people ahead, a month; 0 if none."""
    processed = b.processed_per_month([n for _, n in case.seen[case.cohort]])
    return max(b.recent_pace(processed)[0], 0.0) if processed else 0.0


def predictions(case: Case) -> dict[str, float]:
    """Each model's predicted share cleared for `case`."""
    out = {}
    for share in SHARES:
        pos = b.position(case.seen, case.cohort, case.cut, share)
        out[f"sweep, {share:.0%} left"] = b.sweep_chance(
            case.horizon, case.background, pos, case.pace)
    own = b.own_cohort(case.seen, case.cohort).waiting
    rate = b.window_rate(own, case.cut, b.RECENT_MONTHS)
    out[f"own rate, {b.RECENT_MONTHS} mo"] = (
        1 - (1 - max(rate or 0.0, 0.0)) ** case.horizon)
    ahead = case.seen[case.cohort][0][1]
    raw = ahead_pace(case)
    out["random order"] = min(raw * case.horizon / ahead, 1.0) if ahead > 0 else 1.0
    return out


class Score(NamedTuple):
    error: float                 # mean absolute error, all horizons
    bias: float                  # mean (predicted - actual)
    by_horizon: dict[int, float]


def score(all_cases: list[Case]) -> dict[str, Score]:
    """Each model's errors over `all_cases`."""
    errors: dict[str, list[float]] = defaultdict(list)
    by_h: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for case in all_cases:
        for model, predicted in predictions(case).items():
            errors[model].append(predicted - case.actual)
            by_h[model][case.horizon].append(abs(predicted - case.actual))
    return {m: Score(statistics.mean(map(abs, e)), statistics.mean(e),
                     {h: statistics.mean(v) for h, v in sorted(by_h[m].items())})
            for m, e in errors.items()}


def report(scores: dict[str, Score], n_cases: int, cutoffs: list[str]) -> str:
    """The scores as a table, best first."""
    horizons = sorted({h for s in scores.values() for h in s.by_horizon})
    lines = [f"{n_cases} cases: cutoffs {cutoffs[0]} to {cutoffs[-1]}, "
             f"horizons {horizons[0]}-{horizons[-1]} months.",
             "Error is the predicted share of a cohort cleared minus the actual "
             "share; bias above 0 means too optimistic.", "",
             f"  {'model':<18}{'error':>7}{'bias':>8}"
             + "".join(f"{f'{h} mo':>7}" for h in horizons)]
    for model, s in sorted(scores.items(), key=lambda kv: kv[1].error):
        lines.append(f"  {model:<18}{s.error:7.1%}{s.bias:+8.1%}"
                     + "".join(f"{s.by_horizon[h]:7.1%}" if h in s.by_horizon
                               else f"{'-':>7}" for h in horizons))
    return "\n".join(lines)


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", default=b.SIZE_FROM,
                        type=b.cohort_arg, metavar="PERIOD",
                        help="earliest cutoff (default %(default)s)")
    args = parser.parse_args(list(argv))
    all_cases = cases(b.load_cohorts(b.HISTORY_PATH), args.start)
    if not all_cases:
        print("No cutoff has a settled cohort with later figures to check.")
        return 1
    print(report(score(all_cases), len(all_cases),
                 sorted({c.cut for c in all_cases})))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
