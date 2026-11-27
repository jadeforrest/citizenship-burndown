"""
Backtest the sweep model against months it didn't see.

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

Last, the leftover share actually left by each pass the sweep has made
(measured_passes), against what STRAGGLERS assumes.

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
    reached: bool = False   # the sweep arrives within `horizon` at any of SHARES


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
    """
    Whether the sweep is in or reaches `name` within `horizon` at any of
    SHARES, so the cases scored on their own don't depend on STRAGGLERS
    (at the share alone, changing it from 50% to 20% took them from 15 to 11).
    """
    return any((pos := b.position(seen, name, cut, share)) is not None
               and pos.take > 0 and b.arrival_months(rates, pos) < horizon
               for share in SHARES)


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


# The leftover share measured, to check STRAGGLERS against cohorts the sweep
# has been through: a cohort's own count before its pass, and where the count
# levelled off. Each own count is the difference of two figures rounded to
# 100, so it's off by up to 100 (ROUNDING_SD, about 41, as a standard
# deviation); pooling the finished cohorts averages much of that out.
PASS_DROP = 0.25     # a fall of more than this (and 2 * ROUNDING) over
PASS_MONTHS = 3      # PASS_MONTHS starts a pass; a month at a time misses a
                     # slow sweep (2025-01: ~10%/mo), and 15% caught the
                     # trickle and rounding (2025-03: 1,900 -> 1,600)
FLOOR_MONTHS = 3     # it's over after this many months with ...
FLOOR_CALM = 0.05    # ... no fall, monthly or in all, above this share of
ROUNDING = 100       #     `before` or ROUNDING, whichever is more
ROUNDING_SD = ROUNDING / 6 ** 0.5   # two uniform +-50 errors: sqrt(2 * 100^2 / 12)
SETTLE_MONTHS = 9    # a cohort's bulge has settled by this age (3-7 months seen)
MIN_BEFORE = 500     # smaller cohorts are too much rounding to measure


class Pass(NamedTuple):
    cohort: str
    reached: str        # where its count was highest in the months before the fall
    before: float       # the count then
    ended: str | None   # where it levelled off; None while still falling
    left: float         # the count then, or in the latest period while falling
    n: int = 1          # cohorts pooled

    def share(self) -> float:
        return self.left / self.before

    def bounds(self) -> tuple[float, float]:
        """The share with `left` and `before` each two standard deviations off."""
        e = 2 * ROUNDING_SD * self.n ** 0.5
        return max(self.left - e, 0) / (self.before + e), (self.left + e) / (self.before - e)


def spans(periods: list[str], months: int) -> bool:
    """Whether the first and last of `periods` are `months` apart (no gap)."""
    return b.month_index(periods[-1]) - b.month_index(periods[0]) == months


def levelled_off(series: dict[str, float], periods: list[str],
                 before: float) -> str | None:
    """The first of `periods` that starts FLOOR_MONTHS calm months in a row."""
    calm = max(FLOOR_CALM * before, ROUNDING)
    for i in range(len(periods) - FLOOR_MONTHS):
        window = periods[i:i + FLOOR_MONTHS + 1]
        run = [series[p] for p in window]
        if (spans(window, FLOOR_MONTHS) and run[0] - run[-1] <= calm
                and all(x - y <= calm for x, y in zip(run, run[1:]))):
            return periods[i]
    return None


def falls(series: dict[str, float], start: str, end: str) -> bool:
    """Whether the count fell by more than PASS_DROP and 2 * ROUNDING."""
    drop = series[start] - series[end]
    return drop > 2 * ROUNDING and drop > PASS_DROP * series[start]


def sweep_pass(cohort: str, series: dict[str, float]) -> Pass | None:
    """
    The first fall over PASS_MONTHS that's beyond the trickle and rounding,
    from the highest count in the 2 * PASS_MONTHS months up to where it was
    seen (the latest, if tied) so a slow sweep's first months count, to
    where it levelled off from then on. Windows with a missing month are
    skipped. Only the first such fall counts: an early false one (a late
    bulge, the C-3 break) would hide a later real pass.
    """
    periods = sorted(series)
    for i in range(PASS_MONTHS, len(periods)):
        if (spans(periods[i - PASS_MONTHS:i + 1], PASS_MONTHS)
                and falls(series, periods[i - PASS_MONTHS], periods[i])):
            look = periods[max(i - 2 * PASS_MONTHS, 0):i]
            start = max(reversed(look), key=series.__getitem__)
            ended = levelled_off(series, periods[i:], series[start])
            return Pass(cohort, start, series[start], ended,
                        series[ended or periods[-1]])
    return None


def measured_passes(cohorts: dict[str, list[tuple[str, float]]]) -> list[Pass]:
    """A Pass for each cohort of at least MIN_BEFORE the sweep has reached."""
    out = []
    for name in sorted(cohorts):
        settled = b.month_label(b.month_index(name) + SETTLE_MONTHS)
        waiting = b.own_cohort(cohorts, name).waiting
        found = sweep_pass(name, {p: n for p, n in waiting.items() if p >= settled})
        if found and found.before >= MIN_BEFORE:
            out.append(found)
    return out


def pooled(passes: list[Pass]) -> Pass:
    """The finished passes as one: their counts before and after summed."""
    return Pass(f"{len(passes)} pooled", "", sum(p.before for p in passes), "",
                sum(p.left for p in passes), len(passes))


def pass_report(passes: list[Pass], assumed: float) -> str:
    """Each measured pass, then the finished ones pooled."""
    lines = [f"Leftover share, measured (STRAGGLERS assumes {assumed:.0%}):",
             "A cohort's own count before the sweep and where it levelled off. "
             "The range allows",
             "for the rounding only (two standard deviations); '<' while still "
             "falling.",
             "", f"  {'cohort':<11}{'reached':>8}{'before':>8}{'levelled':>10}"
             f"{'left':>7}{'share':>7}{'range':>10}"]
    done = [p for p in passes if p.ended]
    rows = passes + ([pooled(done)] if len(done) > 1 else [])
    for p in rows:
        low, high = p.bounds()
        share = f"{'' if p.ended is not None else '<'}{p.share():.0%}"
        lines.append(f"  {p.cohort:<11}{p.reached:>8}{p.before:>8,.0f}"
                     f"{'falling' if p.ended is None else p.ended:>10}"
                     f"{p.left:>7,.0f}{share:>7}{f'{low:.0%}-{high:.0%}':>10}")
    if not passes:
        lines.append("  none yet.")
    return "\n".join(lines)


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", default=b.SIZE_FROM,
                        type=b.cohort_arg, metavar="PERIOD",
                        help="earliest cutoff (default %(default)s)")
    args = parser.parse_args(list(argv))
    cohorts = b.load_cohorts(b.HISTORY_PATH)
    all_cases = cases(cohorts, args.start)
    if not all_cases:
        print("No cutoff has a settled cohort with later figures to check.")
        return 1
    print(report(score(all_cases), len(all_cases),
                 sorted({c.cut for c in all_cases})))
    reached = [c for c in all_cases if c.reached]
    print("\nWhere the sweep is in or reaches the cohort within the horizon:")
    print(report(score(reached), len(reached), sorted({c.cut for c in reached}))
          if reached else "  none.")
    print("\n" + pass_report(measured_passes(cohorts), b.STRAGGLERS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
