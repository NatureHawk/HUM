# Production image for the SyncBoard API.
#
# Two stages so the built wheels and their build tooling do not ship: the
# runtime layer gets the installed packages and the application, nothing else.
#
# There are no secrets here by design. DATABASE_URL, REDIS_URL, SESSION_SECRET
# and FRONTEND_URL are supplied by the platform at runtime, never baked in.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependencies resolve from pyproject alone, so this layer is cached until the
# dependency list itself changes - not on every source edit.
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production

# Run as a non-root user. Nothing in this service writes to disk.
RUN useradd --create-home --uid 10001 syncboard

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R syncboard:syncboard /app

USER syncboard

# Documentation only - the platform injects the real PORT and the entrypoint
# binds to it. Hardcoding a published port is exactly the kind of local
# assumption this milestone exists to remove.
EXPOSE 8000

# Liveness only: it touches no dependency, so a database blip cannot make the
# orchestrator restart healthy containers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/api/health/live\", timeout=4)"

ENTRYPOINT ["./entrypoint.sh"]
