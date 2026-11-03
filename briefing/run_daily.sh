#!/usr/bin/env bash
# Start the agent, write today's briefing to ./briefings/, stop the agent.
# Safe to run from cron. Usage: ./run_daily.sh [client args, e.g. --sections opensource]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-9998}"
# Python: $PYTHON, else the repo-root venv, else a local venv, else python3
if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"
elif [[ -x ../.venv/bin/python ]]; then PY=../.venv/bin/python
elif [[ -x .venv/bin/python ]]; then PY=.venv/bin/python
else PY=python3; fi
mkdir -p briefings logs
"$PY" news_agent.py --port "$PORT" >> logs/server.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/.well-known/agent-card.json" >/dev/null && break
  sleep 0.5
done
"$PY" briefing_client.py "$@" --url "http://127.0.0.1:$PORT" --save --out-dir briefings
