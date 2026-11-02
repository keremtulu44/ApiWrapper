# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    UPSTREAM_STATE_FILE=/app/.cache/upstream_state.json

WORKDIR /app

# Headless Chromium için yalnızca gerekli paylaşımlı kütüphaneleri kur:
# `playwright install --with-deps` gereksiz yere `xvfb`, `x11-common` ve ~500 MB
# font paketi indirip Render build ortamında debconf/invoke-rc.d ve bellek
# sorunlarına yol açıyordu.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      fonts-liberation \
      libasound2t64 \
      libatk-bridge2.0-0t64 \
      libatk1.0-0t64 \
      libatspi2.0-0t64 \
      libcairo2 \
      libcups2t64 \
      libdbus-1-3 \
      libdrm2 \
      libfontconfig1 \
      libfreetype6 \
      libgbm1 \
      libglib2.0-0t64 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      libxshmfence1 \
 && rm -rf /var/lib/apt/lists/*

# Bağımlılıkları ve Playwright headless kabuğunu uygulama kodundan ÖNCE kur ki
# `app/` altındaki kod değişikliklerinde Docker katman önbelleği (cache) bozulmasın.
COPY pyproject.toml README.md ./
RUN mkdir -p app && touch app/__init__.py \
 && pip install --no-cache-dir ".[tokens,browser]" \
 && (playwright install --only-shell chromium || playwright install chromium) \
 && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

COPY app ./app
RUN pip install --no-cache-dir --no-deps .

COPY config ./config

RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/.cache \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

# Render'ın verdiği PORT'u, yerel/docker-compose kullanımında ise 8000'i dinle.
# exec, uvicorn'un container sinyallerini doğrudan almasını sağlar.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --workers 1"]
