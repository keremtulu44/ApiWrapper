"""FastAPI uygulama fabrikası ve yaşam döngüsü yönetimi."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.v1 import admin, ask, autopilot, chat, health, models
from app.autopilot.curl_parse import normalize_windows_cmd_curl_for_upstream
from app.autopilot.upstream_slots import UpstreamSlotRuntimeManager
from app.core.config import MAX_ACCOUNT_SLOTS, Settings, get_settings
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
from app.services.lanes import LaneSet
from app.services.model_registry import ModelEntry, ModelRegistry
from app.services.recaptcha import create_recaptcha_provider
from app.services.session_manager import SessionManager
from app.telegram.bot import TelegramSettings, run_from_settings
from app.upstream.browser_login import SessionKeeper, login_configured, run_session_keeper
from app.upstream.client import UpstreamClient
from app.upstream.curl_runtime import apply_upstream_curl
from app.utils.tasks import TaskRegistry

logger = get_logger(__name__)

DESCRIPTION = """
OpenAI uyumlu bir REST cephesi üzerinden harici bir Web LLM servisine erişim.

* `POST /v1/chat/completions` — streaming (SSE) ve non-streaming
* `POST /v1/completions` — eski metin tamamlama biçimi
* `POST /v1/ask` — otomatik sistemler için: `wait` / `done` / `problem` satırları
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
    try:
        await run_from_settings(settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # httpx hata metni Telegram token'lı URL taşıyabilir; yalnız türü logla.
        logger.error("telegram_bot_stopped_unexpectedly", error_type=type(exc).__name__)


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
    try:
        await recaptcha.startup()
    except Exception as exc:
        # Captcha sağlayıcısı başlatılamazsa servis yine ayakta kalsın;
        # hata ilk istekte anlamlı biçimde raporlanır.
        logger.error("recaptcha_startup_failed", provider=recaptcha.name, error=str(exc))

    sessions = SessionManager(settings, warmup=upstream.warmup)
    registry = _load_registry(settings)
    if model_id := app.state.upstream_curl_model_id:
        entry = registry.resolve(None)
        entry.upstream_id = model_id
        logger.info("upstream_curl_model_loaded", model=entry.id)
    accounts = build_account_pool(settings)
    # Hesap başına bir hat: kendi sırası ve kendi sağlık kaydı. İstekler hatlar
    # arasında eşit bölünür; tek hesapta tek hat vardır ve bütün tamamlamalar o
    # sıradan geçer (bkz. `app/services/lanes.py`).
    lanes = LaneSet.from_settings(settings, accounts)
    queue = lanes.primary.queue
    upstream_health = lanes.primary.health
    # Tek bildirimci: probe ve `/v1/ask` aynı arıza kaydını paylaşır ki aynı
    # arıza iki kaynaktan iki ayrı mesaj üretmesin.
    alerter = build_alerter(settings)
    tasks = TaskRegistry()

    app.state.upstream = upstream
    app.state.recaptcha = recaptcha
    app.state.sessions = sessions
    app.state.registry = registry
    app.state.accounts = accounts
    app.state.lanes = lanes
    #: Hesap rotasyon motoru (kapalıysa `None`): `/v1/admin/accounts` ve
    #: Telegram `/hesaplar` buradan "hangi kural işliyor" bilgisini okur.
    app.state.rotation = lanes.rotation
    # İlk hattın sırası ve sağlık kaydı: tek hesaplı kurulumda "sıra" ve "upstream
    # durumu" tam olarak bunlardır.
    app.state.queue = queue
    app.state.upstream_health = upstream_health
    app.state.alerter = alerter
    app.state.tasks = tasks
    app.state.completion_service = CompletionService(
        settings=settings,
        upstream=upstream,
        registry=registry,
        sessions=sessions,
        recaptcha=recaptcha,
        accounts=accounts,
        lanes=lanes,
    )

    telegram_task: asyncio.Task[None] | None = None
    if telegram_settings := _embedded_telegram_settings(settings):
        telegram_task = asyncio.create_task(
            _run_embedded_telegram(telegram_settings), name="telegram-bot"
        )

    # Kalıcı tarayıcı girişi (varsayılan kapalı; bu hedefte önerilmez, bkz.
    # `Settings.upstream_login_enabled`). Açıksa çerezi bu görev tazeler;
    # CAPTCHA'ya dokunmaz (static kalır).
    keeper_task: asyncio.Task[None] | None = None
    app.state.session_keeper = SessionKeeper(settings) if login_configured(settings) else None
    if app.state.session_keeper is not None:
        keeper_task = asyncio.create_task(
            run_session_keeper(
                settings,
                keeper=app.state.session_keeper,
                interval_seconds=settings.upstream_login_interval_seconds,
            ),
            name="browser-login",
        )
        logger.info("upstream_login_keeper_started")
    elif settings.upstream_login_enabled:
        logger.warning(
            "upstream_login_not_configured",
            hint="UPSTREAM_LOGIN_EMAIL / UPSTREAM_LOGIN_PASSWORD eksik.",
        )

    slot_manager = UpstreamSlotRuntimeManager(
        app,
        keepalive_runner=lambda *a, **kw: run_upstream_keepalive(*a, **kw),
    )
    app.state.slot_manager = slot_manager
    slot_manager.start_initial_keepalives()

    logger.info(
        "application_started",
        version=settings.app_version,
        models=len(registry),
        recaptcha_provider=recaptcha.name,
        upstream=settings.base_url,
        accounts=accounts.size,
        lanes=[lane.label for lane in lanes.lanes if lane.label],
        account_failover=settings.account_failover if lanes.multi else None,
    )
    try:
        yield
    finally:
        if keeper_task is not None:
            keeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper_task
            with contextlib.suppress(Exception):
                await app.state.session_keeper.shutdown()
        await slot_manager.shutdown()
        if telegram_task is not None:
            telegram_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telegram_task
        if autopilot_mgr := getattr(app.state, "autopilot_manager", None):
            await autopilot_mgr.shutdown()
        # Süren `/v1/ask` işleri ve gönderilmekte olan bildirimler.
        await tasks.shutdown()
        with contextlib.suppress(Exception):
            await recaptcha.shutdown()
        with contextlib.suppress(Exception):
            await upstream.shutdown()
        await sessions.clear()
        logger.info("application_stopped")


def _prenormalize_startup_curls(settings: Settings) -> Settings:
    """Render ortam değişkenine mobilden yapıştırılan `$'...'` cURL'lerini normalize eder."""
    updates: dict[str, object] = {}
    slot_attrs = ("upstream_curl", *(f"upstream_curl_{i}" for i in range(2, MAX_ACCOUNT_SLOTS + 1)))
    for attr in slot_attrs:
        raw = getattr(settings, attr, "")
        if isinstance(raw, str) and raw.strip():
            norm = normalize_windows_cmd_curl_for_upstream(raw)
            if norm != raw:
                updates[attr] = norm
    return settings.model_copy(update=updates) if updates else settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Uygulamayı oluşturur (testler kendi Settings'ini geçebilir)."""
    settings = settings or get_settings()
    settings = _prenormalize_startup_curls(settings)
    settings, upstream_curl_model_id = apply_upstream_curl(settings)
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
    app.include_router(ask.router, prefix="/v1")
    app.include_router(models.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1")
    app.include_router(autopilot.router, prefix="/v1")
    # OpenAI SDK `base_url` bazen `/v1` olmadan verilir; aksi halde /chat/completions 404 olur.
    app.include_router(chat.router, include_in_schema=False)
    app.include_router(ask.router, include_in_schema=False)
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
