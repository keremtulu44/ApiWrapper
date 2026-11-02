# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    UPSTREAM_STATE_FILE=/app/.cache/upstream_state.json

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
# browser sağlayıcı isteğe bağlı olsa da imaj Render'da seçildiğinde ek kurulum
# gerektirmeden çalışsın. Browser binary'leri root'a özel /root altına değil,
# appuser'ın da okuyabileceği PLAYWRIGHT_BROWSERS_PATH altına kurulur.
RUN pip install --no-cache-dir ".[tokens,browser]" \
 && playwright install --with-deps chromium \
 && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" \
 && rm -rf /var/lib/apt/lists/*

COPY config ./config

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

# Render'ın verdiği PORT'u, yerel/docker-compose kullanımında ise 8000'i dinle.
# exec, uvicorn'un container sinyallerini doğrudan almasını sağlar.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --workers 1"]
