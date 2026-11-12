# Share the reviewed Python/PostgreSQL runtime used by the other gateway services.
FROM cr.selcloud.ru/aof-registry/aof-agent-worker@sha256:996bbeb4aca920deb6a546a2b4514cafdece074fdc4bb7abab544835bea6b7b2
USER 0:0

COPY database/developer.sql database/errors.py database/postgres.py database/migrate.py database/upgrade.py database/schema.sql database/schema_v2.sql database/gateway.sql database/coding.sql /opt/aof-agent/database/
COPY developer/session.py developer/git_tools.py developer/jira.py developer/kubernetes.py developer/effects.py developer/server.py /opt/aof-agent/developer/
COPY worker/requirements.toml /etc/codex/requirements.toml
COPY coding/sandbox.py coding/config.py coding/queue.py coding/prompt.py coding/git.py coding/askpass.sh coding/publish.py coding/client.py coding/executor.py coding/controller.py coding/commands.py coding/verify.py coding/result.schema.json /opt/aof-agent/coding/
COPY repositories/github_app.py repositories/credential.py repositories/bootstrap.py /opt/aof-agent/repositories/
COPY vpn/proxy.py /opt/aof-agent/vpn/
COPY worker/run.py worker/chat.py worker/check_sandbox.py /opt/aof-agent/worker/
COPY telegram/history.py telegram/groups.py telegram/bot.py telegram/api.py telegram/storage.py telegram/updates.py telegram/delivery.py /opt/aof-agent/telegram/
COPY gateway/commands.py gateway/tasks.py gateway/redaction.py gateway/kubernetes.py gateway/diagnostics.py gateway/observer.py gateway/access.py gateway/work.py gateway/buttons.py gateway/notices.py gateway/drafts.py gateway/discussion.py gateway/coordinator.py /opt/aof-agent/gateway/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aof-agent
RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["python3", "/opt/aof-agent/telegram/bot.py", "serve"]
