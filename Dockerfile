# syntax=docker/dockerfile:1
# SABİT TABAN İMAJ (operatör kararı, 19 Kasım 2026). `python:3.12-slim` etiketi
# zamanla başka bir imaja kayar; `@sha256:…` özeti kaymaz. Çekilen şey özettir,
# etiket yalnız okunsun diye duruyor. Bu özet = Python 3.12.16 · Debian trixie ·
# OpenSSL 3.5.7 (5 Kasım 2026 yapısı); canlıdaki 03aca2c derlenirken etiketin
# gösterdiği imaj da buydu. Python ve OpenSSL upstream'e giden TLS el sıkışmasını
# belirler: özeti değiştirmek gönderme tarafını değiştirmektir. Nasıl ve ne zaman
# güncellenir: docs/LESSONS_2026_11_14.md §20.6.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

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
      libatomic1 \
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
#
# SABİT PAKET SÜRÜMLERİ: pyproject.toml aralık verir, requirements.lock tam sürümü.
# `-c` olmadan her derleme aralıktaki en yeni sürümleri kurar ve gönderme tarafı
# kod değişmeden değişebilir.
COPY pyproject.toml README.md requirements.lock ./
RUN mkdir -p app && touch app/__init__.py \
 && pip install --no-cache-dir -c requirements.lock ".[tokens,browser]" \
 && (playwright install --only-shell chromium || playwright install chromium) \
 && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

COPY app ./app
COPY scripts/verify_lock.py ./scripts/verify_lock.py
# Son denetim: imajın içi kilitle ve yukarıdaki taban imajla birebir aynı değilse
# derleme BAŞARISIZ olur (Render yeni sürümü yayına almaz, çalışan sürüm sürer).
# Taban imaj özeti ya da kilit bilerek değiştirildiğinde buradaki iki sürüm de
# güncellenir.
RUN pip install --no-cache-dir --no-deps . \
 && python scripts/verify_lock.py requirements.lock \
      --expect-python 3.12.16 --expect-openssl 3.5.7

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
