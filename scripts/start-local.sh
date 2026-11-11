#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

python=.venv/bin/python
if ! "$python" -c 'import django' >/dev/null 2>&1; then
  echo 'Prepare .venv and install requirements-web.txt. See README.md.'
  exit 1
fi
docker info --format '{{.ServerVersion}}' >/dev/null || { echo 'Start Docker Desktop and try again.'; exit 1; }

local_dir="$PWD/.local"
mkdir -p "$local_dir"
chmod 700 "$local_dir"
export DJANGO_SETTINGS_MODULE=hosted.settings
export OPR_SAMPLE_DB="$local_dir/sample.sqlite3"
export OPR_SAMPLE_REPOSITORY="${OPR_SAMPLE_REPOSITORY:-$PWD/../museum-of-off-by-ones}"
export OPR_OLLAMA_PORT="${OPR_OLLAMA_PORT:-11435}"
web_port="${OPR_WEB_PORT:-8765}"
export OLLAMA_HOST="127.0.0.1:$OPR_OLLAMA_PORT"
model_pid='' worker_pid='' web_pid=''

cleanup() {
  for pid in "$web_pid" "$worker_pid" "$model_pid"; do
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ollama_ready() {
  curl --noproxy '*' -fsS --max-time 2 "http://$OLLAMA_HOST/api/tags" >/dev/null
}
if ! ollama_ready 2>/dev/null; then
  ollama serve >"$local_dir/ollama.log" 2>&1 &
  model_pid=$!
  for _ in {1..15}; do
    if ollama_ready 2>/dev/null; then break; fi
    sleep 1
  done
fi
if ! ollama show llama3.2:latest >/dev/null 2>&1; then
  echo "Install the model with OLLAMA_HOST=$OLLAMA_HOST ollama pull llama3.2:latest, then retry."
  exit 1
fi

if [[ ! -s "$local_dir/secret-key" ]]; then
  "$python" -c 'import secrets; print(secrets.token_urlsafe(48))' >"$local_dir/secret-key"
fi
export OPR_LOCAL_SECRET_KEY="$(cat "$local_dir/secret-key")"
"$python" -m django migrate --noinput --verbosity 0
"$python" - <<'PY'
import secrets
from pathlib import Path

import django
django.setup()
from django.contrib.auth import get_user_model

users = get_user_model().objects
if not users.filter(username='local-operator').exists():
    password = secrets.token_urlsafe(24)
    Path('.local/operator-password').write_text(password + '\n')
    users.create_user('local-operator', password=password, is_staff=True)
PY

"$python" -m django sample_worker >"$local_dir/worker.log" 2>&1 &
worker_pid=$!
"$python" -m django runserver "127.0.0.1:$web_port" --noreload >"$local_dir/web.log" 2>&1 &
web_pid=$!
echo "Open http://127.0.0.1:$web_port and sign in as local-operator."
echo "Initial password file: $local_dir/operator-password"
echo 'Press Ctrl-C to stop the preview.'
while kill -0 "$worker_pid" 2>/dev/null && kill -0 "$web_pid" 2>/dev/null; do
  sleep 1
done
echo "A preview process exited. See $local_dir/worker.log and $local_dir/web.log."
exit 1
