FROM cr.selcloud.ru/aof-registry/aof-agent-worker@sha256:0de0b04484456976541d60027ea2c204d58b00d6686b9ddd25eee006bb8a8cfb
USER 0:0
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jdk-headless maven ripgrep openssl \
    && rm -rf /var/lib/apt/lists/*
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
