#!/bin/sh
# TubeGrab start-up. Made by Jhames Martin.
PORT="${PORT:-10000}"
PIDFILE=/tmp/gunicorn.pid

ytdlp_version() { python -c "import yt_dlp; print(yt_dlp.version.__version__)" 2>/dev/null; }

# Background: get the newest yt-dlp (nightly fixes included) without making anyone wait.
# If a newer version was installed, reload the app once nobody is downloading.
# Turn off with UPDATE_YTDLP_ON_START=0.
update_in_background() {
  before="$(ytdlp_version)"
  timeout 180 pip install --no-cache-dir -q -U --pre "yt-dlp[default]" || { echo "[update] skipped"; return; }
  after="$(ytdlp_version)"
  if [ "$before" = "$after" ]; then echo "[update] yt-dlp $after is already the newest"; return; fi
  echo "[update] yt-dlp $before -> $after, reloading when idle"
  i=0
  while [ $i -lt 120 ]; do
    busy="$(python - "$PORT" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/status", timeout=5) as r:
        print(json.load(r).get("active_downloads", 0))
except Exception:
    print(0)
PY
)"
    [ "$busy" = "0" ] && break
    sleep 5; i=$((i + 1))
  done
  [ -f "$PIDFILE" ] && kill -HUP "$(cat "$PIDFILE")" && echo "[update] reloaded with yt-dlp $after"
}

if [ "${UPDATE_YTDLP_ON_START:-1}" = "1" ]; then
  (sleep 2; update_in_background) &
fi

# PO token provider on 127.0.0.1:4416. Turn off with POT_PROVIDER=0.
if [ "${POT_PROVIDER:-1}" = "1" ] && [ -f /opt/bgutil/server/build/main.js ]; then
  echo "[start] starting PO token provider"
  (cd /opt/bgutil/server && node build/main.js --port 4416) &
fi

# The web app starts right away. One worker so every request sees the same download jobs.
exec gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 1 --threads 16 \
  --timeout 120 --graceful-timeout 60 --pid "$PIDFILE" --access-logfile - --error-logfile -
