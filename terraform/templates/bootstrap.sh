#!/bin/sh
# Lightsail prepends its setup script and may execute the combined user data with sh.
# Invoke Bash explicitly instead of relying on this file's shebang being honored.
bash <<'BOT_RADIO_BOOTSTRAP'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https gnupg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
chmod a+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy
useradd --system --create-home --home-dir /var/lib/the-bot-radio --shell /usr/sbin/nologin bot-radio
install -d -o bot-radio -g bot-radio /var/lib/the-bot-radio/data /opt/the-bot-radio/releases
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
uv python install 3.13
chmod -R a+rX /opt/uv/python
cat > /etc/systemd/system/the-bot-radio.service <<'UNIT'
[Unit]
Description=The Bot Radio FastAPI website
After=network-online.target
Wants=network-online.target
ConditionPathExists=/opt/the-bot-radio/current/.venv/bin/uvicorn

[Service]
Type=simple
User=bot-radio
Group=bot-radio
WorkingDirectory=/opt/the-bot-radio/current
Environment=BOT_RADIO_DATA_DIR=/var/lib/the-bot-radio/data
Environment=BOT_RADIO_SITE_URL=https://thebotradio.com
Environment=TZ=America/New_York
ExecStart=/opt/the-bot-radio/current/.venv/bin/uvicorn the_bot_radio.web.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/the-bot-radio/data

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/caddy/Caddyfile <<'CADDY'
thebotradio.com, www.thebotradio.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
CADDY
caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable the-bot-radio.service
systemctl enable caddy
systemctl restart caddy
touch /var/lib/the-bot-radio/bootstrap-complete
BOT_RADIO_BOOTSTRAP
