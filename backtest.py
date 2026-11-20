"""
Backtest the --fifo model against months it didn't see.

For each cutoff period, the history is cut off there, each settled cohort's
share of its own applicants cleared over the next 1..MAX_HORIZON months is
predicted, and the prediction is compared with what flpt_history.csv went on
to show. Models: the sweep at each leftover share (STRAGGLERS candidates), the
cohort's own recent clearance rate carried forward, random order (the people
ahead clearing at their pace, with you among them at random), a gradient
(each group's rate a falling function of how many are ahead of it, fitted
on the cutoff's last window and simulated forward), and two baselines: the
trickle alone, and nothing cleared at all.

The sweep only differs from the trickle alone where it reaches the cohort
within the horizon, so the report scores those cases again on their own.

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
    leftover: float = 0.0   # how fast the leftovers clear at `cut`
    reached: bool = False   # the 50% sweep arrives within `horizon`


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
        rates = b.model_rates(seen, names, cut)
        pace = max(rates.pace or 0.0, 0.0)
        for horizon in range(1, MAX_HORIZON + 1):
            end = b.month_label(b.month_index(cut) + horizon)
            for name in names:
                now = b.own_cohort(seen, name).waiting.get(cut)
                later = b.own_cohort(cohorts, name).waiting.get(end)
                if now and later is not None:
                    out.append(Case(cut, horizon, name, 1 - later / now, seen,
                                    rates.background, pace, rates.leftover,
                                    reaches(seen, name, cut, rates, horizon)))
    return out


def reaches(seen: dict, name: str, cut: str, rates: b.Rates, horizon: int) -> bool:
    """Whether the STRAGGLERS sweep is in or reaches `name` within `horizon`."""
    pos = b.position(seen, name, cut)
    return (pos is not None and pos.take > 0
            and b.arrival_months(rates, pos) < horizon)


def isotonic_falling(points: list[tuple[float, float, float]]) -> list[tuple[float, float]]:
    """
    (x, fitted y) for (x, y, weight) points, the fitted y never rising with
    x: weighted least squares by pooling adjacent violators. Points without
    weight are dropped.
    """
    blocks: list[list] = []     # [weighted y sum, weight, xs]
    for x, y, w in sorted(p for p in points if p[2] > 0):
        blocks.append([w * y, w, [x]])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]
                                   < blocks[-1][0] / blocks[-1][1]):
            wy, w2, xs = blocks.pop()
            blocks[-1][0] += wy
            blocks[-1][1] += w2
            blocks[-1][2] += xs
    return [(x, wy / w) for wy, w, xs in blocks for x in xs]


def interpolate(fit: list[tuple[float, float]], x: float) -> float:
    """`fit`'s y at `x`: linear between its points, flat past either end."""
    if x <= fit[0][0]:
        return fit[0][1]
    for (x0, y0), (x1, y1) in zip(fit, fit[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return fit[-1][1]


def groups(seen: dict, names: list[str]) -> list[tuple[str, dict[str, float]]]:
    """The queue in order: the tail before `names[0]`, then each settled cohort."""
    return [("", dict(seen[names[0]]))] + [
        (n, b.settled_waiting(b.own_cohort(seen, n).waiting)) for n in names]


def gradient_fit(seen: dict, names: list[str], cut: str) -> list[tuple[float, float]]:
    """
    Each group's compounded clearance over the RECENT_MONTHS to `cut`
    against the middle of its place in the queue when the window opened
    (people ahead plus half its own), weighted by its size then and made to
    fall with the place (isotonic_falling). [] with nothing to fit.
    """
    first = b.month_label(b.month_index(cut) - b.RECENT_MONTHS)
    points, ahead = [], 0.0
    for _, series in groups(seen, names):
        if first not in series:
            return isotonic_falling(points)
        rate = b.window_rate(series, cut, b.RECENT_MONTHS)
        if rate is not None:
            points.append((ahead + series[first] / 2, rate, series[first]))
        ahead += series[first]
    return isotonic_falling(points)


def gradient_cleared(case: Case) -> float:
    """
    The gradient model's share of `case.cohort` cleared after `case.horizon`
    months: every month each group clears its fitted rate at its current
    place in the queue, so a cohort's rate rises as the queue ahead drains.
    """
    names = b.settled_cohorts(case.seen)
    if not names:
        return 0.0
    fit = gradient_fit(case.seen, names, case.cut)
    queue = [(n, s.get(case.cut, 0.0)) for n, s in groups(case.seen, names)]
    start = dict(queue).get(case.cohort, 0.0)
    if not fit or start <= 0:
        return 0.0
    for _ in range(case.horizon):
        ahead, moved = 0.0, []
        for n, count in queue:
            rate = b.unit(interpolate(fit, ahead + count / 2))
            moved.append((n, count * (1 - rate)))
            ahead += count
        queue = moved
    return 1 - dict(queue)[case.cohort] / start


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
            case.horizon, case.background, pos, case.pace, case.leftover)
    own = b.own_cohort(case.seen, case.cohort).waiting
    rate = b.window_rate(own, case.cut, b.RECENT_MONTHS)
    out[f"own rate, {b.RECENT_MONTHS} mo"] = (
        1 - (1 - max(rate or 0.0, 0.0)) ** case.horizon)
    ahead = case.seen[case.cohort][0][1]
    raw = ahead_pace(case)
    out["random order"] = min(raw * case.horizon / ahead, 1.0) if ahead > 0 else 1.0
    out["gradient"] = gradient_cleared(case)
    out["trickle only"] = 1 - (1 - case.background) ** case.horizon
    out["nothing"] = 0.0
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
    reached = [c for c in all_cases if c.reached]
    print("\nWhere the sweep is in or reaches the cohort within the horizon:")
    print(report(score(reached), len(reached), sorted({c.cut for c in reached}))
          if reached else "  none.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
