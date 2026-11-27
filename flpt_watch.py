#!/usr/bin/env python3
"""
FLPT feed watcher.

Polls IRCC's published processing-times JSON feed, reports the current
"citizen-proofs-2025/07" backlog figure and wait-time estimate, and beeps
and posts a macOS notification whenever flpt_lastupdated changes (i.e.
IRCC has refreshed the feed with new data).

Every citizen-proofs-202x/* cohort's people-ahead count and wait-time
estimate is also appended to flpt_history.csv whenever it changes, so
burndown.py can analyze any cohort's month-by-month backlog. The first run
against an empty history seeds it with SEED_HISTORY before recording the live
snapshot.

Usage:
    python3 flpt_watch.py            # run forever, checking hourly
    python3 flpt_watch.py --once     # check once and exit (cron/launchd)
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.canada.ca/content/dam/ircc/documents/json/flpt-en.json"
FIELD_KEY = "citizen-proofs-2025/07"
STATE_PATH = Path(__file__).with_name("flpt_state.json")
CHECK_INTERVAL_SECONDS = 3600

HISTORY_PATH = Path(__file__).with_name("flpt_history.csv")
HISTORY_FIELDS = (
    "period",            # YYYY-MM of flpt_lastupdated: the feed month a row belongs to
    "cohort",            # YYYY-MM application month, from citizen-proofs-YYYY/MM
    "people_ahead",      # leading number in people_ahead_text ("Less than 100" -> 100)
    "wait_months",       # leading number in wait_time_text, blank if there isn't one
    "people_ahead_text",
    "wait_time_text",
    "flpt_lastupdated",
    "observed_at",       # when this watcher saw it (UTC); "seed" for SEED_HISTORY
                         # rows, "wayback:<timestamp>" for the 2026-11-27 backfill
)
COHORT_KEY = re.compile(r"^citizen-proofs-(202\d)/(\d\d)$")

# Figures recorded by hand before the watcher kept history, newest first, as
# (feed period, people ahead). Written once, into an empty history file.
SEED_HISTORY = {
    "2025-07": [
        ("2026-09", 9700), ("2026-08", 10100), ("2026-07", 10200),
        ("2026-06", 10500), ("2026-05", 11300), ("2026-04", 11700),
        ("2026-03", 13000), ("2026-02", 14500), ("2026-01", 15800),
    ],
}


def fetch() -> dict:
    # Canada.ca's CDN appears to block Python's urllib TLS fingerprint but
    # allows curl, so shell out rather than use urllib.request directly.
    result = subprocess.run(
        ["curl", "-fsSL", "--max-time", "30", URL],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


# Installed by error-reporting/install.sh.
REPORT_ERROR = os.path.expanduser("~/.local/bin/report-error")
FAILURES_PATH = Path(__file__).with_name("flpt_failures")
# Hourly, so one failure is usually a wake-from-sleep or network blip: record
# every failure, but only notify once this many runs in a row have failed.
NOTIFY_AFTER_FAILURES = 2


def count_failure() -> int:
    """Increment and return the consecutive --once failure count."""
    try:
        n = int(FAILURES_PATH.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    try:
        FAILURES_PATH.write_text(f"{n}\n")
    except OSError:
        pass
    return n


def clear_failures() -> None:
    try:
        FAILURES_PATH.unlink()
    except OSError:
        pass


def report_failure(reason: str, notify: bool = True) -> None:
    """Record a failed --once run in error-reporting as a warning
    (notified, throttled, only when `notify`). Silently skipped where that CLI
    isn't installed."""
    here = Path(__file__).resolve()
    argv = [REPORT_ERROR, "report", "--job=flpt-watch", "--severity=warning",
            f"--reason={reason}", f"--script={here}",
            f"--log={here.parent / 'flpt_watch.launchd.log'}"]
    if not notify:
        argv.append("--no-notify")
    try:
        subprocess.run(
            argv,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def beep() -> None:
    """Play a single audible beep (macOS); fall back to the terminal bell."""
    try:
        subprocess.run(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        print("\a", end="", flush=True)
    except subprocess.TimeoutExpired:
        pass


def notify(title: str, message: str) -> None:
    """Post a macOS notification; silently skip if osascript isn't available."""
    # Pass the text as argv rather than splicing it into the script so quotes
    # or backslashes in the feed data can't break (or inject into) AppleScript.
    try:
        subprocess.run(
            ["osascript",
             "-e", "on run argv",
             "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
             "-e", "end run",
             title, message],
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def section(data: dict, key: str) -> dict:
    """A top-level object from the feed, or {} if it's missing or not an object."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def feed_text(value) -> str:
    """A feed value as text: null becomes "", anything else non-string is str()'d."""
    return "" if value is None else str(value)


def leading_number(text: str | None) -> int | None:
    """First number in a feed string: "About 9,700 people ahead" -> 9700."""
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group().replace(",", "")) if match else None


def feed_period(last_updated: str | None, fallback: datetime) -> str:
    """
    YYYY-MM for a flpt_lastupdated like "September 3, 2026".

    An unreadable date falls back to the month the watcher ran in, which can
    file a late-month update under the following month — so say so loudly
    (stderr ends up in the launchd log).
    """
    try:
        return datetime.strptime(last_updated or "", "%B %d, %Y").strftime("%Y-%m")
    except ValueError:
        period = fallback.strftime("%Y-%m")
        print(f"warning: can't read flpt_lastupdated {last_updated!r}; "
              f"recording it under {period}", file=sys.stderr)
        return period


def snapshot_rows(data: dict, observed_at: datetime) -> list[dict]:
    """One history row per citizen-proofs-202x/* cohort in the feed."""
    last_updated = feed_text(section(data, "default-update").get("flpt_lastupdated"))
    period = feed_period(last_updated, observed_at)
    ahead = {k: feed_text(v) for k, v in section(data, "people-ahead").items()}
    waits = {k: feed_text(v) for k, v in section(data, "wait-times").items()}
    rows = []
    for key in sorted(set(ahead) | set(waits)):
        match = COHORT_KEY.match(key)
        if not match:
            continue
        rows.append({
            "period": period,
            "cohort": f"{match.group(1)}-{match.group(2)}",
            "people_ahead": leading_number(ahead.get(key)),
            "wait_months": leading_number(waits.get(key)),
            "people_ahead_text": ahead.get(key, ""),
            "wait_time_text": waits.get(key, ""),
            "flpt_lastupdated": last_updated,
            "observed_at": observed_at.isoformat(timespec="seconds"),
        })
    return rows


def seed_rows(live: list[dict]) -> list[dict]:
    """
    SEED_HISTORY as history rows, oldest first.

    A seeded period the live snapshot already has a count for is skipped, so
    the live row (which also carries the wait time) is the one that's kept.
    """
    live_keys = {(r["period"], r["cohort"]) for r in live
                 if r["people_ahead"] is not None}
    rows = []
    for cohort, figures in SEED_HISTORY.items():
        for period, count in reversed(figures):
            if (period, cohort) in live_keys:
                continue
            rows.append({
                "period": period, "cohort": cohort, "people_ahead": count,
                "wait_months": "", "people_ahead_text": "", "wait_time_text": "",
                "flpt_lastupdated": "", "observed_at": "seed",
            })
    return rows


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def changed_rows(history: list[dict], live: list[dict]) -> list[dict]:
    """
    Live rows that differ from the latest recorded row for their cohort.

    A new feed date counts as a change even when the figures didn't move, so
    each cohort gets a row for every month IRCC publishes — a flat month is
    still a data point for the burn-down.
    """
    compare = ("period", "people_ahead_text", "wait_time_text", "flpt_lastupdated")
    latest = {row["cohort"]: row for row in history}
    out = []
    for row in live:
        prev = latest.get(row["cohort"])
        if prev is None or any(str(prev[k]) != str(row[k]) for k in compare):
            out.append(row)
    return out


def append_history(rows: list[dict], path: Path) -> None:
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def record_history(data: dict, observed_at: datetime,
                   path: Path) -> list[dict]:
    """Append whatever changed (seeding an empty file first); return what was written."""
    live = snapshot_rows(data, observed_at)
    history = load_history(path)
    pending = [] if history else seed_rows(live)
    pending += changed_rows(history, live)
    if pending:
        append_history(pending, path)
    return pending


def write_widget() -> bool:
    """burndown.write_widget() on the history; imported here so a broken
    burndown can't stop the watcher loading."""
    import burndown
    return burndown.write_widget(HISTORY_PATH)


def check_once(state: dict) -> dict:
    data = fetch()
    last_updated = section(data, "default-update").get("flpt_lastupdated")
    people_ahead = section(data, "people-ahead").get(FIELD_KEY)
    wait_time = section(data, "wait-times").get(FIELD_KEY)

    observed = datetime.now(timezone.utc)
    now = observed.isoformat(timespec="seconds")
    prev_last_updated = state.get("flpt_lastupdated")

    changed = prev_last_updated is not None and last_updated != prev_last_updated
    if changed:
        print(f"[{now}] flpt_lastupdated changed: {prev_last_updated!r} -> {last_updated!r}")
    else:
        print(f"[{now}] flpt_lastupdated unchanged: {last_updated!r}")

    print(f"[{now}] {FIELD_KEY} people ahead: {people_ahead!r}")
    print(f"[{now}] {FIELD_KEY} wait time:    {wait_time!r}")

    # History is compared against the file, not the state, so a failure here
    # is retried next run on its own. It mustn't stop the state being saved,
    # or the same update would beep and notify again every hour.
    try:
        written = record_history(data, observed, HISTORY_PATH)
        print(f"[{now}] history: {len(written)} row(s) appended to {HISTORY_PATH.name}")
    except Exception as e:
        print(f"[{now}] history: not recorded: {e!r}", file=sys.stderr)

    # Like history, the widget mustn't stop the state being saved.
    try:
        print(f"[{now}] widget: {'written' if write_widget() else 'skipped'}")
    except Exception as e:
        print(f"[{now}] widget: not written: {e!r}", file=sys.stderr)

    state["flpt_lastupdated"] = last_updated
    state["people_ahead"] = people_ahead
    state["wait_time"] = wait_time
    state["last_checked"] = now
    save_state(state)

    # Announce only once the state is saved, for the same reason.
    if changed:
        beep()
        notify(
            "IRCC processing times updated",
            f"Last updated {last_updated} · people ahead: {people_ahead} · wait: {wait_time}",
        )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch the IRCC FLPT feed for updates.")
    parser.add_argument(
        "--once", action="store_true",
        help="Check once and exit instead of looping hourly.",
    )
    args = parser.parse_args()

    state = load_state()

    if args.once:
        try:
            check_once(state)
        except Exception as e:
            print(f"Error checking feed: {e}", file=sys.stderr)
            failures = count_failure()
            report_failure(f"check failed: {type(e).__name__}: {e}",
                           notify=failures >= NOTIFY_AFTER_FAILURES)
            sys.exit(1)
        clear_failures()
        return

    print(f"Watching {URL}")
    print(f"Checking every {CHECK_INTERVAL_SECONDS // 60} minutes. Ctrl+C to stop.\n")
    while True:
        try:
            state = check_once(state)
        except Exception as e:
            print(f"Error checking feed: {e}", file=sys.stderr)
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


if __name__ == "__main__":
    main()
