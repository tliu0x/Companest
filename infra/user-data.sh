#!/usr/bin/env bash
# Cloud-init for Companest EC2 (Ubuntu 24.04 ARM64)
# Only prepares the environment  code is deployed via `companest-ctl.sh deploy`
set -euo pipefail
exec > /var/log/companest-setup.log 2>&1

COMPANEST_HOME="/home/ubuntu/Companest"

echo "=== [1/3] System packages ==="
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv rsync

echo "=== [2/3] Docker ==="
apt-get install -y -qq docker.io docker-compose-v2
systemctl enable docker && systemctl start docker
usermod -aG docker ubuntu

echo "=== [3/3] Prepare directories ==="
sudo -u ubuntu mkdir -p "$COMPANEST_HOME"

echo "=== Setup complete ==="
echo "Run 'companest-ctl.sh deploy' from local machine to push code."
