#!/usr/bin/env bash
# One-time (re-runnable) server setup for HerePilot-AI on Amazon Linux 2023. Run as root:
#     sudo bash server-setup.sh <s3-bucket>
# It downloads the code package from S3, installs nginx/Python/Node, installs the systemd units,
# builds the frontend and starts everything. It prints no secrets. Full log: /var/log/herepilot-setup.log
set -euo pipefail

BUCKET="${1:?usage: server-setup.sh <s3-bucket>}"
APP=/srv/herepilot-ai
LOG=/var/log/herepilot-setup.log
exec > >(tee -a "$LOG") 2>&1
step() { echo; echo "=== $(date -u +%H:%M:%S) $* ==="; }
export AWS_DEFAULT_REGION=eu-west-2

step "OS packages"
dnf -y -q install nginx python3.12 python3.12-pip git tar gzip openssl >/dev/null
# On Amazon Linux 2023 the npm for Node 22 is the separate package nodejs22-npm (plain "npm" is the Node 18 build).
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
    dnf -y -q install nodejs22 nodejs22-npm >/dev/null 2>&1 || dnf -y -q install nodejs20 nodejs20-npm >/dev/null
fi
echo "python $(python3.12 --version | cut -d' ' -f2), node $(node --version), npm $(npm --version), nginx $(nginx -v 2>&1 | cut -d/ -f2)"

step "Code package from s3://$BUCKET"
install -d -o ec2-user -g ec2-user -m 0755 "$APP"
aws s3 cp "s3://$BUCKET/herepilot-ai.tar.gz" /tmp/herepilot-ai.tar.gz --only-show-errors
aws s3 cp "s3://$BUCKET/no_network_guard.py" /tmp/no_network_guard.py --only-show-errors
# Keep code that already exists on re-runs: only unpack into an empty directory.
if [ -z "$(ls -A "$APP" 2>/dev/null)" ]; then
    tar -xzf /tmp/herepilot-ai.tar.gz -C "$APP" --strip-components=1
    chown -R ec2-user:ec2-user "$APP"
    echo "unpacked into $APP"
else
    echo "$APP is not empty: leaving existing code untouched"
fi
install -o ec2-user -g ec2-user -m 0644 /tmp/no_network_guard.py /home/ec2-user/no_network_guard.py
rm -f /tmp/herepilot-ai.tar.gz /tmp/no_network_guard.py

step "Install service files"
D="$APP/deploy/aws"
install -m 0755 "$D/sync-secrets.sh"        /usr/local/sbin/herepilot-sync-secrets
install -m 0755 "$D/build-frontend.sh"      /usr/local/bin/herepilot-build-frontend
install -m 0644 "$D/herepilot-secrets.service" /etc/systemd/system/herepilot-secrets.service
install -m 0644 "$D/herepilot-backend.service" /etc/systemd/system/herepilot-backend.service
[ -f /etc/nginx/nginx.conf.orig ] || cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.orig
install -m 0644 "$D/nginx.conf" /etc/nginx/nginx.conf
install -d -m 0755 /etc/herepilot
[ -f /etc/herepilot/backend.conf ] || cat > /etc/herepilot/backend.conf <<'CONF'
# Non-secret settings, one KEY=value per line. Add CORS_ALLOW_ORIGINS=https://<your-cloudfront-domain> here.
# Spend guard (see backend/services/llm/usage_guard.py): model calls allowed per rolling hour / per UTC day.
BEDROCK_HOURLY_CALL_LIMIT=100
BEDROCK_DAILY_CALL_LIMIT=600
BEDROCK_USAGE_STATE_FILE=/var/lib/herepilot/bedrock-usage.json
CONF
systemctl daemon-reload

step "Secrets from SSM into tmpfs"
systemctl enable --now herepilot-secrets.service
systemctl is-active herepilot-secrets.service

step "Backend: Python venv and dependencies"
sudo -u ec2-user -H bash -c "
    set -euo pipefail
    cd $APP/backend
    [ -d .venv ] || python3.12 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements-dev.txt
"
echo "backend dependencies installed"

step "Frontend build"
sudo -u ec2-user -H /usr/local/bin/herepilot-build-frontend 2>&1 | tail -n 8
test -f "$APP/frontend/dist/index.html"

step "Start nginx and the backend"
nginx -t
systemctl enable --now herepilot-backend.service nginx.service
systemctl restart herepilot-backend.service nginx.service
sleep 4
systemctl is-active herepilot-backend.service nginx.service

step "Local checks"
echo "backend /health      -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)"
echo "nginx, public site   -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/)  (expect 200)"
echo "setup finished"
