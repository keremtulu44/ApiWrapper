"""FastAPI uygulama fabrikası ve yaşam döngüsü yönetimi."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.v1 import admin, chat, health, models
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    AccessLogMiddleware,
    BodyLimitMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from app.services.account import build_account_pool
from app.services.alerting import build_alerter
from app.services.completion_service import CompletionService
from app.services.keepalive import run_upstream_keepalive
from app.services.model_registry import ModelEntry, ModelRegistry
from app.services.recaptcha import create_recaptcha_provider
from app.services.session_manager import SessionManager
from app.services.token_refresh import run_token_refresher
from app.telegram.bot import TelegramSettings, run_from_settings
from app.upstream.client import UpstreamClient
from app.upstream.curl_runtime import apply_upstream_curl
from app.upstream.state_store import restore_runtime_state

logger = get_logger(__name__)

DESCRIPTION = """
OpenAI uyumlu bir REST cephesi üzerinden harici bir Web LLM servisine erişim.

* `POST /v1/chat/completions` — streaming (SSE) ve non-streaming
* `POST /v1/completions` — eski metin tamamlama biçimi
* `GET  /v1/models` — yapılandırılmış modeller
* `GET  /health`, `GET /metrics` — işletim uç noktaları
"""


def _load_registry(settings: Settings) -> ModelRegistry:
    """Model dosyasını yükler; yoksa güvenli bir varsayılana düşer."""
    path = Path(settings.models_file)
    try:
        return ModelRegistry.from_file(path)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("models_file_fallback", path=str(path), error=str(exc))
        return ModelRegistry(
            [
                ModelEntry(
                    id="default-model",
                    upstream_id="default-model",
                    description="Placeholder entry; configure config/models.yaml.",
                )
            ]
        )


def _embedded_telegram_settings(settings: Settings) -> TelegramSettings | None:
    """Token varsa botu Web Service içinde çalıştır; ayrı worker gerektirmez.

    Aynı serviste WRAPPER_BASE_URL ve WRAPPER_API_KEY tekrar istenmez. Bot,
    açıkça verilmediklerinde kendi uvicorn portunu ve API_KEYS ilk değerini
    kullanır. Ayrı Dockerfile.telegram worker'ında bu fallback'ler devre dışıdır.
    """
    try:
        telegram = TelegramSettings()
    except ValueError:
        logger.error("telegram_configuration_invalid")
        return None
    if not telegram.telegram_bot_token.strip():
        return None

    updates: dict[str, str] = {}
    if "wrapper_base_url" not in telegram.model_fields_set:
        updates["wrapper_base_url"] = f"http://127.0.0.1:{settings.port}"
    if not telegram.wrapper_api_key.strip():
        api_key = next((key.strip() for key in settings.api_keys if key.strip()), "")
        if api_key:
            updates["wrapper_api_key"] = api_key
    if updates:
        telegram = telegram.model_copy(update=updates)

    if missing := telegram.missing_secrets():
        logger.error("telegram_configuration_error", missing=missing)
        return None
    return telegram


async def _run_embedded_telegram(settings: TelegramSettings) -> None:
    while True:
        try:
            await run_from_settings(settings)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # httpx hata metni Telegram token'lı URL taşıyabilir; yalnız türü logla
            # ve kısa bir beklemeden sonra botu yeniden başlat (Render'da kalıcı
            # olarak susmasını önler).
            logger.error(
                "telegram_bot_restarting_after_error",
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Kaynakları başlatır ve düzgünce kapatır."""
    settings: Settings = app.state.settings

    domain = settings.target_domain.strip()
    if not domain or domain in ("localhost", "127.0.0.1"):
        logger.warning(
            "target_domain_not_configured",
            target_domain=domain or "(empty)",
            hint=(
                "TARGET_DOMAIN is not set to a real host. Every upstream request will "
                "fail. Set it in .env WITHOUT the scheme, e.g. TARGET_DOMAIN=example.com"
            ),
        )

    upstream = UpstreamClient(settings)
    await upstream.startup()

    recaptcha = create_recaptcha_provider(settings)
    if recaptcha.name != "browser":
        try:
            await recaptcha.startup()
        except Exception as exc:
            # Captcha sağlayıcısı başlatılamazsa servis yine ayakta kalsın;
            # hata ilk istekte anlamlı biçimde raporlanır.
            logger.error(
                "recaptcha_startup_failed", provider=recaptcha.name, error=str(exc)
            )

    sessions = SessionManager(settings, warmup=upstream.warmup)
    registry = _load_registry(settings)
    if model_id := app.state.upstream_curl_model_id:
        entry = registry.resolve(None)
        entry.upstream_id = model_id
        logger.info("upstream_curl_model_loaded", model=entry.id)
    accounts = build_account_pool(settings)

    app.state.upstream = upstream
    app.state.recaptcha = recaptcha
    app.state.sessions = sessions
    app.state.registry = registry
    app.state.accounts = accounts
    app.state.completion_service = CompletionService(
        settings=settings,
        upstream=upstream,
        registry=registry,
        sessions=sessions,
        recaptcha=recaptcha,
        accounts=accounts,
    )

    telegram_task: asyncio.Task[None] | None = None
    if telegram_settings := _embedded_telegram_settings(settings):
        telegram_task = asyncio.create_task(
            _run_embedded_telegram(telegram_settings), name="telegram-bot"
        )

    alerter = build_alerter(settings)

    refresh_task: asyncio.Task[None] | None = None
    if settings.upstream_refresh_enabled:
        # Erişim token'ı ~1 saatte ölüyor: süresi yaklaşınca kendiliğinden yenile
        # ve yeni token/çerezi AccountPool + UpstreamClient jar'larına aktar.
        refresh_task = asyncio.create_task(
            run_token_refresher(
                settings,
                accounts=accounts,
                upstream=upstream,
                alerter=alerter,
            ),
            name="upstream-token-refresh",
        )

    keepalive_task: asyncio.Task[None] | None = None
    if settings.upstream_keepalive_interval_seconds > 0:
        # Aynı görev hem canlılık denemesi hem teşhis/bildirim kaynağıdır.
        keepalive_task = asyncio.create_task(
            run_upstream_keepalive(settings, alerter=alerter),
            name="upstream-keepalive",
        )

    logger.info(
        "application_started",
        version=settings.app_version,
        models=len(registry),
        recaptcha_provider=recaptcha.name,
        upstream=settings.base_url,
        accounts=accounts.size,
    )
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        if keepalive_task is not None:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive_task
        if telegram_task is not None:
            telegram_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telegram_task
        with contextlib.suppress(Exception):
            await recaptcha.shutdown()
        with contextlib.suppress(Exception):
            await upstream.shutdown()
        await sessions.clear()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Uygulamayı oluşturur (testler kendi Settings'ini geçebilir)."""
    from_env = settings is None
    settings = settings or get_settings()
    upstream_curl_model_id: str | None = None
    if from_env:
        try:
            settings, upstream_curl_model_id = apply_upstream_curl(settings)
        except ValueError as exc:
            # Kullanıcı Render'da UPSTREAM_CURL içine yanlışlıkla yalnız çerez
            # veya eksik bir komut yapıştırdıysa uvicorn açılışta çökmesin;
            # çerez olarak uygulanabiliyorsa uygula, değilse uyarı logla.
            raw_curl = (settings.upstream_curl or "").strip()
            from app.upstream.session_capture import apply_cookie_to_settings

            with contextlib.suppress(ValueError):
                apply_cookie_to_settings(settings, raw_curl)
            settings.upstream_curl = ""
            logger.warning("upstream_curl_startup_fallback", detail=str(exc))
    else:
        settings, upstream_curl_model_id = apply_upstream_curl(settings)

    restore_runtime_state(settings)

    if from_env and (
        os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID")
    ):
        target = (settings.target_domain or "").strip().lower()
        if target in {"", "localhost", "127.0.0.1"}:
            settings.target_domain = "arena.ai"
            if settings.upstream_referer_path == "/c/{chat_id}":
                settings.upstream_referer_path = "/text/direct"
        keys = [k.strip() for k in settings.api_keys if k.strip()]
        if not keys or keys == ["sk-local-dev-key"]:
            settings.api_keys = [f"sk-render-{secrets.token_urlsafe(24)}"]

    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
        root_path=settings.root_path,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.upstream_curl_model_id = upstream_curl_model_id

    # Middleware sırası: son eklenen en dışta çalışır.
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-response-time-ms"],
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/v1")
    app.include_router(models.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1")
    # OpenAI SDK `base_url` bazen `/v1` olmadan verilir; aksi halde /chat/completions 404 olur.
    app.include_router(chat.router, include_in_schema=False)
    app.include_router(models.router, include_in_schema=False)

    return app


app = create_app()


def main() -> None:  # pragma: no cover - CLI giriş noktası
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,  # structlog zaten yapılandırıldı
        access_log=False,
        reload=settings.debug,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
