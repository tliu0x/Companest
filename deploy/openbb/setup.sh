#!/usr/bin/env bash
#
# OpenBB API Server Setup
#
# Installs OpenBB and configures it as a systemd service.
# Run this on the target EC2 instance (or call from companest-ctl.sh deploy).
#
# The OpenBB API server provides financial data (equity, crypto, economy, news)
# via REST endpoints that Companest info-collection fetches from.
#
set -euo pipefail

OPENBB_PORT="${OPENBB_PORT:-6900}"
PYTHON_BIN="${PYTHON_BIN:-$(which python3)}"

echo "=== Installing OpenBB ==="
$PYTHON_BIN -m pip install --break-system-packages -q openbb

echo "=== Creating systemd service ==="
sudo tee /etc/systemd/system/openbb-api.service > /dev/null <<UNIT
[Unit]
Description=OpenBB API Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu
ExecStart=$PYTHON_BIN -m uvicorn openbb_core.api.rest_api:app --host 127.0.0.1 --port $OPENBB_PORT
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/Companest/deploy/.env

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable openbb-api
sudo systemctl restart openbb-api

echo "Waiting for OpenBB API..."
for i in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$OPENBB_PORT/docs" > /dev/null 2>&1 && break
  sleep 2
done

if curl -sf "http://127.0.0.1:$OPENBB_PORT/docs" > /dev/null 2>&1; then
  echo "OpenBB API healthy on port $OPENBB_PORT"
else
  echo "WARNING: OpenBB API not responding after 60s. Check: journalctl -u openbb-api"
fi
