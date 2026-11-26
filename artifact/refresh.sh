#!/bin/bash
# Regenerate the shared timeline page's data (burndown.py --json) into
# data.json.new and say whether it differs from the published data.json.
# Prints CHANGED (data.json.new left for publishing) or UNCHANGED (removed).
# The daily "citizenship-artifact" scheduled task runs this, publishes
# data.json.new only on CHANGED, and then moves it over data.json, so a failed
# publish is retried the next day.
set -euo pipefail
cd "$(dirname "$0")"
python3 ../burndown.py --json > data.json.new
if [ -f data.json ] && cmp -s data.json data.json.new; then
    rm data.json.new
    echo UNCHANGED
else
    echo CHANGED
fi
