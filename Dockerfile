# syntax=docker/dockerfile:1.26

ARG PYTHON_VERSION=3.13
ARG SERVICE_VERSION=0.0.0+dev

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM node:24-bookworm-slim AS player-build

ARG SERVICE_VERSION
ENV VITE_SERVICE_VERSION=${SERVICE_VERSION}

WORKDIR /build/sendspin_player

COPY sendspin_player/package.json sendspin_player/package-lock.json ./
RUN npm ci

COPY sendspin_player/ ./
RUN npm run build

FROM python:${PYTHON_VERSION}-alpine

ARG SERVICE_VERSION

LABEL org.opencontainers.image.title="Sendspin Service"
LABEL org.opencontainers.image.description="Standalone Sendspin playback service with HTTP ingest API"
LABEL org.opencontainers.image.source="https://github.com/dutchdronesquad/rh-race-voice"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV SENDSPIN_SERVICE_VERSION=${SERVICE_VERSION}
ENV SENDSPIN_INGEST_HOST=0.0.0.0
ENV SENDSPIN_INGEST_PORT=8766
ENV SENDSPIN_HOST=0.0.0.0
ENV SENDSPIN_PORT=8927
ENV SENDSPIN_ADVERTISE=false
ENV SENDSPIN_MAX_BODY_MB=50
ENV SENDSPIN_PLAYER_DIR=/opt/sendspin-service/player

WORKDIR /opt/sendspin-service

COPY pyproject.toml uv.lock ./

RUN python3 - <<'PY' > /tmp/sendspin-service-requirements.txt
import tomllib
from pathlib import Path
for dep in tomllib.loads(Path("pyproject.toml").read_text())["project"]["optional-dependencies"]["sendspin-service"]:
    print(dep)
PY

RUN apk add --no-cache libstdc++

RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --requirements /tmp/sendspin-service-requirements.txt \
    && rm /tmp/sendspin-service-requirements.txt

COPY sendspin_service ./sendspin_service
COPY --from=player-build /build/custom_plugins/race_voice/player ./player

RUN adduser -D -H -u 10001 -s /sbin/nologin sendspin

USER sendspin

EXPOSE 8766 8927

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=3).read()"

CMD ["python", "-m", "sendspin_service"]
