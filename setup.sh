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

# Create the venv if missing OR incomplete. A failed attempt (e.g. the
# python3-venv package isn't installed) can leave a partial .venv with no
# activate script, so check for activate, not just the directory.
if [ ! -f .venv/bin/activate ]; then
  rm -rf .venv
  echo "Creating virtual environment (.venv)…"
  if ! "$PYTHON" -m venv .venv || [ ! -f .venv/bin/activate ]; then
    rm -rf .venv
    echo >&2
    echo "Could not create the virtual environment." >&2
    echo "On Debian/Ubuntu/Mint, install the venv package and re-run:" >&2
    echo "  sudo apt install -y python3-venv" >&2
    exit 1
  fi
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
