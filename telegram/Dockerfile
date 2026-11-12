# Share the reviewed Python/PostgreSQL runtime used by the other gateway services.
FROM cr.selcloud.ru/aof-registry/aof-agent-worker@sha256:e8edf36bb8251247120be6b6d9201a580de6ceae42f0a5a4ec654eceb600fbce
USER 0:0

COPY database/developer.sql database/errors.py database/postgres.py database/migrate.py database/upgrade.py database/schema.sql database/schema_v2.sql database/gateway.sql database/coding.sql /opt/aof-agent/database/
COPY operations/kube.py operations/archive.py operations/jobs.py operations/client.py operations/terraform.py operations/runner.py operations/recovery.py /opt/aof-agent/operations/
COPY developer/work_tools.py developer/session.py developer/git_tools.py developer/jira.py developer/kubernetes.py developer/effects.py developer/server.py /opt/aof-agent/developer/
COPY agent/identity.md agent/requirements.md agent/memory.md /opt/aof-agent/agent/
COPY worker/requirements.toml /etc/codex/requirements.toml
COPY coding/sandbox.py coding/config.py coding/queue.py coding/prompt.py coding/git.py coding/askpass.sh coding/publish.py coding/client.py coding/executor.py coding/controller.py coding/commands.py coding/verify.py coding/result.schema.json /opt/aof-agent/coding/
COPY repositories/github_app.py repositories/credential.py repositories/bootstrap.py /opt/aof-agent/repositories/
COPY vpn/proxy.py /opt/aof-agent/vpn/
COPY worker/background.py worker/run.py worker/chat.py worker/check_sandbox.py /opt/aof-agent/worker/
COPY telegram/history.py telegram/groups.py telegram/bot.py telegram/api.py telegram/storage.py telegram/updates.py telegram/delivery.py /opt/aof-agent/telegram/
COPY gateway/preflight.py gateway/agent_memory.py gateway/jira_intake.py gateway/subscriptions.py gateway/models.py gateway/commands.py gateway/tasks.py gateway/redaction.py gateway/kubernetes.py gateway/diagnostics.py gateway/observer.py gateway/access.py gateway/work.py gateway/buttons.py gateway/notices.py gateway/drafts.py gateway/discussion.py gateway/coordinator.py /opt/aof-agent/gateway/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aof-agent
RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["python3", "/opt/aof-agent/telegram/bot.py", "serve"]
