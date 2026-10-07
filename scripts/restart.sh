#!/usr/bin/env bash
# Stop this app's API and Postgres, then start both again.
# The database volume is kept. Run from anywhere: ./scripts/restart.sh
#
# Ports are private to this repo. 8000 and 5432 belong to other local apps.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

API_HOST="127.0.0.1"
API_PORT="8010"
POSTGRES_PORT="5433"
COMPOSE=(docker compose -p jobs-agent)

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

stop_api() {
  local pid cwd parent parent_cmd
  local -a targets=()
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {print substr($0, 2); exit}')"
    if [[ "$cwd" != "$ROOT" && "$(ps -p "$pid" -o command= 2>/dev/null || true)" != *"$ROOT"* ]]; then
      continue
    fi
    targets+=("$pid")
  done < <(pgrep -f "uvicorn app.main:app --reload --host ${API_HOST} --port ${API_PORT}" || true)

  if [[ ${#targets[@]} -eq 0 ]]; then
    echo "API is not running."
    return
  fi

  for pid in "${targets[@]}"; do
    parent="$(ps -o ppid= -p "$pid" | tr -d ' ')"
    parent_cmd="$(ps -p "$parent" -o command= 2>/dev/null || true)"
    if [[ "$parent_cmd" == *"uvicorn app.main:app --reload --host ${API_HOST} --port ${API_PORT}"* ]]; then
      echo "Stopping API process ${parent}"
      kill "$parent" 2>/dev/null || true
    else
      echo "Stopping API process ${pid}"
      kill "$pid" 2>/dev/null || true
    fi
  done

  local _try
  for _try in 1 2 3 4 5 6 7 8 9 10; do
    if ! pgrep -f "uvicorn app.main:app --reload --host ${API_HOST} --port ${API_PORT}" >/dev/null 2>&1; then
      return
    fi
    # A match may be another checkout. Confirm none of ours remain.
    local still=0
    while read -r pid; do
      [[ -z "$pid" ]] && continue
      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {print substr($0, 2); exit}')"
      if [[ "$cwd" == "$ROOT" || "$(ps -p "$pid" -o command= 2>/dev/null || true)" == *"$ROOT"* ]]; then
        still=1
      fi
    done < <(pgrep -f "uvicorn app.main:app --reload --host ${API_HOST} --port ${API_PORT}" || true)
    if [[ "$still" -eq 0 ]]; then
      return
    fi
    sleep 0.3
  done

  for pid in "${targets[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
}

stop_api

if port_in_use "$API_PORT"; then
  echo "Port ${API_PORT} is still in use by another process. Not starting this API." >&2
  exit 1
fi

echo "Restarting this app's Postgres on port ${POSTGRES_PORT}..."
"${COMPOSE[@]}" down

if port_in_use "$POSTGRES_PORT"; then
  echo "Port ${POSTGRES_PORT} is in use by another process. Not starting Postgres." >&2
  exit 1
fi

"${COMPOSE[@]}" up -d

ready=0
for _try in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U jobs -d jobs >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "Postgres did not become ready." >&2
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi

echo "Applying database migrations..."
"$PY" -m alembic upgrade head

echo "Starting API at http://${API_HOST}:${API_PORT}/"
exec "$PY" -m uvicorn app.main:app --reload --host "$API_HOST" --port "$API_PORT"
