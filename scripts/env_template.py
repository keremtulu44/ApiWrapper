#!/usr/bin/env python3
"""Bütün ortam değişkenlerinin alfabetik tam listesini üretir.

Liste elle yazılmaz: adlar ``Settings`` ve ``TelegramSettings`` alanlarından
gelir, bu yüzden yeni bir ayar eklendiğinde doküman sessizce eksik kalamaz
(``tests/test_env_template.py`` sürüklenmeyi yakalar).

Her satır dört durumdan birini taşır:

* ``✅`` bu kurulumda **yazılması gereken** bir değer var.
* ``⬜`` satır **boş bırakılmalı** (dolu olması bir şeyi bozar ya da gereksizdir).
* ``⛔`` satır **hiç yazılmamalı** (istek/oturum başına üretilir, sabitlenirse 401/403/409).
* ``↺`` varsayılan doğru; **dokunma**.

Kullanım::

    python -m scripts.env_template            # docs/ENV_FULL.md üretir
    python -m scripts.env_template --env      # kopyalanabilir .env iskeleti
    python -m scripts.env_template --check    # dosya güncel mi (CI/test)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.telegram.bot import TelegramSettings

DOC_PATH = Path("docs/ENV_FULL.md")

WRITE = "✅"
EMPTY = "⬜"
NEVER = "⛔"
DEFAULT = "↺"

MARKERS: dict[str, str] = {WRITE: "yaz", EMPTY: "boş", NEVER: "yazma", DEFAULT: "varsayılan"}

#: Elle bakım gerektiren anahtarlar: ad -> (işaret, "ne yazacaksın").
#: Burada olmayan anahtarlar için varsayılan değerden otomatik cümle kurulur.
ENTRIES: dict[str, tuple[str, str]] = {
    # ---------------------------------------------------------------- sunucu
    "APP_NAME": (DEFAULT, "`ApiWrapper` — servis adı, loglarda görünür."),
    "APP_VERSION": (DEFAULT, "`1.0.0` — sürüm etiketi."),
    "HOST": (DEFAULT, "`0.0.0.0` — konteynerde/Render'da böyle kalmalı."),
    "PORT": (DEFAULT, "`8000`. Render `PORT`'u kendi verir, yerelde 8000."),
    "DEBUG": (DEFAULT, "`false`. Açarsan hata gövdeleri istemciye ayrıntı sızdırır."),
    "LOG_LEVEL": (DEFAULT, "`INFO`. Teşhis sırasında `DEBUG`."),
    "LOG_JSON": (DEFAULT, "`true` — Render log panelinde alan adlarıyla görünür."),
    "ROOT_PATH": (EMPTY, "Ters proxy alt yolda sunmuyorsa boş."),
    # ------------------------------------------------------------------ auth
    "API_KEYS": (WRITE, "En az bir anahtar: `sk-local-dev-key`. İstemciler `Authorization: Bearer <bu>` yollar."),
    "AUTH_DISABLED": (DEFAULT, "`false` — `true` yapmak API'yi kimliksiz bırakır."),
    "ADMIN_API_KEYS": (
        WRITE,
        "Oturumu değiştiren `/v1/admin` uçları için ayrı anahtar; boş bırakırsan tüm `API_KEYS` admin sayılır. Telegram botu da bunu kullanır.",
    ),
    "CORS_ORIGINS": (DEFAULT, "`*`; tarayıcıdan çağrı yoksa da `*` zararsız."),
    "CORS_ALLOW_CREDENTIALS": (DEFAULT, "`false` — çerezle tarayıcı çağrısı yapmayacaksan."),
    # -------------------------------------------------------------- upstream
    "UPSTREAM_CURL": (
        WRITE,
        "Tam `create-evaluation` cURL'ü. **Tek kaynak bu olsun**; doldurursan `UPSTREAM_COOKIE`'yi boş bırak (açılışta cURL çerezi ezer).",
    ),
    "TARGET_DOMAIN": (
        WRITE,
        "Gerçek host, **şemasız** (`ornek-servis.com`). Boş kalırsa `/readyz` 503 `target_domain_not_configured`.",
    ),
    "UPSTREAM_SCHEME": (DEFAULT, "`https`; yalnız yerel taklit sunucu için `http`."),
    "UPSTREAM_STREAM_PATH": (
        WRITE,
        "Yakalanan cURL'ün yolu: `/nextjs-api/stream/create-evaluation`. Yolu uydurma; 404 alırsın.",
    ),
    "UPSTREAM_REFERER_PATH": (WRITE, "`/text/direct` — cURL'ün `referer` yolundan birebir."),
    "UPSTREAM_CAPTURED_MODEL_ID": (
        EMPTY,
        "cURL içe aktarılınca kendisi dolar (`modelAId` UUID). Elle yazma.",
    ),
    "UPSTREAM_SNAPSHOT_VERSION": (WRITE, "`1` — kimlik + tarayıcı izi dahil tam snapshot."),
    "UPSTREAM_CX": (
        NEVER,
        "Boş bırak. İstek başına üretilir; sabit değer ikinci sohbette reddedilir.",
    ),
    "UPSTREAM_CX_SEND": (WRITE, "`false` — canlı gözlemde bu alan olmadan da istek geçti."),
    "UPSTREAM_MODE": (WRITE, "`direct-battle` — gövdedeki `mode` alanı; cURL'den birebir."),
    "UPSTREAM_RECAPTCHA_FIELD": (
        WRITE,
        "`recaptchaV3Token` (v2 görürsen `recaptchaV2Token`) — gövdedeki captcha alan adı.",
    ),
    "UPSTREAM_ACCEPT_LANGUAGE": (
        WRITE,
        "Yakalandığı cihazınkiyle birebir: `tr,en;q=0.9,en-GB;q=0.8,en-US;q=0.7`.",
    ),
    "UPSTREAM_USER_AGENT": (
        WRITE,
        "cURL'deki UA ile birebir; sürüm numarası dahil (`Edg/154` gibi). `sec-ch-ua` bundan türetilir.",
    ),
    "UPSTREAM_COOKIE": (
        EMPTY,
        "`UPSTREAM_CURL` kullanıyorsan boş. Yalnız çerezle gideceksen **tam** `Cookie` başlığı, parçalı `.0/.1` dahil.",
    ),
    "UPSTREAM_AUTH_FROM_COOKIE": (
        WRITE,
        "Tarayıcı isteğinde `authorization` başlığı **yoksa** `false`; 401 alıyorsan `true` (token çerezden ayıklanır).",
    ),
    "UPSTREAM_ACCESS_TOKEN": (
        EMPTY,
        "Boş bırak. Yer tutucu (`APIWRAPPER_EMPTY_VALUE_V1`) yazarsan yüklenirken boşaltılır ve 401 sürer.",
    ),
    "UPSTREAM_TOKEN_COOKIE_NAMES": (
        EMPTY,
        "Çerez adını biliyorsan yaz (`arena-auth-prod-v1` gibi); bilmiyorsan boş → sezgisel arama.",
    ),
    "UPSTREAM_AUTH_SCHEME": (WRITE, "`Bearer` — tarayıcı başlığında şema yoksa boş bırak."),
    "UPSTREAM_WARN_ON_EXPIRED_TOKEN": (DEFAULT, "`true` — süresi dolmuş JWT'yi loglar, isteği yine dener."),
    "UPSTREAM_EXTRA_HEADERS": (
        EMPTY,
        "`dnt=1` yeterli. `x-deployment-id` / `x-arena-web-deployment` **buraya yazma** (her deploy'da değişir).",
    ),
    "UPSTREAM_PROXY": (EMPTY, "Proxy yoksa boş; varsa `http://kullanici:sifre@host:port`."),
    "UPSTREAM_VERIFY_TLS": (DEFAULT, "`true` — kapatmak MITM'e açık kapı bırakır."),
    "UPSTREAM_HTTP2": (DEFAULT, "`true` — tarayıcı parmak izine uyar."),
    # ------------------------------------------------------- sürüm damgası
    "DEPLOYMENT_ID_REFRESH_ENABLED": (DEFAULT, "`true` — kanarya damgayı upstream sayfasından okur."),
    "DEPLOYMENT_ID_TTL_SECONDS": (DEFAULT, "`1800`. DİKKAT: adında `UPSTREAM_` öneki **yok**."),
    "DEPLOYMENT_PROBE_PATH": (DEFAULT, "`/` — damganın okunduğu HTML sayfası."),
    "DEPLOYMENT_PROBE_TIMEOUT": (DEFAULT, "`8` saniye."),
    "DEPLOYMENT_ID_AUTO_RETRY": (DEFAULT, "`true` — 409'da damga tazelenip istek bir kez tekrarlanır."),
    "DEPLOYMENT_STAMP_STRIP_ON_CONFLICT": (
        DEFAULT,
        "`true` — damga bulunamazsa bayat başlıklar atılıp bir kez denenir.",
    ),
    # ------------------------------------------------------ kota / hesap
    "UPSTREAM_LIMIT_MARKERS": (
        DEFAULT,
        "Örnekteki virgüllü liste. Ayırt edici ibare seç; `rate limit` gibi genel söz yanlış alarm üretir.",
    ),
    "QUOTA_TEXT_SCAN_CHARS": (DEFAULT, "`0` = düz metin taraması kapalı; kısıtlama düz metin geliyorsa `300`."),
    "UPSTREAM_ACCOUNT_NAME": (DEFAULT, "`hesap-1` — loglarda hangi hesabın kullanıldığını gösterir."),
    "ACCOUNT_MSG_BUDGET": (DEFAULT, "`15` — yumuşak bütçe; isteği engellemez, hesap seçimini yönlendirir."),
    "ACCOUNT_QUOTA_WINDOW_SECONDS": (DEFAULT, "`1200` — bütçenin sayıldığı pencere."),
    "ACCOUNT_BUDGET_GROWTH_STREAK": (DEFAULT, "`3` — üst üste başarıdan sonra bütçe artışı."),
    "ACCOUNT_COOLDOWN_SECONDS": (DEFAULT, "`1200` — kilit yiyen hesabın dinlenme süresi."),
    "ACCOUNT_MAX_SWITCHES": (DEFAULT, "`2` — tek istekte en fazla hesap değişimi."),
    # ------------------------------------------------------------- zamanlar
    "CONNECT_TIMEOUT": (DEFAULT, "`10` sn."),
    "READ_TIMEOUT": (DEFAULT, "`300` sn — akış için üst sınır."),
    "WRITE_TIMEOUT": (DEFAULT, "`30` sn."),
    "POOL_TIMEOUT": (DEFAULT, "`10` sn — bağlantı havuzu beklemesi."),
    "TOTAL_REQUEST_TIMEOUT": (DEFAULT, "`600` sn."),
    "STREAM_IDLE_TIMEOUT": (DEFAULT, "`120` sn — bu süre veri gelmezse akış düşer."),
    # --------------------------------------------------------------- refresh
    "UPSTREAM_REFRESH_ENABLED": (DEFAULT, "`true` — erişim token'ı kendiliğinden yenilenir."),
    "UPSTREAM_REFRESH_LEEWAY_SECONDS": (
        DEFAULT,
        "`600`. JWT ömrünün yarısından büyük olursa her istekte yenileme tetiklenir; `/v1/admin/auth` bunu uyarır.",
    ),
    "UPSTREAM_REFRESH_CHECK_INTERVAL_SECONDS": (DEFAULT, "`60` sn."),
    "UPSTREAM_REFRESH_URL": (EMPTY, "Boş → çerezdeki JWT'nin `iss` alanından türetilir. Yenileme 401 verirse doldur."),
    "UPSTREAM_REFRESH_API_KEY": (EMPTY, "Upstream `apikey` istiyorsa; boşsa siteden otomatik keşfedilir."),
    "UPSTREAM_REFRESH_TOKEN": (
        EMPTY,
        "Çerezde refresh token varsa boş bırak. Yenilemede dönen yeni değer bellekte tutulur.",
    ),
    "UPSTREAM_STATE_FILE": (
        WRITE,
        "`.cache/upstream_state.json` — dönen token ve damga restart'ta buradan geri yüklenir.",
    ),
    "RENDER_API_KEY": (EMPTY, "Yalnız Render'da restart sonrası oturum korunsun istiyorsan yaz."),
    "RENDER_SERVICE_ID": (EMPTY, "Render kendisi verir; elle yazma."),
    # -------------------------------------------------------------- keepalive
    "UPSTREAM_KEEPALIVE_INTERVAL_SECONDS": (
        DEFAULT,
        "`0` = kapalı. `10800` = üç saatte bir kısa gerçek completion (kota ve captcha harcar).",
    ),
    "UPSTREAM_KEEPALIVE_MODEL": (DEFAULT, "`max` — probe'un kullanacağı model."),
    "UPSTREAM_KEEPALIVE_INITIAL_DELAY_SECONDS": (DEFAULT, "`60` — deploy sonrası ilk probe."),
    "UPSTREAM_KEEPALIVE_JITTER_SECONDS": (DEFAULT, "`300` — tam saatte atılan istek desenini kırar."),
    "UPSTREAM_KEEPALIVE_STREAM_SMOKE_INTERVAL_SECONDS": (
        DEFAULT,
        "`86400` — günde bir gerçek akış yolu denemesi; `0` = kapalı.",
    ),
    # -------------------------------------------------------------- bildirim
    "TELEGRAM_ALERTS_ENABLED": (DEFAULT, "`true` — arıza ve düzelme bildirimi."),
    "TELEGRAM_ALERT_REPEAT_SECONDS": (DEFAULT, "`21600` — aynı arıza sürerken hatırlatma aralığı."),
    # ------------------------------------------------------- sağlık & metrik
    "HEALTH_EXPOSE_UPSTREAM": (DEFAULT, "`false` — `/health` upstream hostunu gizler."),
    "METRICS_REQUIRE_AUTH": (DEFAULT, "`false`; herkese açık adreste `true`."),
    "METRICS_ENABLED": (DEFAULT, "`true`."),
    # ----------------------------------------------------------------- havuz
    "MAX_CONNECTIONS": (DEFAULT, "`200`."),
    "MAX_KEEPALIVE_CONNECTIONS": (DEFAULT, "`50`."),
    "MAX_CONCURRENT_UPSTREAM": (DEFAULT, "`32` — eşzamanlı upstream isteği."),
    # --------------------------------------------------------------- dayanıklılık
    "RETRY_MAX_ATTEMPTS": (DEFAULT, "`3`."),
    "RETRY_BASE_DELAY": (DEFAULT, "`0.5` sn."),
    "RETRY_MAX_DELAY": (DEFAULT, "`8` sn."),
    "BREAKER_ENABLED": (DEFAULT, "`true`."),
    "BREAKER_FAILURE_THRESHOLD": (DEFAULT, "`5` üst üste hata."),
    "BREAKER_RESET_TIMEOUT": (DEFAULT, "`30` sn."),
    "RATE_LIMIT_ENABLED": (DEFAULT, "`true`."),
    "RATE_LIMIT_RPM": (DEFAULT, "`120` istek/dakika."),
    "RATE_LIMIT_BURST": (DEFAULT, "`30`."),
    # ----------------------------------------------------------------- girdi
    "MAX_BODY_BYTES": (DEFAULT, "`4194304` (4 MB)."),
    "MAX_MESSAGES": (DEFAULT, "`400`."),
    "MAX_PROMPT_CHARS": (DEFAULT, "`500000`."),
    "UNSUPPORTED_PARAMS": (DEFAULT, "`ignore`; `error` yaparsan şema dışı alan 400 döner."),
    # -------------------------------------------------------------- recaptcha
    "RECAPTCHA_PROVIDER": (
        WRITE,
        "`browser` — Enterprise token'ını gerçek tarayıcıda üretir. `static`/`noop` sabit token ister.",
    ),
    "RECAPTCHA_STATIC_TOKEN": (NEVER, "Boş bırak. Tek kullanımlık ve saniyeler içinde ölür."),
    "RECAPTCHA_TOKEN_TTL": (DEFAULT, "`100` sn — üretilen token'ın kullanım ömrü."),
    "RECAPTCHA_EXTERNAL_URL": (EMPTY, "Yalnız `external` sağlayıcısı için."),
    "RECAPTCHA_EXTERNAL_API_KEY": (EMPTY, "Yalnız `external` sağlayıcısı için."),
    "RECAPTCHA_EXTERNAL_TIMEOUT": (DEFAULT, "`60` sn — `external` için."),
    "RECAPTCHA_SITE_KEY": (WRITE, "`RECAPTCHA_SITE_KEY` = gözlenen sitedeki public site key (gizli değil, sayfada açık)."),
    "RECAPTCHA_VERSION": (WRITE, "`enterprise` — gözlenen hedef Enterprise ve `chat_submit` kullanıyor."),
    "RECAPTCHA_ACTION": (WRITE, "`chat_submit` — token'daki action bu olmalı."),
    "RECAPTCHA_BROWSER_TIMEOUT": (DEFAULT, "`45` sn — tarayıcıda token üretme süresi."),
    # ---------------------------------------------------------------- oturum
    "SESSION_STATELESS": (DEFAULT, "`true` — istemci `conversation_id` taşımıyorsa."),
    "SESSION_REUSE": (
        DEFAULT,
        "`false`. `true` yaparsan sohbet API anahtarına bağlanır ve tek upstream sohbetinden ilerlenir.",
    ),
    "SESSION_ROTATE_AFTER_MESSAGES": (DEFAULT, "`35` — sohbet bu sayıdan sonra yenilenir (`0` = sınırsız)."),
    "SESSION_ROTATE_AFTER_SECONDS": (DEFAULT, "`0` = yaş sınırı yok; kota penceresine hizalamak için `1200`."),
    "SESSION_TTL": (DEFAULT, "`1800` sn."),
    "SESSION_CACHE_SIZE": (DEFAULT, "`1024`. Tek worker ile çalıştır; sayaç worker'lar arasında paylaşılmaz."),
    # ---------------------------------------------------------------- modeller
    "MODELS_FILE": (DEFAULT, "`config/models.yaml` — `id` ↔ `modelAId` eşlemesi."),
    # ---------------------------------------------------------------- telegram
    "TELEGRAM_BOT_TOKEN": (EMPTY, "Bot kullanmayacaksan boş; doluysa bot aynı servis içinde başlar."),
    "WRAPPER_BASE_URL": (EMPTY, "Aynı serviste çalışıyorsa **yazma**; varsayılan yerel adres kullanılır."),
    "WRAPPER_API_KEY": (EMPTY, "Aynı serviste çalışıyorsa yazma; `API_KEYS` ilk değerinden türetilir."),
    "TELEGRAM_ALLOWED_CHAT_IDS": (EMPTY, "İlk `/start` chat ID'yi gösterir; sonra buraya yaz."),
    "TELEGRAM_ALERT_CHAT_ID": (EMPTY, "Boşsa allowlist'in ilk kaydı arıza bildirimi hedefi olur."),
    "TELEGRAM_MODEL": (DEFAULT, "`max` — wrapper üzerinden çağrılacak model adı."),
    "TELEGRAM_POLL_TIMEOUT": (DEFAULT, "`30` sn."),
    "TELEGRAM_REQUEST_TIMEOUT": (DEFAULT, "`180` sn."),
    "TELEGRAM_MAX_REPLY_CHARS": (DEFAULT, "`12000` — Telegram mesaj bölme sınırı."),
}


def _all_fields() -> dict[str, Any]:
    """Ad -> alan tanımı (Settings + TelegramSettings)."""
    fields: dict[str, Any] = {name.upper(): info for name, info in Settings.model_fields.items()}
    fields.update({name.upper(): info for name, info in TelegramSettings.model_fields.items()})
    return fields


def _group(key: str) -> str:
    """Doküman tablosunda gösterilecek kısa grup adı."""
    if key.startswith(("TELEGRAM_", "WRAPPER_")):
        return "telegram"
    if key.startswith(("RECAPTCHA_",)):
        return "captcha"
    if key.startswith(("DEPLOYMENT_",)):
        return "damga"
    if key.startswith(("UPSTREAM_REFRESH", "RENDER_")):
        return "yenileme"
    if key.startswith(("UPSTREAM_KEEPALIVE",)):
        return "keepalive"
    if key.startswith(("UPSTREAM_COOKIE", "UPSTREAM_ACCESS_TOKEN", "UPSTREAM_AUTH", "TARGET_DOMAIN")):
        return "kimlik"
    if key.startswith(("UPSTREAM_", "TARGET_DOMAIN")):
        return "upstream"
    if key.startswith(("ACCOUNT_", "QUOTA_")):
        return "kota"
    if key.startswith(("SESSION_", "MODELS_FILE")):
        return "oturum"
    if key.startswith(("MAX_", "RETRY_", "BREAKER_", "RATE_", "CONNECT_", "READ_", "WRITE_", "POOL_", "TOTAL_", "STREAM_", "UNSUPPORTED_")):
        return "dayanıklılık"
    if key.startswith(("HEALTH_", "METRICS_")):
        return "gözlem"
    return "sunucu"


def _slot_note(key: str) -> str | None:
    """`_2/_3/_4` sonekli hesap yuvası açıklaması."""
    for slot in range(2, MAX_ACCOUNT_SLOTS + 1):
        if key.endswith(f"_{slot}"):
            return (
                f"{slot}. hesap yuvası. Kullanmıyorsan boş bırak; "
                "boş yuva hesap havuzuna girmez."
            )
    return None


def entry_for(key: str, field: Any) -> tuple[str, str]:
    """Bir anahtar için (işaret, açıklama) döner."""
    curated = ENTRIES.get(key)
    if curated is not None:
        return curated

    slot = _slot_note(key)
    if slot is not None:
        default = field.default
        if default not in ("", None):
            return (DEFAULT, f"{slot} Varsayılan `{default}`.")
        return (EMPTY, slot)

    default = field.default
    if default in (None, "", []):
        return (EMPTY, "Boş bırak.")
    if isinstance(default, bool):
        return (DEFAULT, f"`{str(default).lower()}` — dokunma.")
    return (DEFAULT, f"`{default}` — dokunma.")


def rows() -> list[tuple[str, str, str, str]]:
    """Alfabetik satırlar: (anahtar, işaret, açıklama, grup)."""
    fields = _all_fields()
    out: list[tuple[str, str, str, str]] = []
    for key in sorted(fields):
        marker, text = entry_for(key, fields[key])
        out.append((key, marker, text, _group(key)))
    return out


def render_markdown() -> str:
    """`docs/ENV_FULL.md` içeriği."""
    lines = [
        "# Ortam değişkenlerinin tam listesi",
        "",
        "> Bu dosya **üretilir**: `python -m scripts.env_template`. Elle düzenleme;",
        "> `tests/test_env_template.py` sürüklenmeyi yakalar. Liste alfabetiktir ve",
        "> `Settings` + `TelegramSettings` alanlarının tamamını içerir.",
        "",
        "İşaretler:",
        "",
        "| İşaret | Anlamı |",
        "|---|---|",
        f"| {WRITE} | Bu kurulumda yazılması gereken bir değer var |",
        f"| {EMPTY} | Satır boş bırakılmalı (dolu olması bir şeyi bozar ya da gereksiz) |",
        f"| {NEVER} | Hiç yazılmamalı — istek/oturum başına üretilir, sabitlenirse 401/403/409 |",
        f"| {DEFAULT} | Varsayılan doğru; dokunma |",
        "",
        "Gizli değer yazan satırlar yoktur: buraya kendi çerezini/token'ını yapıştırma,",
        "o değerler `/v1/admin/auth` (cookie/curl) veya `UPSTREAM_CURL` üzerinden girer.",
        "",
        "| Değişken | Ne yazacaksın | Grup |",
        "|---|---|---|",
    ]
    for key, marker, text, group in rows():
        lines.append(f"| `{key}` | {marker} {text} | {group} |")
    write_count = sum(1 for row in rows() if row[1] == WRITE)
    empty_count = sum(1 for row in rows() if row[1] == EMPTY)
    lines += [
        "",
        f"Toplam **{len(rows())}** anahtar: {write_count} yazılacak, "
        f"{empty_count} boş, {len(rows()) - write_count - empty_count} varsayılan/yazma.",
        "Denetim için: `python -m scripts.env_audit .env`.",
        "",
    ]
    return "\n".join(lines)


def _short(text: str) -> str:
    """Yorum satırı için ilk cümleyi alır (numaralı yuva notlarını bölmeden)."""
    if re.match(r"^\d+\.\s", text):
        return text
    return text.split(". ")[0].rstrip(".")


def render_env() -> str:
    """Kopyalanabilir `.env` iskeleti: yazılacaklar dolu, gerisi yorumlu."""
    lines = [
        "# `python -m scripts.env_template --env` ile üretildi.",
        "# ✅ yazılacak · ⬜ boş bırak · ⛔ yazma · ↺ varsayılan",
        "",
    ]
    for key, marker, text, _group in rows():
        if marker in (EMPTY, NEVER):
            lines.append(f"# {marker} {key}=  — {_short(text)}")
        elif marker == DEFAULT:
            continue
        else:
            lines.append(f"{key}={_placeholder(key)}  # {text}")
    return "\n".join(lines) + "\n"


def _placeholder(key: str) -> str:
    """`.env` iskeletinde yazılacak değer (gizli olmayan)."""
    return {
        "API_KEYS": "sk-local-dev-key",
        "ADMIN_API_KEYS": "sk-local-admin-key",
        "TARGET_DOMAIN": "ornek-servis.com",
        "UPSTREAM_STREAM_PATH": "/nextjs-api/stream/create-evaluation",
        "UPSTREAM_REFERER_PATH": "/text/direct",
        "UPSTREAM_SNAPSHOT_VERSION": "1",
        "UPSTREAM_CX_SEND": "false",
        "UPSTREAM_MODE": "direct-battle",
        "UPSTREAM_RECAPTCHA_FIELD": "recaptchaV3Token",
        "UPSTREAM_ACCEPT_LANGUAGE": "tr,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
        "UPSTREAM_USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
        ),
        "UPSTREAM_AUTH_FROM_COOKIE": "false",
        "RECAPTCHA_PROVIDER": "browser",
        "RECAPTCHA_SITE_KEY": "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0",
        "RECAPTCHA_VERSION": "enterprise",
        "RECAPTCHA_ACTION": "chat_submit",
        "UPSTREAM_CURL": "curl '<tam create-evaluation cURL>'",
    }.get(key, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tam ortam değişkeni listesini üretir.")
    parser.add_argument("--env", action="store_true", help="`.env` iskeleti yazdır")
    parser.add_argument("--check", action="store_true", help="docs/ENV_FULL.md güncel mi")
    args = parser.parse_args(argv)

    if args.env:
        print(render_env())
        return 0

    content = render_markdown()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != content:
            print(f"{DOC_PATH} güncel değil: python -m scripts.env_template", file=sys.stderr)
            return 1
        return 0

    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"{DOC_PATH} yazıldı ({len(rows())} anahtar).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
