#!/usr/bin/env bash
# Installed as /usr/local/sbin/herepilot-sync-secrets and run by herepilot-secrets.service (root).
#
# Reads the HERE server key from SSM Parameter Store using the instance role and writes it to
# tmpfs under /run/herepilot. Nothing secret is written to disk or printed.
# To rotate it: update the SSM parameter, then `sudo systemctl restart herepilot-secrets`.
set -euo pipefail
umask 077

REGION="${AWS_REGION:-eu-west-2}"
PREFIX="/herepilot-ai"
RUN_DIR="/run/herepilot"

install -d -m 0755 "$RUN_DIR"

here_key="$(aws ssm get-parameter --region "$REGION" --name "$PREFIX/here-api-key" --with-decryption \
    --query 'Parameter.Value' --output text)"

tmp="$(mktemp -d "$RUN_DIR/.new.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

printf 'HERE_API_KEY=%s\n' "$here_key" > "$tmp/backend.env"
chmod 0600 "$tmp/backend.env"
mv -f "$tmp/backend.env" "$RUN_DIR/backend.env"
echo "herepilot-sync-secrets: wrote backend.env to $RUN_DIR"
