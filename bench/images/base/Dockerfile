# Base image for every RepoPilot sandbox. Benchmark fixture images extend it with their
# dependencies baked in, because sandboxes run with no network.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest>=8.3,<9" "ruff>=0.8,<1" \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp sandbox \
    && git config --system safe.directory '*' \
    && git config --system user.name "RepoPilot" \
    && git config --system user.email "repopilot@localhost" \
    && git config --system init.defaultBranch main

COPY --chmod=755 rp-killall rp-read /usr/local/bin/

ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
