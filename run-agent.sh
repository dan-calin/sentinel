#!/usr/bin/env bash
# Run a Sentinel agent on THIS host so a controller can manage it.
# Sets up the venv on first run, generates + remembers tokens, and starts it.
#
#   ./run-agent.sh               bind 0.0.0.0:8765, generate read + admin tokens
#   ./run-agent.sh --read-only   diagnostics only (no admin token, no execute)
#   PORT=9000 ./run-agent.sh     pick a port (also: HOST=127.0.0.1)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  ./setup.sh --agent
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -c "import fastapi, uvicorn" >/dev/null 2>&1 || pip install -q -r agent/requirements.txt

gen_token() { openssl rand -hex 16 2>/dev/null || python -c "import secrets; print(secrets.token_hex(16))"; }

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
TOKENS_FILE=".agent-tokens.env"

# Persist tokens so restarts keep the same ones (the controller stays registered).
if [ -f "$TOKENS_FILE" ]; then
  # shellcheck disable=SC1090
  source "$TOKENS_FILE"
else
  SENTINEL_AGENT_READ_TOKEN="$(gen_token)"
  if [ "${1:-}" = "--read-only" ]; then
    SENTINEL_AGENT_ADMIN_TOKEN=""
  else
    SENTINEL_AGENT_ADMIN_TOKEN="$(gen_token)"
  fi
  {
    echo "SENTINEL_AGENT_READ_TOKEN=$SENTINEL_AGENT_READ_TOKEN"
    echo "SENTINEL_AGENT_ADMIN_TOKEN=$SENTINEL_AGENT_ADMIN_TOKEN"
  } > "$TOKENS_FILE"
  chmod 600 "$TOKENS_FILE"
fi
export SENTINEL_AGENT_READ_TOKEN SENTINEL_AGENT_ADMIN_TOKEN

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "──────────────────────────────────────────────────────────────"
echo " Sentinel agent — register this on your controller (host add):"
echo "   URL:          http://${IP:-<this-host-ip>}:$PORT"
echo "   READ token:   $SENTINEL_AGENT_READ_TOKEN"
echo "   ADMIN token:  ${SENTINEL_AGENT_ADMIN_TOKEN:-(none — read-only)}"
echo "   (saved in $TOKENS_FILE — reused on restart)"
echo " Bind is $HOST:$PORT — keep this on a trusted LAN/VPN and firewall it."
echo "──────────────────────────────────────────────────────────────"
exec uvicorn agent.server:app --host "$HOST" --port "$PORT"
