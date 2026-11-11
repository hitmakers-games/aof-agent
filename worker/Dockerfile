FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CODEX_VERSION=0.167.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-psycopg git bubblewrap ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --ignore-scripts @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace /state /policy /etc/codex /run/secrets \
    && printf 'public sandbox test fixture\n' > /run/secrets/aof-sandbox-canary \
    && chown agent:agent /workspace /state

COPY database/errors.py database/postgres.py database/migrate.py database/upgrade.py database/schema.sql database/schema_v2.sql database/gateway.sql /opt/aof-agent/database/
COPY worker/requirements.toml /etc/codex/requirements.toml
COPY worker/run.py worker/chat.py worker/check_sandbox.py /opt/aof-agent/worker/
COPY telegram/bot.py telegram/api.py telegram/storage.py telegram/updates.py telegram/delivery.py /opt/aof-agent/telegram/
COPY gateway/commands.py gateway/tasks.py gateway/redaction.py gateway/kubernetes.py gateway/diagnostics.py gateway/observer.py gateway/access.py gateway/work.py gateway/buttons.py gateway/notices.py gateway/drafts.py gateway/discussion.py gateway/coordinator.py /opt/aof-agent/gateway/

ENV HOME=/home/agent CODEX_HOME=/state/auth PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aof-agent
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/aof-agent/worker/run.py"]
