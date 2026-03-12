#!/usr/bin/env bash
#
# companest-ctl  local control for Companest EC2 instance
#
# Usage:
#   ./companest-ctl.sh create    First-time: terraform apply (creates everything)
#   ./companest-ctl.sh deploy    rsync code to EC2 + restart all services
#   ./companest-ctl.sh up        Start stopped instance (~20s)
#   ./companest-ctl.sh down      Stop instance (keeps data, stops billing compute)
#   ./companest-ctl.sh ssh       SSH into instance
#   ./companest-ctl.sh status    Show instance state
#   ./companest-ctl.sh logs      Tail Companest logs via SSH
#   ./companest-ctl.sh litellm-logs  Tail LiteLLM proxy logs
#   ./companest-ctl.sh openbb-logs   Tail OpenBB API server logs
#   ./companest-ctl.sh destroy   Tear down everything (terraform destroy)
#
set -euo pipefail
cd "$(dirname "$0")"

# Read instance ID and region from terraform state
get_instance_id() {
  terraform output -raw instance_id 2>/dev/null
}

get_region() {
  grep -o '"region":"[^"]*"' terraform.tfvars 2>/dev/null | head -1 | cut -d'"' -f4 || \
  grep 'aws_region' terraform.tfvars 2>/dev/null | head -1 | sed 's/.*= *"\(.*\)"/\1/' || \
  echo "us-east-1"
}

get_ssh_cmd() {
  terraform output -raw ssh_command 2>/dev/null
}

get_key_path() {
  terraform output -raw ssh_command 2>/dev/null | sed 's/.*-i \([^ ]*\).*/\1/'
}

REGION=$(get_region)
REPO_ROOT="$(cd .. && pwd)"

case "${1:-help}" in

  create)
    echo "Creating Companest infrastructure..."
    terraform init
    terraform apply
    echo ""
    echo "Infrastructure created! Wait ~2 min for cloud-init, then:"
    echo "  ./companest-ctl.sh deploy"
    ;;

  deploy)
    KEY_PATH=$(get_key_path)
    COMPANEST_IP=$(terraform output -raw public_ip)
    SSH_OPTS="-i $KEY_PATH -o StrictHostKeyChecking=no"

    echo "Deploying to $COMPANEST_IP..."

    # 1. rsync code to EC2
    rsync -az --delete \
      --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
      --exclude 'infra/.terraform' --exclude 'infra/*.tfstate*' \
      --exclude '.claude' --exclude '.env' --exclude 'deploy/litellm/.env' \
      --exclude '._*' --exclude '__MACOSX' --exclude '.DS_Store' \
      -e "ssh $SSH_OPTS" \
      "$REPO_ROOT/" "ubuntu@$COMPANEST_IP:/home/ubuntu/Companest/"

    # 2. Upload .env (contains API keys, not included in rsync)
    if [ -f "$REPO_ROOT/deploy/.env" ]; then
      echo "Uploading deploy/.env..."
      scp $SSH_OPTS "$REPO_ROOT/deploy/.env" "ubuntu@$COMPANEST_IP:/home/ubuntu/Companest/deploy/.env"
    else
      echo "WARNING: deploy/.env not found locally. Remote will use .env.example defaults."
    fi

    echo "Setting up remote..."

    # 3. Install deps + setup systemd + restart Companest
    ssh $SSH_OPTS "ubuntu@$COMPANEST_IP" bash <<'REMOTE'
    set -euo pipefail
    cd /home/ubuntu/Companest

    # Install Python deps
    python3 -m pip install --break-system-packages -q -r requirements.txt
    python3 -m pip install --break-system-packages -q python-telegram-bot -e .

    # Fallback: create .env from example if neither local nor remote exists
    [ -f deploy/.env ] || cp deploy/.env.example deploy/.env

    # Install systemd services (idempotent)
    PYTHON_BIN=$(which python3)
    sudo tee /etc/systemd/system/master-gateway.service > /dev/null <<UNIT
[Unit]
Description=Companest Master Gateway (Telegram + WS)
After=network.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Companest
ExecStart=$PYTHON_BIN scripts/master_gateway.py --port 19000
Restart=always
RestartSec=5
EnvironmentFile=/home/ubuntu/Companest/deploy/.env
[Install]
WantedBy=multi-user.target
UNIT

    sudo tee /etc/systemd/system/companest.service > /dev/null <<UNIT
