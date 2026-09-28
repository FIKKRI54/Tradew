#!/usr/bin/env bash
set -euo pipefail

DOMAIN=${1:-}
ORIGIN_IP=${2:-}
APP_USER=${3:-${SUDO_USER:-}}

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo: install-nginx-root.sh <domain> <origin-ip> [app-user]' >&2
  exit 1
fi
if [ -z "$DOMAIN" ] || [ -z "$ORIGIN_IP" ] || [ -z "$APP_USER" ]; then
  echo 'Usage: install-nginx-root.sh <domain> <origin-ip> [app-user]' >&2
  exit 1
fi

APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
test -n "$APP_HOME"
SOURCE_ROOT=$APP_HOME/tradew-app/current
CLIENT_CA=$APP_HOME/.config/tradew/device-ca.crt
SITE=/etc/nginx/sites-available/tradew

if ! getent ahostsv4 "$DOMAIN" | awk '{print $1}' | grep -Fxq "$ORIGIN_IP"; then
  echo "$DOMAIN must first resolve directly to $ORIGIN_IP." >&2
  exit 1
fi

test -s "$CLIENT_CA"
test -s "$SOURCE_ROOT/frontend/dist/index.html"

install -d -m 0755 /var/www/tradew /var/www/tradew-acme
cp -a "$SOURCE_ROOT/frontend/dist/." /var/www/tradew/
chown -R root:root /var/www/tradew
install -m 0644 "$CLIENT_CA" /etc/nginx/tradew-client-ca.crt

if ! command -v certbot >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y certbot
fi

if [ ! -s "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  cat > "$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/tradew-acme; }
    location / { return 404; }
}
EOF
  ln -sfn "$SITE" /etc/nginx/sites-enabled/tradew
  nginx -t
  systemctl reload nginx
  certbot certonly --webroot -w /var/www/tradew-acme -d "$DOMAIN" \
    --non-interactive --agree-tos --register-unsafely-without-email
fi

cat > "$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_client_certificate /etc/nginx/tradew-client-ca.crt;
    ssl_verify_client on;

    root /var/www/tradew;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF

ln -sfn "$SITE" /etc/nginx/sites-enabled/tradew
nginx -t
systemctl reload nginx
echo "Tradew HTTPS and client-certificate enforcement are active on $DOMAIN."
