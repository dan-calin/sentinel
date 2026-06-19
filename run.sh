#!/usr/bin/env bash
# Launch the Sentinel CLI (the controller). Sets up the venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .venv/bin/activate ]; then
  ./setup.sh
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python main.py "$@"