[Unit]
Description=Companest Orchestrator
After=master-gateway.service
Requires=master-gateway.service
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Companest
ExecStart=$PYTHON_BIN -m companest serve -c /home/ubuntu/Companest/deploy/config.prod.md
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/Companest/deploy/.env
[Install]
WantedBy=multi-user.target
UNIT

    # Start LiteLLM proxy (Docker)
    cd /home/ubuntu/Companest/deploy/litellm
    docker compose down 2>/dev/null || true
    docker compose up -d
    echo "Waiting for LiteLLM..."
    for i in $(seq 1 30); do
      curl -sf http://127.0.0.1:4000/health > /dev/null 2>&1 && break
      sleep 2
    done
    if curl -sf http://127.0.0.1:4000/health > /dev/null 2>&1; then
      echo "LiteLLM healthy."
    else
      echo "WARNING: LiteLLM health check failed after 60s. Check 'docker compose logs'."
    fi
    cd /home/ubuntu/Companest

    # Install OpenBB API server (financial data)
    if [ -f deploy/openbb/setup.sh ]; then
      echo "Setting up OpenBB API..."
      bash deploy/openbb/setup.sh
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable master-gateway companest
    sudo systemctl restart master-gateway companest

    echo "Deploy complete. LiteLLM (Docker) + OpenBB + Companest + Gateway all running."
REMOTE
    ;;

  up)
    ID=$(get_instance_id)
    echo "Starting $ID..."
    aws ec2 start-instances --region "$REGION" --instance-ids "$ID" --output text
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID"
    IP=$(terraform output -raw public_ip)
    echo "Running! IP: $IP"
    echo ""
    echo "Services auto-start with the instance."
    echo "  ./companest-ctl.sh ssh"
    ;;

  down)
    ID=$(get_instance_id)
    echo "Stopping $ID... (data preserved, compute billing stops)"
    aws ec2 stop-instances --region "$REGION" --instance-ids "$ID" --output text
    aws ec2 wait instance-stopped --region "$REGION" --instance-ids "$ID"
    echo "Stopped."
    ;;

  ssh)
    SSH_CMD=$(get_ssh_cmd)
    echo "$SSH_CMD"
    exec $SSH_CMD
    ;;

  status)
    ID=$(get_instance_id)
    aws ec2 describe-instance-status \
      --region "$REGION" \
      --instance-ids "$ID" \
      --include-all-instances \
      --query 'InstanceStatuses[0].{State:InstanceState.Name,System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
      --output table
    ;;

  logs)
    SSH_CMD=$(get_ssh_cmd)
    exec $SSH_CMD -t "journalctl -u master-gateway -u companest -f"
    ;;

  litellm-logs)
    SSH_CMD=$(get_ssh_cmd)
    exec $SSH_CMD -t "cd /home/ubuntu/Companest/deploy/litellm && docker compose logs -f --tail=100"
    ;;

  openbb-logs)
    SSH_CMD=$(get_ssh_cmd)
    exec $SSH_CMD -t "journalctl -u openbb-api -f"
    ;;

  destroy)
    echo "This will DELETE the instance and all data."
    read -rp "Type 'yes' to confirm: " confirm
    if [ "$confirm" = "yes" ]; then
      terraform destroy
    else
      echo "Cancelled."
    fi
    ;;

  *)
    echo "Usage: ./companest-ctl.sh {create|deploy|up|down|ssh|status|logs|litellm-logs|openbb-logs|destroy}"
    echo ""
    echo "  create        First-time setup (terraform apply)"
    echo "  deploy        rsync code to EC2 + restart all services"
    echo "  up            Start stopped instance (~20s)"
    echo "  down          Stop instance (keep data, stop billing)"
    echo "  ssh           SSH into instance"
    echo "  status        Show instance state"
    echo "  logs          Tail Companest + Gateway logs"
    echo "  litellm-logs  Tail LiteLLM proxy logs"
    echo "  openbb-logs   Tail OpenBB API server logs"
    echo "  destroy       Delete everything"
    ;;
esac
