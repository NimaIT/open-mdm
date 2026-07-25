#!/usr/bin/env bash
# Dev helper: (re)start the API server in the background.
#   ./scripts/devserver.sh restart|stop|status
set -uo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8099}"
PIDFILE="/tmp/mdm-uvicorn.pid"
LOG="/tmp/uvicorn.log"

stop() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    sleep 2
  fi
}

case "${1:-restart}" in
  stop) stop; echo "stopped" ;;
  status)
    curl -sf "localhost:$PORT/health" && echo || echo "not running" ;;
  restart|start)
    stop
    setsid nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
      > "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 25); do
      sleep 1
      if curl -sf "localhost:$PORT/health" > /dev/null; then
        echo "listening on http://127.0.0.1:$PORT"
        exit 0
      fi
    done
    echo "failed to start; log:"; tail -25 "$LOG"; exit 1
    ;;
esac
