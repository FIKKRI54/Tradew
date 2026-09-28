#!/usr/bin/env bash
set -euo pipefail

RCLONE=${RCLONE:-$HOME/.local/bin/rclone}
ENV_FILE=${TRADEW_ENV_FILE:-$HOME/.config/tradew/tradew.env}
REMOTE_NAME=tradew-drive
REMOTE_PATH="$REMOTE_NAME:Tradew/Audit"

test -x "$RCLONE"
test -s "$ENV_FILE"

echo "Configure a Google Drive remote named: $REMOTE_NAME"
echo "Use your own Google OAuth client ID and secret, choose drive.file scope,"
echo "and complete browser authorization. Quit the menu when it is saved."
echo
DISPLAY=${DISPLAY:-:2} "$RCLONE" config

if ! "$RCLONE" listremotes | grep -Fxq "$REMOTE_NAME:"; then
  echo "Remote $REMOTE_NAME was not created; no Tradew settings were changed." >&2
  exit 1
fi

"$RCLONE" mkdir "$REMOTE_PATH"
"$RCLONE" lsd "$REMOTE_NAME:Tradew" >/dev/null

temporary=$(mktemp "$HOME/.config/tradew/tradew.env.XXXXXX")
awk '!/^TRADEW_AUDIT_REMOTE=/' "$ENV_FILE" > "$temporary"
printf 'TRADEW_AUDIT_REMOTE=%s\n' "$REMOTE_PATH" >> "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$ENV_FILE"
systemctl --user restart tradew-api.service

echo "Google Drive audit upload enabled: $REMOTE_PATH"
echo "Check /api/status -> automation.audit_storage for upload progress."
