FROM cr.selcloud.ru/aof-registry/aof-agent-worker@sha256:0de0b04484456976541d60027ea2c204d58b00d6686b9ddd25eee006bb8a8cfb
USER 0:0
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jdk-headless maven ripgrep openssl \
    && rm -rf /var/lib/apt/lists/*
COPY coding/sandbox.py coding/config.py coding/queue.py coding/prompt.py coding/git.py coding/askpass.sh coding/publish.py coding/client.py coding/executor.py coding/controller.py coding/commands.py coding/verify.py coding/result.schema.json /opt/aof-agent/coding/
COPY repositories/github_app.py repositories/credential.py repositories/bootstrap.py /opt/aof-agent/repositories/
COPY database/errors.py database/postgres.py database/migrate.py database/upgrade.py database/schema.sql database/schema_v2.sql database/gateway.sql database/coding.sql /opt/aof-agent/database/
COPY gateway/commands.py gateway/tasks.py gateway/redaction.py gateway/kubernetes.py gateway/diagnostics.py gateway/observer.py gateway/access.py gateway/work.py gateway/buttons.py gateway/notices.py gateway/drafts.py gateway/discussion.py gateway/coordinator.py /opt/aof-agent/gateway/
COPY telegram/groups.py telegram/bot.py telegram/api.py telegram/storage.py telegram/updates.py telegram/delivery.py /opt/aof-agent/telegram/
COPY worker/run.py worker/chat.py worker/check_sandbox.py worker/requirements.toml /opt/aof-agent/worker/
COPY vpn/proxy.py /opt/aof-agent/vpn/
COPY worker/requirements.toml /etc/codex/requirements.toml
RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
