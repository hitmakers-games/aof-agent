FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CODEX_VERSION=0.167.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-psycopg git bubblewrap ca-certificates curl openssl ripgrep openjdk-17-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --ignore-scripts @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace /state /policy /etc/codex /run/secrets \
    && printf 'public sandbox test fixture\n' > /run/secrets/aof-sandbox-canary \
    && chown agent:agent /workspace /state

COPY worker/install-tools.sh /opt/aof-agent/worker/install-tools.sh
RUN sh /opt/aof-agent/worker/install-tools.sh

COPY agent/ /opt/aof-agent/agent/
COPY coding/ /opt/aof-agent/coding/
COPY database/ /opt/aof-agent/database/
COPY developer/ /opt/aof-agent/developer/
COPY gateway/ /opt/aof-agent/gateway/
COPY operations/ /opt/aof-agent/operations/
COPY repositories/ /opt/aof-agent/repositories/
COPY telegram/ /opt/aof-agent/telegram/
COPY vpn/ /opt/aof-agent/vpn/
COPY worker/ /opt/aof-agent/worker/
COPY worker/requirements.toml /etc/codex/requirements.toml

ENV HOME=/home/agent CODEX_HOME=/state/auth PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aof-agent
RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/aof-agent/worker/run.py"]
