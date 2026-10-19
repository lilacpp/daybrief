#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/pip install -e './backend[dev]'
[ -f backend/.env ] || cp .env.example backend/.env
[ -f frontend/.env.local ] || printf '%s\n' 'NEXT_PUBLIC_API_BASE_URL=http://localhost:8000' > frontend/.env.local
(cd frontend && npm ci)
(cd backend && ../.venv/bin/uvicorn daybrief.api:app --host 127.0.0.1 --port 8000) &
backend_pid=$!
trap 'kill "$backend_pid" 2>/dev/null || true' EXIT INT TERM
cd frontend
npm run dev
