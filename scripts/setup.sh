#!/usr/bin/env bash
# Bootstraps a fresh clone: env file -> (optional local venv) -> db + api -> migrations.
# Usage: ./scripts/setup.sh [--local]
#   --local  also creates backend/.venv with dev deps (for IDE / running linters on the host)
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 1; }; }
need docker
need curl

docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required" >&2; exit 1; }

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c "import secrets; print(secrets.token_hex(32))"
  fi
}

# 1. .env (git-ignored). Placeholders are replaced with generated secrets.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

fill_placeholder() {
  local key="$1" val
  if grep -q "^${key}=change-me" .env; then
    val="$(gen_secret)"
    sed "s|^${key}=change-me.*|${key}=${val}|" .env > .env.tmp && mv .env.tmp .env
    echo "Generated ${key}"
  fi
}

fill_placeholder DJANGO_SECRET_KEY
fill_placeholder POSTGRES_PASSWORD

# 2. Optional host virtualenv
if [ "${1:-}" = "--local" ]; then
  python3 -m venv backend/.venv
  # shellcheck disable=SC1091
  . backend/.venv/bin/activate
  pip install --upgrade pip
  pip install -r backend/requirements/dev.txt
  echo "Local venv ready: source backend/.venv/bin/activate"
fi

# 3. Build and start db + backend (the backend container runs migrate on start)
docker compose up -d --build

# 4. Wait for the API
echo "Waiting for API on http://localhost:8000 ..."
ready=0
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/healthz/ >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done

if [ "$ready" -ne 1 ]; then
  echo "API did not become healthy. Check: docker compose logs backend" >&2
  exit 1
fi

echo
echo "Ready."
echo "  API:       http://localhost:8000/healthz/"
echo "  Admin:     http://localhost:8000/admin/"
echo "  Superuser: docker compose exec backend python manage.py createsuperuser"
echo "  Tests:     docker compose exec backend python manage.py test"