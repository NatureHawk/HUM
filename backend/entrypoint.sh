#!/bin/sh
# Production start for the SyncBoard API.
#
# Runs migrations, then hands over to uvicorn with exec so the ASGI server is
# PID 1 and receives SIGTERM directly - without that, a platform's graceful
# shutdown is a kill, and every open WebSocket is severed rather than closed.
#
# This is the normal Linux ASGI path. It does NOT use run.py, which exists only
# to work around a Windows event-loop limitation during local development.
set -e

: "${PORT:=8000}"
: "${WEB_CONCURRENCY:=1}"

echo "==> applying database migrations"
alembic upgrade head

echo "==> starting uvicorn on 0.0.0.0:${PORT} (${WEB_CONCURRENCY} worker(s))"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --workers "${WEB_CONCURRENCY}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --timeout-graceful-shutdown 20 \
  --no-server-header
