#!/bin/bash
# AWS Cost Dashboard — One-shot setup for Ubuntu 22.04 / Amazon Linux 2023
# Run as root or with sudo.
set -e

APP_DIR=/opt/aws-cost-dashboard
FRONTEND_DIR=/var/www/aws-cost-dashboard
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
apt-get update -q
apt-get install -y python3 python3-venv nginx

echo "==> Creating app directories"
mkdir -p "$APP_DIR/backend" "$FRONTEND_DIR"

echo "==> Copying backend"
cp "$REPO_DIR/backend/app.py" "$APP_DIR/backend/"
cp "$REPO_DIR/backend/requirements.txt" "$APP_DIR/backend/"

echo "==> Creating virtualenv and installing deps"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

echo "==> Copying frontend"
cp "$REPO_DIR/frontend/index.html" "$FRONTEND_DIR/"

echo "==> Installing nginx config"
cp "$REPO_DIR/deploy/nginx.conf" /etc/nginx/sites-available/aws-cost-dashboard
ln -sf /etc/nginx/sites-available/aws-cost-dashboard /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==> Installing systemd service"
cp "$REPO_DIR/deploy/aws-cost-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable aws-cost-dashboard
systemctl restart aws-cost-dashboard

echo ""
echo "✓ Dashboard running at http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || hostname -I | awk '{print $1}')"
echo ""
echo "Prerequisites:"
echo "  1. Attach the IAM role with deploy/iam-policy.json to this EC2 instance"
echo "  2. Tag your AWS resources with 'Project=<name>' (or set PROJECT_TAG_KEY env var)"
echo "  3. Open port 80 in your Security Group (restrict to your IP for security)"
