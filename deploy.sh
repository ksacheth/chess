#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="oracle"
REMOTE_DIR="/home/ubuntu/chess"

echo "==> Syncing files to ${TARGET_HOST}:${REMOTE_DIR}..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude 'venv' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    ./ "${TARGET_HOST}:${REMOTE_DIR}/"

echo "==> Ensuring virtualenv dependencies and restarting service..."
ssh "${TARGET_HOST}" "cd ${REMOTE_DIR} && ./venv/bin/pip install -q -r requirements.txt && sudo systemctl restart chess"

echo "==> Checking service status..."
ssh "${TARGET_HOST}" "sudo systemctl status chess --no-pager"

echo "==> Deployment complete!"
