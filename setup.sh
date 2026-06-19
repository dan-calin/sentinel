#!/usr/bin/env bash
# Set up the Python environment for Sentinel — controller (CLI) or agent.
#
#   ./setup.sh            install the CLI / controller dependencies
#   ./setup.sh --agent    also install the agent (HTTP service) dependencies
#
# Idempotent: safe to re-run. Creates .venv if missing and installs deps.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c "import venv" >/dev/null 2>&1; then
  echo "The Python 'venv' module is missing. Install it and re-run, e.g.:" >&2
  echo "  sudo apt install -y python3-venv" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)…"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --quiet --upgrade pip
echo "Installing core requirements…"
pip install -r requirements.txt
if [ "${1:-}" = "--agent" ]; then
  echo "Installing agent requirements…"
  pip install -r agent/requirements.txt
fi

echo "Done. Activate later with:  source .venv/bin/activate"
