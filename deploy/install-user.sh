#!/usr/bin/env bash
set -euo pipefail

ARCHIVE=${1:-$HOME/tradew-release.tar.gz}
BASE=${TRADEW_APP_HOME:-$HOME/tradew-app}
CONFIG_DIR=${TRADEW_CONFIG_DIR:-$HOME/.config/tradew}
UNIT_DIR=$HOME/.config/systemd/user
RELEASE=$(date -u +%Y%m%dT%H%M%SZ)
TARGET=$BASE/releases/$RELEASE

test -s "$ARCHIVE"
mkdir -p "$TARGET" "$BASE/data" "$CONFIG_DIR" "$UNIT_DIR"
tar -xzf "$ARCHIVE" -C "$TARGET"
test -s "$TARGET/api_server.py"
test -s "$TARGET/frontend/dist/index.html"
ln -sfn "$TARGET" "$BASE/current"

if [ ! -s "$CONFIG_DIR/scan_config.json" ]; then
  cp "$TARGET/scan_config.example.json" "$CONFIG_DIR/scan_config.json"
fi
if [ ! -s "$CONFIG_DIR/llm_config.json" ]; then
  cp "$TARGET/llm_config.example.json" "$CONFIG_DIR/llm_config.json"
fi
chmod 600 "$CONFIG_DIR/scan_config.json" "$CONFIG_DIR/llm_config.json"

if [ ! -x "$BASE/venv/bin/python" ]; then
  python3 -m venv "$BASE/venv"
fi
"$BASE/venv/bin/pip" install --disable-pip-version-check \
  -r "$BASE/current/deploy/requirements-linux.txt"

TOKEN=''
AUDIT_REMOTE=''
CODEX_COMMAND=''
if [ -f "$CONFIG_DIR/tradew.env" ]; then
  TOKEN=$(sed -n 's/^TRADEW_BRIDGE_TOKEN=//p' "$CONFIG_DIR/tradew.env" | head -n 1)
  AUDIT_REMOTE=$(sed -n 's/^TRADEW_AUDIT_REMOTE=//p' "$CONFIG_DIR/tradew.env" | head -n 1)
  CODEX_COMMAND=$(sed -n 's/^TRADEW_CODEX_COMMAND=//p' "$CONFIG_DIR/tradew.env" | head -n 1)
fi
if [ -z "$TOKEN" ]; then
  TOKEN=$(openssl rand -hex 32)
fi
if [ -z "$CODEX_COMMAND" ]; then
  CODEX_COMMAND=$HOME/.local/bin/codex
fi

umask 077
cat > "$CONFIG_DIR/tradew.env" <<EOF
TRADEW_MT5_BRIDGE_URL=http://127.0.0.1:5010
TRADEW_BRIDGE_TOKEN=$TOKEN
TRADEW_MT5_TERMINAL_PATH="C:\\Program Files\\MetaTrader 5\\terminal64.exe"
TRADEW_GEMINI_TRANSPORT=local_worker
TRADEW_LLM_DEFAULT_PROVIDER=gemini
TRADEW_AUTO_SCAN_INTERVAL_SECONDS=1
TRADEW_AUTO_GEMINI=true
TRADEW_AUTO_GEMINI_MIN_SCORE=60
TRADEW_AUTO_GEMINI_REPEAT_SECONDS=900
TRADEW_AUDIT_ROTATE_BYTES=26214400
TRADEW_AUDIT_UPLOAD_INTERVAL_SECONDS=60
TRADEW_AUDIT_REMOTE=$AUDIT_REMOTE
TRADEW_RCLONE_COMMAND=$HOME/.local/bin/rclone
TRADEW_DATA_ROOT=$BASE/data
TRADEW_SCAN_CONFIG=$CONFIG_DIR/scan_config.json
TRADEW_LLM_CONFIG=$CONFIG_DIR/llm_config.json
TRADEW_CODEX_COMMAND=$CODEX_COMMAND
EOF
chmod 600 "$CONFIG_DIR/tradew.env"

install -m 0644 "$BASE/current/deploy/tradew-api.service" "$UNIT_DIR/tradew-api.service"
install -m 0644 "$BASE/current/deploy/tradew-mt5-bridge.service" "$UNIT_DIR/tradew-mt5-bridge.service"
if [ -s "$BASE/current/vps_gemini_reviewer.py" ] && [ -d "$HOME/tradew-gemini" ]; then
  install -m 0600 "$BASE/current/vps_gemini_reviewer.py" \
    "$HOME/tradew-gemini/vps_gemini_reviewer.py"
fi
systemctl --user daemon-reload

echo "Installed Tradew release $RELEASE"
