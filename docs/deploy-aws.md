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
| Reverse proxy with TLS | Caddy on the instance, the only thing the security group exposes (443) |
| LLM | A hosted OpenAI-compatible API (no GPU needed), or Ollama on a GPU instance |
| Secrets | SSM Parameter Store, read at boot into `.env` |
| Traces (optional) | Langfuse Cloud through its OTLP endpoint |

## 1. Instance

- **Hosted model:** `t3.xlarge` (4 vCPU, 16 GiB), Amazon Linux 2023, 60 GiB gp3. Each sandbox is
  capped at 1 CPU and 1 GiB. The compose file starts one worker, which handles one run at a
  time; add workers (each with its own `--name`) to run more at once, about two per 4 vCPU.
- **Local model:** `g5.xlarge` and Ollama instead. The api and worker containers reach an
  Ollama on the instance at `LLM_BASE_URL="http://host.docker.internal:11434/v1"`, which needs
  Ollama to listen beyond loopback (`OLLAMA_HOST=0.0.0.0`; the security group still keeps port
  11434 private). Create the model as the README's Run it section shows.
- **Security group:** inbound 443 from your office or VPN range only; nothing else. SSH through
  SSM Session Manager, not port 22. With this group no public certificate authority can reach
  the instance, so the default TLS mode is Caddy's own CA (see [TLS](#tls) below).
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
/repopilot/REPOPILOT_OTEL_ENDPOINT          (optional, e.g. https://cloud.langfuse.com/api/public/otel)
/repopilot/REPOPILOT_LANGFUSE_PUBLIC_KEY   (optional)
/repopilot/REPOPILOT_LANGFUSE_SECRET_KEY   (optional)
```

The GitHub token is only ever used by the worker on the host side. It is not passed to
sandboxes.

## 3. Boot

[`deploy/aws/user-data.sh`](../deploy/aws/user-data.sh) does the following:

1. installs Docker, the Compose plugin, git and Node 22;
2. clones the repository to `/opt/repopilot`;
3. writes `/opt/repopilot/.env` from Parameter Store (Compose passes it to the api and worker),
   and a separate `/etc/repopilot/web.env` for the console, outside the checkout, holding only
   the API token, the API URL and the allowed host (`REPOPILOT_UI_HOSTS=$DOMAIN`; the console's
   proxy refuses requests for any other host, which stops DNS-rebinding pages);
4. builds the sandbox images;
5. starts the stack and runs migrations;
6. builds the console and installs Caddy as its TLS front.

Pass it as EC2 user data, with the four variables at the top set: `REPO_URL`, `DOMAIN`,
`LLM_BASE_URL` and `LLM_MODEL`. Optional ones follow them:

- `LLM_PRICE_IN` / `LLM_PRICE_OUT`: USD per 1M prompt and completion tokens, from your
  provider's price list. At the default 0, runs record tokens but no cost.
- `TLS_MODE`: `internal` (default) or `acme`; see [TLS](#tls).
- `TRACE_URL`: the console's "Open trace" link, a URL with `{trace_id}` in place of the run's
  trace id. Next.js inlines it when the console is built, so the script writes it to
  `web/.env.production.local`, which later builds read too.

### TLS

Caddy's default, a public certificate over ACME, needs the certificate authority to reach the
instance on port 80 (HTTP-01) or 443 (TLS-ALPN-01) from the internet. The security group above
does not allow that, so `user-data.sh` defaults to `TLS_MODE=internal`, which writes
`tls internal` into the Caddyfile: Caddy signs the certificate with its own local CA. Browsers
warn until they trust that CA's root certificate, which Caddy keeps at
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`. Copy it off the instance
(SSM Session Manager) and add it to your devices' trust stores, or distribute it with your
device management. `DOMAIN` must still resolve to the instance for your users, for example in
a private Route 53 zone.

Alternatives:

- **ALB with an ACM certificate.** ACM validates the domain through a DNS record, so nothing on
  the instance has to be reachable from the internet, and the ALB's own security group can stay
  limited to your office or VPN range. The ALB terminates TLS and forwards plain HTTP: make the
  Caddyfile `http://:80 { reverse_proxy 127.0.0.1:3000 }` and let the instance's security
  group accept port 80 only from the ALB's security group. The ALB keeps the `Host` header,
  which the console checks against `REPOPILOT_UI_HOSTS`.
- **A public certificate from Caddy.** Set `TLS_MODE=acme` and open 80 and 443 to `0.0.0.0/0`.
  The console is then reachable from the whole internet, and it has no login: add
  `basic_auth` or an OIDC proxy in the Caddyfile before you do this.
- **ACME's DNS challenge** needs no inbound access, but a Caddy build with a DNS provider
  plugin (for example Route 53), which the packaged Caddy does not include.

## 4. Updating

```
cd /opt/repopilot && git pull
docker compose --profile app up -d --build
docker compose exec api repopilot migrate
(cd web && npm ci && npm run build) && sudo systemctl restart repopilot-web
```

## 5. Backups and data

- **Snapshots:** take daily EBS snapshots of the Docker volume disk (Data Lifecycle Manager).
- **Moving to RDS:** use RDS for Postgres 16 with the `vector` extension enabled. The `app`
  profile pins `REPOPILOT_PG_HOST` and `REPOPILOT_PG_PORT` to the bundled `postgres` service,
  so change those two in `docker-compose.yml`'s `agent-env` block and put the RDS user,
  database and password in `.env` (`REPOPILOT_PG_USER`, `REPOPILOT_PG_DB`,
  `REPOPILOT_PG_PASSWORD`). LangGraph's checkpoint tables and RepoPilot's migrations both run
  there unchanged.

## 6. Stronger isolation

Install gVisor and register it as a Docker runtime:

```
# /etc/docker/daemon.json
{ "runtimes": { "runsc": { "path": "/usr/local/bin/runsc" } } }
```

Then make sandbox-svc ask for it. Its Docker client sends its own `HostConfig` struct, which
has no runtime field yet, so this is a change in two files of `services/sandbox-svc`:

1. `internal/docker/client.go`: add the field `` Runtime string `json:"Runtime,omitempty"` ``
   to `HostConfig`;
2. `internal/sandbox/manager.go`: set `Runtime: "runsc"` in the `docker.HostConfig` literal in
   `Manager.Create`.

The sandbox's syscalls are then served by gVisor's user-space kernel instead of the host
kernel. This repository does not test sandboxes under gVisor, so run the sandbox test suite
against it (`uv run pytest -m sandbox`; see the README's Tests section) before relying on it.

## 7. Costs to expect

| Item | Rough monthly cost |
|---|---|
| `t3.xlarge` on demand, plus disk | about $130 |
| Model usage | depends on the model. Every run records its tokens, and its cost in USD once `LLM_PRICE_IN`/`LLM_PRICE_OUT` are set; `repopilot eval` reports both per resolved task |
