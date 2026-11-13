# Share the reviewed Python/PostgreSQL runtime used by the other gateway services.
FROM cr.selcloud.ru/aof-registry/aof-agent-worker@sha256:e8edf36bb8251247120be6b6d9201a580de6ceae42f0a5a4ec654eceb600fbce
USER 0:0

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

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aof-agent
RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["python3", "/opt/aof-agent/telegram/bot.py", "serve"]
