#!/usr/bin/env bash
# Installed as /usr/local/bin/herepilot-build-frontend. Run as ec2-user (not root).
#
# Builds frontend/dist. The API is reached at the relative path /api (nginx proxies it), and the
# HERE browser key is read from SSM Parameter Store so it never sits in a file in the repo.
# The key ends up inside the built JavaScript; that is how the HERE map works. Use a key
# restricted to your domain in the HERE portal, and keep the site behind the Basic-auth gate.
set -euo pipefail

cd /srv/herepilot-ai/frontend
export VITE_BACKEND_URL=/api
VITE_HERE_API_KEY="$(aws ssm get-parameter --region eu-west-2 --name /herepilot-ai/here-browser-key \
    --with-decryption --query 'Parameter.Value' --output text)"
export VITE_HERE_API_KEY

[ -d node_modules ] || npm ci --no-audit --no-fund
npm run build
# nginx runs as its own user. Vite copies public/ files with their original mode, and files created with
# owner-only permissions (600) would answer 403. Make everything in dist readable.
chmod -R a+rX dist
echo "herepilot-build-frontend: done"
