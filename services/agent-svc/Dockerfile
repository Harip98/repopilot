# agent-svc: the RepoPilot API (`repopilot serve`) and worker (`repopilot worker`).
# Build from the repository root so migrations and benchmark tasks are included:
#   docker build -f services/agent-svc/Dockerfile -t repopilot-agent-svc .
# The base is pinned by digest (Dependabot proposes updates); uv is pinned exactly.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.13.4" \
    && useradd --uid 10010 --create-home repopilot

ENV REPOPILOT_ROOT=/app UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/services/agent-svc

COPY services/agent-svc/pyproject.toml services/agent-svc/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY services/agent-svc/src ./src
RUN uv sync --locked --no-dev

COPY db /app/db
COPY bench /app/bench
COPY docker-compose.yml /app/docker-compose.yml

USER repopilot
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["repopilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
