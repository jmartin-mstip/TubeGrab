# TubeGrab. Made by Jhames Martin.
# Server and plugin versions of the PO token provider must match.
ARG BGUTIL_VERSION=2.0.2

# ---------- stage 1: build the PO token provider (helps get past YouTube's bot checks) ----------
FROM node:22-bookworm-slim AS pot
ARG BGUTIL_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN git clone --depth 1 --single-branch --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil \
    && cd bgutil/server && npm ci && npx tsc && npm prune --omit=dev

# ---------- stage 2: the app ----------
FROM python:3.12-slim-bookworm
ARG BGUTIL_VERSION

# ffmpeg joins video + audio and converts to MP3.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
# Node + the built provider server.
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /opt/bgutil/server /opt/bgutil/server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U "yt-dlp[default]" "bgutil-ytdlp-pot-provider==${BGUTIL_VERSION}" \
    && deno --version && node --version && yt-dlp --version

COPY . .
# Files edited on phones/Windows can have CRLF line endings, which break shell scripts.
RUN sed -i 's/\r$//' /app/start.sh

ENV PORT=10000 PYTHONUNBUFFERED=1
EXPOSE 10000

CMD ["sh", "/app/start.sh"]
