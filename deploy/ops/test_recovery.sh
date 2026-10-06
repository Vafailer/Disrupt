#!/usr/bin/env bash
# Disposable CI data only. No production configuration or provider credentials.
set -euo pipefail
umask 077
work=$(mktemp -d)
export APP_DOMAIN=http://localhost
cat > "$work/telegram-fixture.yaml" <<'YAML'
services:
  api:
    environment:
      NOTES_INTERNAL_API_TOKEN: synthetic-ci-internal-token-not-for-deployment
  telegram:
    image: postgres:17
    network_mode: none
    entrypoint: [sleep, infinity]
    volumes:
      - telegram-state:/app/data
volumes:
  telegram-state:
YAML
cleanup() {
  docker rm -f beresta-remote-bot-ci >/dev/null 2>&1 || true
  docker compose -p beresta-ops-ci -f compose.yaml -f "$work/telegram-fixture.yaml" down -v --remove-orphans >/dev/null 2>&1 || true
  docker compose -p beresta-restore-ci -f deploy/ops/restore.compose.yaml --profile tools down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
age-keygen -o "$work/identity" 2>/dev/null
recipient=$(age-keygen -y "$work/identity")
docker compose -p beresta-ops-ci -f compose.yaml -f "$work/telegram-fixture.yaml" up -d --build api worker scheduler telegram
docker compose -p beresta-ops-ci -f compose.yaml -f "$work/telegram-fixture.yaml" exec -T telegram sh -c 'printf "42" > /app/data/offset; printf "synthetic-journal" > /app/data/delivery.json'
docker compose -p beresta-ops-ci exec -T api python - <<'PY'
import hashlib
import time
import uuid
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import User, Capture
engine = create_engine('postgresql+psycopg://notes:local-demo-only@db:5432/notes')
# Original preservation test uses synthetic bytes, never a real recording.
data = b'synthetic original for recovery test'
key = str(uuid.uuid4()) + '.audio'
Path('/app/data/audio', key).write_bytes(data)
with Session(engine) as session:
    user = User(username='recovery-fixture', password_hash='unusable', is_test=True)
    session.add(user)
    session.flush()
    session.add(Capture(user_id=user.id, original_text='', idempotency_key='recovery-fixture',
                        input_kind='audio', audio_key=key, audio_sha256=hashlib.sha256(data).hexdigest(),
                        audio_bytes=len(data), audio_seconds=1, audio_media_type='audio/ogg', created_at=time.time()))
    session.commit()
PY
backup_bot_args=(--telegram)
monitor_bot_args=(--telegram)
if [ -n "${REMOTE_BOT_CONTEXT:-}" ]; then
  docker compose -p beresta-ops-ci -f compose.yaml -f "$work/telegram-fixture.yaml" exec -T telegram chown -R 1000:1000 /app/data
  docker compose -p beresta-ops-ci -f compose.yaml -f "$work/telegram-fixture.yaml" rm -sf telegram
  docker run -d --name beresta-remote-bot-ci --network none \
    --label com.docker.compose.project=beresta-bot-ci \
    --label com.docker.compose.service=telegram --label com.docker.compose.oneoff=False \
    -v beresta-ops-ci_telegram-state:/app/data --entrypoint sleep postgres:17 infinity
  backup_bot_args=(--bot-context "$REMOTE_BOT_CONTEXT" --bot-project beresta-bot-ci)
  monitor_bot_args=(--bot-context "$REMOTE_BOT_CONTEXT" --bot-project beresta-bot-ci)
fi
python deploy/ops/beresta_ops.py backup --project beresta-ops-ci --compose compose.yaml \
  --compose "$work/telegram-fixture.yaml" "${backup_bot_args[@]}" \
  --destination "$work/backups" --recipient "$recipient" --maintenance
bundle=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["file"])' "$work/backups/latest.json")
# Allow the API to become ready again after maintenance.
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null; then break; fi
  sleep 1
done
python deploy/ops/beresta_ops.py monitor --project beresta-ops-ci --compose compose.yaml \
  --compose "$work/telegram-fixture.yaml" "${monitor_bot_args[@]}" --backup-dir "$work/backups"
python deploy/ops/beresta_ops.py restore --project beresta-restore-ci \
  --bundle "$work/backups/$bundle" --identity "$work/identity" --work-dir "$work/recovery"
test "$(docker compose -p beresta-restore-ci -f deploy/ops/restore.compose.yaml run --rm --no-deps -T --entrypoint cat files /state/offset)" = 42
test "$(docker compose -p beresta-restore-ci -f deploy/ops/restore.compose.yaml run --rm --no-deps -T --entrypoint cat files /state/delivery.json)" = synthetic-journal
if [ -n "${REMOTE_BOT_CONTEXT:-}" ]; then
  test "$(docker inspect --format '{{.State.Running}}' beresta-remote-bot-ci)" = true
fi
# Recovery must refuse to overwrite even its own previous successful result.
if python deploy/ops/beresta_ops.py restore --project beresta-restore-ci \
  --bundle "$work/backups/$bundle" --identity "$work/identity" --work-dir "$work/recovery"; then
  exit 1
fi
# Exercise Caddy routing against the running mock API. No public DNS or TLS issuer.
docker run -d --name beresta-ops-caddy --network beresta-ops-ci_default \
  -p 127.0.0.1:8088:80 -e APP_DOMAIN=http://localhost \
  -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 >/dev/null
trap 'docker rm -f beresta-ops-caddy >/dev/null 2>&1 || true; cleanup' EXIT
for attempt in $(seq 1 30); do
  if curl -fsS -H 'Host: localhost' http://127.0.0.1:8088/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS -H 'Host: localhost' http://127.0.0.1:8088/health >/dev/null
for path in /internal/v1/telegram/updates /internal/v1/deliveries/claim; do
  # Prove the route exists and is protected before checking ingress blocking.
  test "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:8000$path")" = 401
  test "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' -H 'Host: localhost' "http://127.0.0.1:8088$path")" = 404
done
