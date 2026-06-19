#!/usr/bin/env bash
# Run a Sentinel agent on THIS host so a controller can manage it.
# First run offers to install it as an always-on systemd service with periodic
# health checks; otherwise it runs in the foreground.
#
#   ./run-agent.sh               foreground, generate read + admin tokens
#   ./run-agent.sh --read-only   diagnostics only (no admin token, no execute)
#   ./run-agent.sh --service     install as a boot service (non-interactive yes)
#   PORT=9000 ./run-agent.sh     pick a port (also: HOST=127.0.0.1)
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
UNIT_PATH="/etc/systemd/system/sentinel-agent.service"

if [ ! -f .venv/bin/activate ]; then
  ./setup.sh --agent
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -c "import fastapi, uvicorn" >/dev/null 2>&1 || pip install -q -r agent/requirements.txt

gen_token() { openssl rand -hex 16 2>/dev/null || python -c "import secrets; print(secrets.token_hex(16))"; }

READ_ONLY=""
FORCE_SERVICE=""
for arg in "$@"; do
  case "$arg" in
    --read-only) READ_ONLY=1 ;;
    --service)   FORCE_SERVICE=1 ;;
  esac
done

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
TOKENS_FILE="$REPO/.agent-tokens.env"

# Persist tokens so restarts (and the service) keep the same ones.
if [ -f "$TOKENS_FILE" ]; then
  # shellcheck disable=SC1090
  source "$TOKENS_FILE"
else
  SENTINEL_AGENT_READ_TOKEN="$(gen_token)"
  if [ -n "$READ_ONLY" ]; then SENTINEL_AGENT_ADMIN_TOKEN=""; else SENTINEL_AGENT_ADMIN_TOKEN="$(gen_token)"; fi
  {
    echo "SENTINEL_AGENT_READ_TOKEN=$SENTINEL_AGENT_READ_TOKEN"
    echo "SENTINEL_AGENT_ADMIN_TOKEN=$SENTINEL_AGENT_ADMIN_TOKEN"
  } > "$TOKENS_FILE"
  chmod 600 "$TOKENS_FILE"
fi
export SENTINEL_AGENT_READ_TOKEN SENTINEL_AGENT_ADMIN_TOKEN

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
print_registration() {
  echo "──────────────────────────────────────────────────────────────"
  echo " Sentinel agent — register this on your controller (host add):"
  echo "   URL:          http://${IP:-<this-host-ip>}:$PORT"
  echo "   READ token:   $SENTINEL_AGENT_READ_TOKEN"
  echo "   ADMIN token:  ${SENTINEL_AGENT_ADMIN_TOKEN:-(none — read-only)}"
  echo "──────────────────────────────────────────────────────────────"
}

enable_monitor() {
  python -c "import health; c=health.load_monitor_config(); c.enabled=True; health.save_monitor_config(c)" \
    && echo "Periodic health checks enabled (every ${1:-300}s)."
}

install_service() {
  echo "Installing the systemd service (needs sudo)…"
  sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Sentinel agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO
EnvironmentFile=$TOKENS_FILE
ExecStart=$REPO/.venv/bin/uvicorn agent.server:app --host $HOST --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now sentinel-agent
  echo
  echo "Service installed and started. Manage it with:"
  echo "   systemctl status sentinel-agent      # health"
  echo "   journalctl -u sentinel-agent -f      # live logs"
  echo "   sudo systemctl stop sentinel-agent   # stop"
  print_registration
}

# Already installed as a service? Don't start a conflicting foreground copy.
if [ -f "$UNIT_PATH" ] && [ -z "$FORCE_SERVICE" ]; then
  echo "Sentinel agent is already installed as a service."
  systemctl --no-pager --lines=0 status sentinel-agent 2>/dev/null || true
  print_registration
  exit 0
fi

# Offer the always-on install on first run (interactive), or force with --service.
if [ -n "$FORCE_SERVICE" ] || { [ -t 0 ] && [ ! -f "$UNIT_PATH" ]; }; then
  do_install="$FORCE_SERVICE"
  if [ -z "$do_install" ]; then
    read -r -p "Install Sentinel as an always-on service that starts on boot? [y/N] " ans
    case "$ans" in [yY]*) do_install=1 ;; esac
  fi
  if [ -n "$do_install" ]; then
    if command -v sudo >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
      enable_health=1
      if [ -z "$FORCE_SERVICE" ]; then
        read -r -p "Enable periodic health checks now? [Y/n] " ans2
        case "$ans2" in [nN]*) enable_health="" ;; esac
      fi
      [ -n "$enable_health" ] && enable_monitor "$PORT"
      install_service
      exit 0
    else
      echo "sudo or systemctl not available — running in the foreground instead."
    fi
  fi
fi

# Foreground run.
print_registration
echo " Bind is $HOST:$PORT — keep this on a trusted LAN/VPN and firewall it."
echo " (saved tokens in .agent-tokens.env; re-run with --service to install on boot)"
exec uvicorn agent.server:app --host "$HOST" --port "$PORT"
