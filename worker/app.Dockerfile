# Code-only builds reuse the reviewed runtime. Rebuild worker/Dockerfile when
# Node, Python, Codex, OS packages, or the base image need an update.
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

RUN chmod 755 /opt/aof-agent/coding/askpass.sh
USER 10001:10001
