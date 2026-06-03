#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! python3 -c "import fastapi" 2>/dev/null; then
  pip install -r requirements.txt
fi

uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
