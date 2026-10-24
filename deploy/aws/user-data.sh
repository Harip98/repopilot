#!/bin/bash
# EC2 user data for a single-instance RepoPilot deployment (Amazon Linux 2023).
# See docs/deploy-aws.md. Set these four before launching:
REPO_URL="https://github.com/your-org/repopilot.git"
DOMAIN="repopilot.example.com"
LLM_BASE_URL="https://api.openai.com/v1"
LLM_MODEL="gpt-4.1-mini"
# Optional:
# USD per 1M prompt / completion tokens, from your provider's price list (0: cost not tracked).
LLM_PRICE_IN="0"
LLM_PRICE_OUT="0"
# "internal": a certificate from Caddy's own CA, for the VPN-only security group in the guide.
# "acme": a public Let's Encrypt/ZeroSSL certificate; needs 80 and 443 open to the internet.
TLS_MODE="internal"
# The run page's "Open trace" link, with {trace_id} in place of the id (empty: no link).
TRACE_URL=""

set -euo pipefail
exec > >(tee /var/log/repopilot-setup.log) 2>&1
case "$TLS_MODE" in internal|acme) ;; *) echo "TLS_MODE must be internal or acme" >&2; exit 1 ;; esac

dnf install -y docker git
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
dnf install -y nodejs22 || (curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - && dnf install -y nodejs)

git clone "$REPO_URL" /opt/repopilot
cd /opt/repopilot

param() { aws ssm get-parameter --with-decryption --name "/repopilot/$1" --query Parameter.Value --output text; }
optional_param() { param "$1" 2>/dev/null || true; }
# Compose reads this file and passes the settings to the api and worker (gitignored).
cat > .env <<EOF
REPOPILOT_PG_PASSWORD=$(param REPOPILOT_PG_PASSWORD)
REPOPILOT_SANDBOX_TOKEN=$(param REPOPILOT_SANDBOX_TOKEN)
REPOPILOT_API_TOKEN=$(param REPOPILOT_API_TOKEN)
REPOPILOT_LLM_API_KEY=$(param REPOPILOT_LLM_API_KEY)
REPOPILOT_GITHUB_TOKEN=$(param REPOPILOT_GITHUB_TOKEN)
REPOPILOT_LLM_BASE_URL=$LLM_BASE_URL
REPOPILOT_LLM_MODEL=$LLM_MODEL
REPOPILOT_LLM_PRICE_IN=$LLM_PRICE_IN
REPOPILOT_LLM_PRICE_OUT=$LLM_PRICE_OUT
REPOPILOT_UI_ORIGIN=https://$DOMAIN
REPOPILOT_OTEL_ENDPOINT=$(optional_param REPOPILOT_OTEL_ENDPOINT)
REPOPILOT_LANGFUSE_PUBLIC_KEY=$(optional_param REPOPILOT_LANGFUSE_PUBLIC_KEY)
REPOPILOT_LANGFUSE_SECRET_KEY=$(optional_param REPOPILOT_LANGFUSE_SECRET_KEY)
EOF
chmod 600 .env

# The console needs only the API token and where it is served: not the GitHub token, the LLM
# key or the database password. Kept outside the checkout; systemd reads it as root.
install -d -m 700 /etc/repopilot
cat > /etc/repopilot/web.env <<EOF
REPOPILOT_API_TOKEN=$(param REPOPILOT_API_TOKEN)
REPOPILOT_API_URL=http://127.0.0.1:8000
REPOPILOT_UI_HOSTS=$DOMAIN
EOF
chmod 600 /etc/repopilot/web.env

# Sandbox images, then the stack.
docker build -t repopilot-sandbox-base bench/images/base
for d in bench/images/*/; do
  name=$(basename "$d"); [ "$name" = base ] && continue
  docker build -t "repopilot-bench-$name" "$d"
done
docker compose --profile app up -d --build
docker compose exec -T api repopilot migrate

# Review console behind Caddy. NEXT_PUBLIC_* values are inlined at build time, so the trace
# link goes in a file `next build` reads (gitignored), and later rebuilds keep it.
if [ -n "$TRACE_URL" ]; then
  echo "NEXT_PUBLIC_TRACE_URL=$TRACE_URL" > web/.env.production.local
fi
(cd web && npm ci && npm run build)
cat > /etc/systemd/system/repopilot-web.service <<EOF
[Unit]
Description=RepoPilot review console
After=network.target
[Service]
WorkingDirectory=/opt/repopilot/web
EnvironmentFile=/etc/repopilot/web.env
ExecStart=/usr/bin/npm start
Restart=always
User=ec2-user
[Install]
WantedBy=multi-user.target
EOF
chown -R ec2-user /opt/repopilot/web
systemctl enable --now repopilot-web

dnf install -y 'dnf-command(copr)' && dnf copr enable -y @caddy/caddy && dnf install -y caddy
if [ "$TLS_MODE" = internal ]; then
  TLS_DIRECTIVE="tls internal"  # clients must trust Caddy's root CA: see docs/deploy-aws.md
else
  TLS_DIRECTIVE=""  # automatic public certificate over ACME
fi
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
  $TLS_DIRECTIVE
  reverse_proxy 127.0.0.1:3000
}
EOF
systemctl enable --now caddy
