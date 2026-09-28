#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR=${TRADEW_CONFIG_DIR:-$HOME/.config/tradew}
mkdir -p "$CONFIG_DIR"
umask 077
cd "$CONFIG_DIR"

if [ -s personal-device.p12 ] && [ -s personal-device-password.txt ] && [ -s device-ca.crt ]; then
  echo "Existing Tradew device certificate retained."
  exit 0
fi

openssl genrsa -out device-ca.key 4096
openssl req -x509 -new -sha256 -days 3650 -key device-ca.key \
  -out device-ca.crt -subj '/CN=Tradew Personal Device CA'

openssl genrsa -out personal-device.key 3072
openssl req -new -key personal-device.key -out personal-device.csr \
  -subj '/CN=Tradew Personal Device'
cat > personal-device.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -sha256 -days 825 -in personal-device.csr \
  -CA device-ca.crt -CAkey device-ca.key -CAcreateserial \
  -extfile personal-device.ext -out personal-device.crt

PASSWORD=$(openssl rand -hex 24)
printf '%s\n' "$PASSWORD" > personal-device-password.txt
openssl pkcs12 -export -out personal-device.p12 \
  -inkey personal-device.key -in personal-device.crt -certfile device-ca.crt \
  -name 'Tradew Personal Device' -passout "pass:$PASSWORD"

chmod 600 device-ca.key personal-device.key personal-device.p12 personal-device-password.txt
chmod 644 device-ca.crt personal-device.crt
echo "Created the Tradew personal-device client certificate."
