#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then cp .env.example .env; fi
if ! grep -q '^APP_SECRET_KEY=.' .env; then
  secret="$(openssl rand -base64 32 2>/dev/null | tr -d '\n' || python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  if grep -q '^APP_SECRET_KEY=' .env; then
    sed -i.bak "s#^APP_SECRET_KEY=.*#APP_SECRET_KEY=$secret#" .env
  else
    printf '\nAPP_SECRET_KEY=%s\n' "$secret" >> .env
  fi
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Starting AI Novel Generator with Docker Compose..."
  docker compose up --build
  exit $?
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Docker is unavailable. Local mode requires Python 3.11+ and Node.js 22+."
  read -r -p "Press Enter to close..."
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "Docker is unavailable and uv is not installed. Install uv from https://docs.astral.sh/uv/"
  read -r -p "Press Enter to close..."
  exit 1
fi

uv sync --dev
(cd web && test -d node_modules || npm ci)
uv run uvicorn main:app --host 127.0.0.1 --port 8000 & backend_pid=$!
uv run python -m app.worker & worker_pid=$!
(cd web && npm run dev) & frontend_pid=$!
trap 'kill "$backend_pid" "$worker_pid" "$frontend_pid" 2>/dev/null || true' EXIT INT TERM
sleep 4
open "http://localhost:3000" 2>/dev/null || true
wait "$frontend_pid"
