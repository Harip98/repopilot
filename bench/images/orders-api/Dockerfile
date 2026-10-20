# Sandbox image for the orders-api fixture: dependencies are baked in because sandboxes have
# no network. The repo itself is uploaded per run.
FROM repopilot-sandbox-base

USER root
RUN pip install --no-cache-dir "fastapi>=0.115,<0.116" "httpx>=0.27,<0.28" "pydantic>=2.9,<3"
USER 10001:10001
