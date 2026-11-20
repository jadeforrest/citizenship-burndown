#!/bin/bash
# Fast-pass gate: tests under coverage, CRAP for touched functions, clones.
#   ./check.sh            score uncommitted changes (against HEAD)
#   ./check.sh main~3     score everything changed since main~3
#   CRAP_ALL=1 ./check.sh list every function's score
# Dev tools live in .venv (requirements-dev.txt); the tests stay stdlib-only.
set -euo pipefail
cd "$(dirname "$0")"
ref="${1:-HEAD}"

if [ ! -x .venv/bin/python ] || ! cmp -s requirements-dev.txt .venv/requirements-dev.txt; then
  python3 -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check -r requirements-dev.txt
  cp requirements-dev.txt .venv/requirements-dev.txt
fi

export PYTHONDONTWRITEBYTECODE=1
.venv/bin/coverage run --branch -m unittest test_burndown test_flpt_watch test_crap
.venv/bin/coverage report --include=burndown.py,flpt_watch.py,crap.py --skip-covered --show-missing
.venv/bin/coverage json -q -o coverage.json --include=burndown.py,flpt_watch.py,crap.py
.venv/bin/python crap.py coverage.json --ref "$ref" ${CRAP_ALL:+--all}

if command -v npx >/dev/null; then
  npx -y jscpd@4.0.5 .
else
  echo "npx not found: skipping the clone check (jscpd)" >&2
fi
