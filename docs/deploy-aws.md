# Deploying RepoPilot on AWS

RepoPilot runs as one Docker Compose stack, so the smallest sensible deployment is a single EC2
instance. This guide sets one up for a team's private use. It is not hardened for running
untrusted repositories from the public internet: for that, swap the container runtime for gVisor
(step 6) or move sandboxes to Firecracker microVMs.

## What runs where

| Piece | Where |
|---|---|
| Postgres 16 + pgvector, Redis 7, sandbox-svc, api, worker | Containers on the instance (`docker compose --profile app`) |
| Review console (Next.js) | Same instance, `npm run build && npm start`, behind the reverse proxy |
| Reverse proxy with TLS | Caddy on the instance, the only thing listening publicly (443) |
| LLM | A hosted OpenAI-compatible API (no GPU needed), or Ollama on a GPU instance |
| Secrets | SSM Parameter Store, read at boot into `.env` |
| Traces (optional) | Langfuse Cloud through its OTLP endpoint |

## 1. Instance

- **Hosted model:** `t3.xlarge` (4 vCPU, 16 GiB), Amazon Linux 2023, 60 GiB gp3. Each sandbox is
  capped at 1 CPU and 1 GiB, so this runs about two agent runs at once.
- **Local model:** `g5.xlarge` and Ollama instead.
- **Security group:** inbound 443 from your office or VPN range only; nothing else. SSH through
  SSM Session Manager, not port 22.
- **Console access:** the console has no login of its own; anyone who reaches it can approve
  runs. Keep the security group tight, or add `basic_auth` (or an OIDC proxy) in the Caddyfile.
- **IAM role:** `AmazonSSMManagedInstanceCore`, plus read access to the parameters below.

## 2. Secrets in Parameter Store (SecureString)

```
/repopilot/REPOPILOT_PG_PASSWORD
/repopilot/REPOPILOT_SANDBOX_TOKEN      (32+ random characters)
/repopilot/REPOPILOT_API_TOKEN          (32+ random characters)
/repopilot/REPOPILOT_LLM_API_KEY
/repopilot/REPOPILOT_GITHUB_TOKEN       (fine-grained: issues read; contents + pull requests write)
/repopilot/REPOPILOT_LANGFUSE_SECRET_KEY   (optional)
```

The GitHub token is only ever used by the worker on the host side. It is not passed to
sandboxes.

## 3. Boot

[`deploy/aws/user-data.sh`](../deploy/aws/user-data.sh) does the following:

1. installs Docker, the Compose plugin, git and Node 22;
2. clones the repository;
3. writes `.env` from Parameter Store;
4. builds the sandbox images;
5. starts the stack and runs migrations;
6. installs Caddy as the TLS front for the console.

Pass it as EC2 user data, with `REPO_URL`, `DOMAIN` and `LLM_BASE_URL` set at the top.

## 4. Updating

```
cd /opt/repopilot && git pull
docker compose --profile app up -d --build
docker compose exec api repopilot migrate
(cd web && npm ci && npm run build) && sudo systemctl restart repopilot-web
```

## 5. Backups and data

- **Snapshots:** take daily EBS snapshots of the Docker volume disk (Data Lifecycle Manager).
- **Moving to RDS:** point `REPOPILOT_PG_HOST` at RDS for Postgres 16 with the `vector`
  extension enabled. LangGraph's checkpoint tables and RepoPilot's migrations both run there
  unchanged.

## 6. Stronger isolation

Install gVisor and register it as a Docker runtime:

```
# /etc/docker/daemon.json
{ "runtimes": { "runsc": { "path": "/usr/local/bin/runsc" } } }
```

Then set `HostConfig.Runtime = "runsc"` in sandbox-svc's container spec (one field in
`internal/sandbox/manager.go`). The sandbox's syscalls are then served by gVisor's user-space
kernel instead of the host kernel.

## 7. Costs to expect

| Item | Rough monthly cost |
|---|---|
| `t3.xlarge` on demand, plus disk | about $130 |
| Model usage | depends on the model; `repopilot eval` reports tokens and USD per resolved task |
