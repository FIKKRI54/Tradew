#!/usr/bin/env bash
set -euo pipefail

SLUG=${1:-}
LABEL=${2:-}
CONFIG_DIR=${TRADEW_CONFIG_DIR:-$HOME/.config/tradew}

if ! [[ "$SLUG" =~ ^[a-z0-9-]+$ ]] || [ -z "$LABEL" ]; then
  echo 'Usage: issue-device-certificate.sh <safe-name> <display-label>' >&2
  exit 1
fi
test -s "$CONFIG_DIR/device-ca.key"
test -s "$CONFIG_DIR/device-ca.crt"

umask 077
cd "$CONFIG_DIR"
if [ -s "$SLUG.p12" ] && [ -s "$SLUG-password.txt" ]; then
  echo "Existing $LABEL certificate retained."
  exit 0
fi

openssl genrsa -out "$SLUG.key" 3072
openssl req -new -key "$SLUG.key" -out "$SLUG.csr" -subj "/CN=$LABEL"
cat > "$SLUG.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -sha256 -days 825 -in "$SLUG.csr" \
  -CA device-ca.crt -CAkey device-ca.key -CAcreateserial \
  -extfile "$SLUG.ext" -out "$SLUG.crt"

PASSWORD=$(openssl rand -hex 24)
printf '%s\n' "$PASSWORD" > "$SLUG-password.txt"
openssl pkcs12 -export -out "$SLUG.p12" -inkey "$SLUG.key" \
  -in "$SLUG.crt" -certfile device-ca.crt -name "$LABEL" \
  -passout "pass:$PASSWORD"
chmod 600 "$SLUG.key" "$SLUG.p12" "$SLUG-password.txt"
chmod 644 "$SLUG.crt"
echo "Created $LABEL client certificate."
