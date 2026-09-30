#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=${1:?Usage: publish-frontend.sh <release-root>}
WEB_ROOT=/var/www/tradew
DIST=$SOURCE_ROOT/frontend/dist
test -s "$DIST/index.html"
test -d "$WEB_ROOT"

# Keep hashed assets for browsers with an older page still open. Publish the
# entry page last so it never references assets that have not been copied yet.
while IFS= read -r -d '' file; do
  relative=${file#"$DIST/"}
  if [ "$relative" = index.html ]; then
    continue
  fi
  destination=$WEB_ROOT/$relative
  if [ -f "$destination" ] && cmp -s "$file" "$destination"; then
    continue
  fi
  mkdir -p "$(dirname "$destination")"
  temporary=$(mktemp "$(dirname "$destination")/.tradew-publish.XXXXXX")
  cp "$file" "$temporary"
  chmod 0644 "$temporary"
  mv -f "$temporary" "$destination"
done < <(find "$DIST" -type f -print0)

if [ -f "$WEB_ROOT/index.html" ]; then
  cp "$WEB_ROOT/index.html" "$SOURCE_ROOT/frontend/previous-index.html"
fi
temporary=$(mktemp "$WEB_ROOT/.tradew-index.XXXXXX")
cp "$DIST/index.html" "$temporary"
chmod 0644 "$temporary"
mv -f "$temporary" "$WEB_ROOT/index.html"
cmp -s "$DIST/index.html" "$WEB_ROOT/index.html"
echo "Published frontend to $WEB_ROOT"
