#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
  PUID="${PUID:-99}"
  PGID="${PGID:-100}"
  groupmod --non-unique --gid "$PGID" negadownloader
  usermod --non-unique --uid "$PUID" --gid "$PGID" negadownloader
  exec gosu negadownloader /app/entrypoint.sh
fi

mkdir -p "${NEGADOWNLOADER_CONFIG_DIR:-${YOULOGGER_CONFIG_DIR:-/config}}" "${NEGADOWNLOADER_DOWNLOAD_DIR:-${YOULOGGER_DOWNLOAD_DIR:-/downloads}}"
export HOME="${NEGADOWNLOADER_CONFIG_DIR:-${YOULOGGER_CONFIG_DIR:-/config}}/home"
mkdir -p "$HOME"
chmod 700 "$HOME"
Xvfb "${DISPLAY:-:99}" -screen 0 1360x900x24 -nolisten tcp -ac &
XVFB_PID=$!
DISPLAY_NUMBER="${DISPLAY:-:99}"
DISPLAY_NUMBER="${DISPLAY_NUMBER#:}"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  [ -S "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ] && break
  sleep 0.2
done
openbox-session >/dev/null 2>&1 &
OPENBOX_PID=$!
x11vnc -display "${DISPLAY:-:99}" -localhost -forever -shared -nopw -rfbport 5900 >/dev/null 2>&1 &
VNC_PID=$!
websockify --heartbeat 30 127.0.0.1:6081 127.0.0.1:5900 >/dev/null 2>&1 &
WEBSOCKIFY_PID=$!

cleanup() {
  if [ -n "${APP_PID:-}" ]; then kill "$APP_PID" 2>/dev/null || true; fi
  kill "$WEBSOCKIFY_PID" "$VNC_PID" "$OPENBOX_PID" "$XVFB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log &
APP_PID=$!
wait "$APP_PID"
