#!/bin/bash
# EC2 user data for a single-instance RepoPilot deployment (Amazon Linux 2023).
# See docs/deploy-aws.md. Set these three before launching:
REPO_URL="https://github.com/your-org/repopilot.git"
DOMAIN="repopilot.example.com"
LLM_BASE_URL="https://api.openai.com/v1"
LLM_MODEL="gpt-4.1-mini"

set -euo pipefail
exec > >(tee /var/log/repopilot-setup.log) 2>&1

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
cat > .env <<EOF
REPOPILOT_PG_PASSWORD=$(param REPOPILOT_PG_PASSWORD)
REPOPILOT_SANDBOX_TOKEN=$(param REPOPILOT_SANDBOX_TOKEN)
REPOPILOT_API_TOKEN=$(param REPOPILOT_API_TOKEN)
REPOPILOT_LLM_API_KEY=$(param REPOPILOT_LLM_API_KEY)
REPOPILOT_GITHUB_TOKEN=$(param REPOPILOT_GITHUB_TOKEN)
REPOPILOT_LLM_BASE_URL=$LLM_BASE_URL
REPOPILOT_LLM_MODEL=$LLM_MODEL
REPOPILOT_UI_ORIGIN=https://$DOMAIN
REPOPILOT_OTEL_ENDPOINT=$(optional_param REPOPILOT_OTEL_ENDPOINT)
REPOPILOT_LANGFUSE_PUBLIC_KEY=$(optional_param REPOPILOT_LANGFUSE_PUBLIC_KEY)
REPOPILOT_LANGFUSE_SECRET_KEY=$(optional_param REPOPILOT_LANGFUSE_SECRET_KEY)
EOF
chmod 600 .env

# The console needs only the API token and where it is served: not the GitHub token, the LLM
# key or the database password.
cat > web.env <<EOF
REPOPILOT_API_TOKEN=$(param REPOPILOT_API_TOKEN)
REPOPILOT_API_URL=http://127.0.0.1:8000
REPOPILOT_UI_HOSTS=$DOMAIN
EOF
chmod 600 web.env && chown ec2-user web.env

# Sandbox images, then the stack.
docker build -t repopilot-sandbox-base bench/images/base
for d in bench/images/*/; do
  name=$(basename "$d"); [ "$name" = base ] && continue
  docker build -t "repopilot-bench-$name" "$d"
done
docker compose --profile app up -d --build
docker compose exec -T api repopilot migrate

# Review console behind Caddy (automatic TLS).
(cd web && npm ci && npm run build)
cat > /etc/systemd/system/repopilot-web.service <<EOF
[Unit]
Description=RepoPilot review console
After=network.target
[Service]
WorkingDirectory=/opt/repopilot/web
EnvironmentFile=/opt/repopilot/web.env
ExecStart=/usr/bin/npm start
Restart=always
User=ec2-user
[Install]
WantedBy=multi-user.target
EOF
chown -R ec2-user /opt/repopilot/web
systemctl enable --now repopilot-web

dnf install -y 'dnf-command(copr)' && dnf copr enable -y @caddy/caddy && dnf install -y caddy
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
  reverse_proxy 127.0.0.1:3000
}
EOF
systemctl enable --now caddy
