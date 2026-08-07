#!/usr/bin/env python3
"""
Burn-down timeline analyzer.

Enter a comma-separated list of backlog counts (the number of people ahead of
you in line), where the FIRST number is the current month and each number after
it is the previous month. Example:

    10200, 10500, 11300, 11700, 13000, 14500, 15800

The script figures out how many people are processed per month, computes low /
likely / high estimates from the stats, and prints how much time is probably
left before the queue clears.
"""

import statistics
import sys


def parse_numbers(raw: str) -> list[float]:
    """Parse a comma- (or whitespace-) separated line into a list of numbers."""
    # Allow commas, spaces, tabs, and stray pipes as separators.
    for sep in ("|", "\t"):
        raw = raw.replace(sep, ",")
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    numbers = []
    for p in parts:
        if not p:
            continue
        try:
            numbers.append(float(p))
        except ValueError:
            raise ValueError(f"Could not read {p!r} as a number.")
    return numbers


def extract_backlog(numbers: list[float]) -> tuple[list[float], list[float], str]:
    """
    Detect and strip a leading counter/index column.

    Some pastes come as two columns: [marker, value, marker, value, ...]
    e.g.  1  2600  1  2700  1  3100  2  7800
    where the odd-position tokens (1, 1, 1, 2, ...) are page/row markers to
    ignore. This keeps every 2nd token when the input clearly looks like
    counter/value pairs, and otherwise leaves the list untouched.

    Returns (values, dropped, description).
    """
    n = len(numbers)
    if n >= 4 and n % 2 == 0:
        markers = numbers[0::2]   # 1st, 3rd, 5th ...  (candidate counters)
        values = numbers[1::2]    # 2nd, 4th, 6th ...  (candidate backlog)
        # Treat as two-column only if every marker is much smaller than its
        # paired value AND the biggest marker is far below the smallest value.
        # That ratio guard keeps genuine single-column data from being halved.
        looks_paired = (
            all(m < v for m, v in zip(markers, values))
            and max(markers) * 5 <= min(values)
        )
        if looks_paired:
            return values, markers, "two-column (dropped counter, kept every 2nd value)"
    return numbers, [], "single column"


def fmt_months(months: float) -> str:
    """Human-friendly rendering of a month count."""
    if months == float("inf"):
        return "never (rate <= 0)"
    years = months / 12
    if years >= 1:
        return f"{months:>5.1f} mo  (~{years:.1f} yr)"
    return f"{months:>5.1f} mo  (~{years:.2f} yr)"


def analyze(entered: list[float]) -> None:
    if len(entered) < 2:
        print("Need at least two months to compute a rate.")
        return

    current = entered[0]

    # Input is newest -> oldest; flip to chronological oldest -> newest.
    chrono = list(reversed(entered))

    # People processed each month (positive = queue shrinking).
    processed = [chrono[i] - chrono[i + 1] for i in range(len(chrono) - 1)]

    # ---- direction sanity check -------------------------------------------
    net = chrono[-1] - chrono[0]  # newest - oldest
    if net > 0:
        print("\n  ⚠  Heads up: the queue is GROWING over this window")
        print(f"     (net +{net:,.0f} since the oldest month). Estimates below")
        print("     assume the average rate; if it keeps growing it won't clear.\n")

    # ---- stats ------------------------------------------------------------
    lo = min(processed)
    hi = max(processed)
    mean = statistics.mean(processed)
    median = statistics.median(processed)
    stdev = statistics.stdev(processed) if len(processed) >= 2 else 0.0

    recent_n = min(3, len(processed))
    recent = statistics.mean(processed[-recent_n:])

    fast = mean + stdev   # +1 sigma
    slow = mean - stdev   # -1 sigma

    def months_left(rate: float) -> float:
        return current / rate if rate > 0 else float("inf")

    # ---- report -----------------------------------------------------------
    print("\n" + "=" * 60)
    print("BURN-DOWN ANALYSIS")
    print("=" * 60)
    print(f"Current backlog:            {current:>10,.0f}")
    print(f"Months of data:             {len(entered):>10d}")
    print(f"Chronological (old->new):   "
          + " -> ".join(f"{v:,.0f}" for v in chrono))
    print(f"Processed each month:       "
          + ", ".join(f"{v:,.0f}" for v in processed))

    print("\nRate stats (people/month)")
    print("-" * 60)
    print(f"  Slowest month:            {lo:>10,.0f}")
    print(f"  Fastest month:            {hi:>10,.0f}")
    print(f"  Average:                  {mean:>10,.0f}")
    print(f"  Median:                   {median:>10,.0f}")
    print(f"  Std deviation:            {stdev:>10,.0f}")
    print(f"  Recent pace (last {recent_n} mo):  {recent:>10,.0f}")

    # Trend note.
    if len(processed) >= 4:
        first_half = statistics.mean(processed[: len(processed) // 2])
        second_half = statistics.mean(processed[len(processed) // 2 :])
        if second_half > first_half * 1.15:
            trend = "ACCELERATING (speeding up)"
        elif second_half < first_half * 0.85:
            trend = "DECELERATING (slowing down)"
        else:
            trend = "roughly steady"
        print(f"  Trend:                    {trend}")

    print("\nTime remaining scenarios")
    print("-" * 60)
    scenarios = [
        ("Fast  (+1 sigma)", fast),
        ("Likely (average)", mean),
        ("Median          ", median),
        (f"Recent ({recent_n} mo pace)", recent),
        ("Slow  (-1 sigma)", slow),
    ]
    for label, rate in scenarios:
        rate_disp = max(rate, 0)
        print(f"  {label:20s}  {rate_disp:>7,.0f}/mo   {fmt_months(months_left(rate))}")

    print("\nAbsolute bounds (single best / worst month)")
    print("-" * 60)
    print(f"  Best case  ({hi:,.0f}/mo):   {fmt_months(months_left(hi))}")
    print(f"  Likely     ({mean:,.0f}/mo):   {fmt_months(months_left(mean))}")
    print(f"  Worst case ({lo:,.0f}/mo):   {fmt_months(months_left(lo))}")
    print("=" * 60 + "\n")


def main() -> None:
    print("Enter backlog counts, comma-separated.")
    print("First number = current month, each next = previous month.")
    print("Example: 10200, 10500, 11300, 11700, 13000, 14500, 15800\n")

    try:
        raw = input("> ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    try:
        tokens = parse_numbers(raw)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    numbers, dropped, mode = extract_backlog(tokens)

    if dropped:
        print(f"\nInput mode: {mode}")
        print("  Using:   " + ", ".join(f"{v:,.0f}" for v in numbers))
        print("  Ignored: " + ", ".join(f"{v:,.0f}" for v in dropped))

    analyze(numbers)


if __name__ == "__main__":
    main()
