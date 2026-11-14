#!/bin/sh
# TubeGrab start-up. Made by Jhames Martin.

# 1) Get the newest yt-dlp every time the server starts (YouTube changes often, and
#    Docker build caches can leave an old copy behind). Includes nightly fixes.
#    Turn off with UPDATE_YTDLP_ON_START=0.
if [ "${UPDATE_YTDLP_ON_START:-1}" = "1" ]; then
  echo "[start] updating yt-dlp..."
  timeout 120 pip install --no-cache-dir -q -U --pre "yt-dlp[default]" \
    && echo "[start] yt-dlp $(yt-dlp --version)" \
    || echo "[start] yt-dlp update skipped, using the built-in version"
fi

# 2) PO token provider on 127.0.0.1:4416. Turn off with POT_PROVIDER=0.
if [ "${POT_PROVIDER:-1}" = "1" ] && [ -f /opt/bgutil/server/build/main.js ]; then
  echo "[start] starting PO token provider"
  (cd /opt/bgutil/server && node build/main.js --port 4416) &
fi

# 3) The web app. One worker so every request sees the same download jobs.
exec gunicorn app:app --bind "0.0.0.0:${PORT:-10000}" --workers 1 --threads 16 \
  --timeout 120 --access-logfile - --error-logfile -
