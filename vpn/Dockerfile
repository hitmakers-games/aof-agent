FROM golang:1.25-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS tunnel
ENV GOMAXPROCS=2 GOFLAGS=-p=2
RUN apk add --no-cache git
WORKDIR /src
# Official upstream revisions, pinned independently of moving tags.
RUN git init && git remote add origin https://github.com/amnezia-vpn/amneziawg-go.git \
    && git fetch --depth=1 origin b5928efb6ca19f0153958460c3d141f04abc5c2e \
    && git checkout --detach FETCH_HEAD \
    && go mod download && go mod verify \
    && CGO_ENABLED=0 go build -trimpath -o /amneziawg-go .

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS tools
RUN apk add --no-cache git build-base linux-headers
WORKDIR /src
RUN git init && git remote add origin https://github.com/amnezia-vpn/amneziawg-tools.git \
    && git fetch --depth=1 origin ee0f0a9aa34ff0a0da4b3433b9512781cfe02843 \
    && git checkout --detach FETCH_HEAD && make -C src -j2

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
RUN apk add --no-cache python3 py3-cryptography iproute2 iptables curl ca-certificates \
    && mkdir -p /run/amneziawg /run/vpn /status
COPY --from=tunnel /amneziawg-go /usr/local/bin/amneziawg-go
COPY --from=tools /src/src/wg /usr/local/bin/awg
COPY --from=tunnel /src/LICENSE /licenses/amneziawg-go-LICENSE
COPY --from=tools /src/COPYING /licenses/amneziawg-tools-COPYING
COPY vpn/vpn.py vpn/proxy.py vpn/subscription.py vpn/connection_key.py vpn/failover.py vpn/gateway-public.pem /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/vpn.py"]
CMD ["run"]
