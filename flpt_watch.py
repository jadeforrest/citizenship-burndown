#!/usr/bin/env python3
"""
FLPT feed watcher.

Polls IRCC's published processing-times JSON feed, reports the current
"citizen-proofs-2025/07" backlog figure, and beeps once whenever
flpt_lastupdated changes (i.e. IRCC has refreshed the feed with new data).

Usage:
    python3 flpt_watch.py            # run forever, checking hourly
    python3 flpt_watch.py --once     # check once and exit (cron/testing)
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.canada.ca/content/dam/ircc/documents/json/flpt-en.json"
FIELD_SECTION = "people-ahead"
FIELD_KEY = "citizen-proofs-2025/07"
STATE_PATH = Path(__file__).with_name("flpt_state.json")
CHECK_INTERVAL_SECONDS = 3600


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


def check_once(state: dict) -> dict:
    data = fetch()
    last_updated = data.get("default-update", {}).get("flpt_lastupdated")
    backlog = data.get(FIELD_SECTION, {}).get(FIELD_KEY)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev_last_updated = state.get("flpt_lastupdated")

    changed = prev_last_updated is not None and last_updated != prev_last_updated
    if changed:
        print(f"[{now}] flpt_lastupdated changed: {prev_last_updated!r} -> {last_updated!r}")
        beep()
    else:
        print(f"[{now}] flpt_lastupdated unchanged: {last_updated!r}")

    print(f"[{now}] {FIELD_KEY}: {backlog!r}")

    state["flpt_lastupdated"] = last_updated
    state[FIELD_KEY] = backlog
    state["last_checked"] = now
    save_state(state)
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
            sys.exit(1)
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
