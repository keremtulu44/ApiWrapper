"""Uygulama yapılandırması — tek doğruluk kaynağı.

Tüm ayarlar ortam değişkenlerinden (veya .env dosyasından) okunur.
Hedef domain ve model kimlikleri KODA GÖMÜLMEZ; config'den gelir.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

UnsupportedParamPolicy = Literal["ignore", "hint", "error"]
RecaptchaProviderName = Literal["static", "noop", "browser", "external"]
RecaptchaVersion = Literal["v3", "enterprise"]

#: cURL içe aktarıcı ve hesap havuzu yalnızca aşağıda açıkça tanımlanan
#: soneksiz/_2/_3/_4 yuvalarını destekler. Araçların daha fazla yuvaya
#: "kaydedildi" deyip uygulamanın onları yok saymasını önlemek için tek kaynak.
MAX_ACCOUNT_SLOTS = 4


class Settings(BaseSettings):
    """Ortam değişkeni tabanlı ayarlar."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- server
    app_name: str = "ApiWrapper"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    root_path: str = ""

    # ------------------------------------------------------------------ auth
    #: Yerel istemcilerin kullanacağı API anahtarları (virgülle ayrılmış).
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["sk-local-dev-key"]
    )
    #: True ise kimlik doğrulama tamamen devre dışı (yalnız güvenli yerel ağda).
    auth_disabled: bool = False

    # ------------------------------------------------------------------ CORS
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    cors_allow_credentials: bool = False

    # -------------------------------------------------------------- upstream
    #: Tarayıcıdan kopyalanan tam cURL. Render gibi ortamlarda secret olarak
    #: verilir; doluysa aşağıdaki ayrı upstream alanlarının yerine başlangıçta
    #: ayrıştırılır. Ham değer loglanmaz veya diske yazılmaz.
    upstream_curl: str = ""
    #: 2.–4. hesabın tam cURL'ü (isteğe bağlı, aynı servisin **başka** hesabı).
    #: Boşsa servis tek hesapla eskisi gibi çalışır. Doluysa o hesabın kimliği
    #: (çerez/token, tarayıcı başlıkları, statik captcha token'ı) bu cURL'den
    #: okunur; akış yolu, `mode` gibi ortak alanlar 1. hesabın cURL'ünden gelir.
    #: İstekler hesaplar arasında eşit bölünür (bkz. `app/services/lanes.py`).
    upstream_curl_2: str = ""
    upstream_curl_3: str = ""
    upstream_curl_4: str = ""
    #: Örn. "example-llm.com" (şemasız!). Zorunlu alan.
    target_domain: str = "localhost"
    upstream_scheme: Literal["https", "http"] = "https"
    #: Stream uç noktası şablonu. `{chat_id}` yer tutucusu varsa doldurulur;
    #: yoksa yol olduğu gibi kullanılır (kimlik yalnızca gövdede taşınıyor demektir).
    #: Varsayılan eski bir uçtur; tarayıcı cURL'ünden `scripts/curl_to_env.py` ile
    #: alınmalıdır — aksi halde upstream HTTP 404 döner.
    upstream_stream_path: str = "/nextjs-api/stream/post-to-evaluation/{chat_id}"
    #: Tarayıcıdaki sohbet sayfası yolu (referer üretimi için).
    upstream_referer_path: str = "/c/{chat_id}"
    #: Gövdeye yazılacak `mode` alanı (örn. "direct-battle"). Boşsa alan
    #: gönderilmez — gözlenen hedef alanı zorunlu tutuyor, boşsa `400 ZodError`.
    #: `UPSTREAM_CURL` doluyken bu ayar env'den **okunmaz**: değer cURL'ün
    #: gövdesinden gelir; gövdede `mode` yoksa alan boşa sıfırlanır
    #: (`curl_runtime.apply_upstream_curl`). Env değeri yalnız cURL'süz
    #: (parça parça) kurulumda kullanılır. Aynısı `upstream_stream_path` için de
    #: geçerlidir: cURL varsa yol cURL'ün adresinden okunur.
    upstream_mode: str = ""
    #: Gövdedeki reCAPTCHA alanının adı. Hedefe göre "recaptchaV3Token" veya
    #: "recaptchaV2Token" olabilir; tarayıcı isteğinden birebir kopyalayın.
    upstream_recaptcha_field: str = "recaptchaV3Token"
    #: Accept-Language başlığı (tarayıcınızın gönderdiğiyle aynı olmalı).
    upstream_accept_language: str = "en-US,en;q=0.9"
    upstream_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    )
    #: Ham Cookie başlığı (oturum gerekiyorsa).
    upstream_cookie: str = ""
    #: Doğrudan bearer token. Verilirse çerezden ayıklamaya gerek kalmaz (en yüksek öncelik).
    upstream_access_token: str = ""
    #: Çerezden okunan yenileme anahtarı (kalıcı tarayıcı girişi yazar).
    upstream_refresh_token: str = ""
    #: Cookie içinden `access_token` ayıklanıp `Authorization: Bearer` olarak eklensin mi?
    #: Varsayılan False: tarayıcı cURL'inde bu başlık yoksa eklemek parmak izini bozar.
    #: Hedef 401 döndürüyorsa true yapın.
    upstream_auth_from_cookie: bool = False
    #: Token'ın aranacağı çerez adları (virgülle ayrılmış). Boşsa sezgisel arama yapılır.
    upstream_token_cookie_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Authorization başlığının şeması. Boş bırakılırsa token ham olarak gönderilir.
    upstream_auth_scheme: str = "Bearer"
    #: Token süresi dolmuşsa uyar (JWT `exp`); istek yine de denenir.
    upstream_warn_on_expired_token: bool = True
    #: Ek başlıklar, "K1=V1;K2=V2" biçiminde.
    upstream_extra_headers: str = ""
    upstream_proxy: str | None = None
    upstream_verify_tls: bool = True
    upstream_http2: bool = True

    # ------------------------------------------- deployment (sürüm) kanaryası
    #: `x-deployment-id` / `x-arena-web-deployment` gibi sürüm damgalarını
    #: upstream'in kendi sayfasından okuyup tazele. Kapalıysa `UPSTREAM_CURL`'deki
    #: donmuş değer kullanılır (eski davranış).
    deployment_id_refresh_enabled: bool = True
    #: Okunan değerin önbellek ömrü (saniye). Her istekte sayfa okunmaz.
    deployment_id_ttl_seconds: float = 1800.0
    #: Damganın okunacağı yol (ana sayfa yeterlidir).
    deployment_probe_path: str = "/"
    #: Kanarya okuması için zaman aşımı (saniye).
    deployment_probe_timeout: float = 8.0
    #: 409 `refresh_required` alınınca damga zorla yenilenip istek bir kez
    #: tekrar edilsin mi? (Tekrar edilen istek mevcut retry bütçesinden bir yuva
    #: kullanır; 409 zaten nadirdir.)
    deployment_id_auto_retry: bool = True

    # --------------------------------------------------------------- quota
    #: Upstream'in hesap kısıtlamasını bildiren metin işaretleri (virgülle ayrılmış).
    #: İşaretler **ayırt edici ifadeler** olmalı: "rate limit" gibi genel sözler bir
    #: modelin meşru cevabında da geçebileceği için düz metin taramasında yanlış
    #: alarm üretir. Hedef servisin gerçek mesajını gözlemlediğinizde buraya birebir
    #: yazın (örn. "upstream limit reached").
    #:
    #: Varsayılan liste canlı sürümle (`03aca2c`) **birebir aynıdır** ve bilerek
    #: öyle tutulur: liste, upstream'e bir isteğin kaç kez gönderileceğini belirler
    #: (kota sayılan 429 tekrar denenmez, sayılmayan denenir). 18 Kasım 2026'da
    #: görülen `{"error":"daily spend limit reached"}` gövdesi bu listede **yoktur**;
    #: o 429 canlıdaki gibi tekrar denenir. Kota olarak tanınması istenirse kod
    #: değişmez, operatör env'e tam listeyi yazar (liste varsayılanın YERİNE geçer):
    #: `.env.example` → `UPSTREAM_LIMIT_MARKERS`, docs/LESSONS §16.6 ve §20.
    upstream_limit_markers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "upstream limit reached",
            "message limit reached",
            "conversation limit reached",
            "daily limit reached",
            "usage limit reached",
            "quota exceeded",
            "rate limit exceeded",
            "too many requests",
        ]
    )
    #: Yanıtın ilk kaç karakterinde düz metin kota taraması yapılsın. 0 = kapalı.
    #: Uçlar kısıtlama mesajını bazen hata olayı yerine metin delta'sı olarak
    #: gönderir; varsayılan kapalılık yanlış alarm riskini sıfırda tutar.
    quota_text_scan_chars: int = 0

    # --------------------------------------------------------- account pool
    #: Hesap yuvaları. `scripts/curl_to_env.py` 1. cURL'ü soneksiz (yukarıdaki)
    #: anahtarlara, sonraki cURL'leri `_2`, `_3`, `_4` soneksli anahtarlara yazar.
    #: pydantic-settings yalnızca **tanımlı** alanları okuduğu için yuvalar burada
    #: açıkça bildirilir. Daha fazla hesap gerekirse aynı kalıp devam ettirilir
    #: (veya `config/accounts.yaml` desteği eklenir).
    upstream_account_name: str = ""

    upstream_cookie_2: str = ""
    target_domain_2: str = ""
    upstream_access_token_2: str = ""
    upstream_auth_from_cookie_2: bool = False
    upstream_auth_scheme_2: str = "Bearer"
    upstream_token_cookie_names_2: Annotated[list[str], NoDecode] = Field(default_factory=list)
    upstream_user_agent_2: str = ""
    upstream_accept_language_2: str = ""
    upstream_referer_path_2: str = ""
    recaptcha_static_token_2: str = ""
    upstream_account_2_name: str = ""

    upstream_cookie_3: str = ""
    target_domain_3: str = ""
    upstream_access_token_3: str = ""
    upstream_auth_from_cookie_3: bool = False
    upstream_auth_scheme_3: str = "Bearer"
    upstream_token_cookie_names_3: Annotated[list[str], NoDecode] = Field(default_factory=list)
    upstream_user_agent_3: str = ""
    upstream_accept_language_3: str = ""
    upstream_referer_path_3: str = ""
    recaptcha_static_token_3: str = ""
    upstream_account_3_name: str = ""

    upstream_cookie_4: str = ""
    target_domain_4: str = ""
    upstream_access_token_4: str = ""
    upstream_auth_from_cookie_4: bool = False
    upstream_auth_scheme_4: str = "Bearer"
    upstream_token_cookie_names_4: Annotated[list[str], NoDecode] = Field(default_factory=list)
    upstream_user_agent_4: str = ""
    upstream_accept_language_4: str = ""
    upstream_referer_path_4: str = ""
    recaptcha_static_token_4: str = ""
    upstream_account_4_name: str = ""

    #: Upstream'in hesap başına izin verdiği tahmini mesaj sayısı (kayan pencere).
    #: Bu bir **gözlemdir**: isteği engellemez, hesap seçimini de belirlemez
    #: (bölüşüm `app/services/lanes.py` içindedir). Kilitlerden öğrenilen tavanla
    #: (`learned_limit`) birlikte `/v1/admin/accounts` çıktısında görünür.
    account_msg_budget: int = 15
    #: Kota penceresinin uzunluğu (saniye). Hedef 20 dk kilitliyorsa 1200.
    account_quota_window_seconds: float = 1200.0
    #: Bütçenin bir mesaj artması için gereken üst üste "temiz pencere" sayısı
    #: (AIMD'nin toplama artırma adımı). Düşük değer limite hızlı yaklaşır ama
    #: kilit riskini artırır.
    account_budget_growth_streak: int = 3
    #: Kilit algılanan hesabın dinlendirme süresi (saniye).
    account_cooldown_seconds: float = 1200.0
    #: Tek bir istemci isteğinde en fazla kaç kez hesap değiştirilsin.
    account_max_switches: int = 2
    #: Birden çok hesap varken arızalı hesabın işi çalışan hesaba verilsin mi?
    #: True (varsayılan): arızalı hesap yeni istek almaz; kimlik/kota/captcha
    #: reddi alan istek cevap gönderilmeden çalışan hesaba taşınır. False: katı
    #: bölüşüm — her hesap yalnız kendi payını işler, arızalı hesabın payı
    #: hata/"problem" alır. Tek hesapta etkisizdir.
    account_failover: bool = True

    # -------------------------------------------------------------- timeouts
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    write_timeout: float = 30.0
    pool_timeout: float = 10.0
    #: Tek bir upstream isteği için toplam üst sınır (saniye).
    total_request_timeout: float = 600.0
    #: İki token arasında izin verilen en uzun sessizlik.
    stream_idle_timeout: float = 120.0

    # ------------------------------------------------------------- keepalive
    #: 0 = kapalı. Sentetik non-stream completion'ın aralığı (saniye). Canlıda
    #: denenmiş değerler: 600 (ep3: ~28 saat kesintisiz; günde ~144 probe kota harcar)
    #: ve 3600 (docs/ROLLBACK §12). Daha uzun aralık ölçülmedi.
    #: Süreç ayaktayken çalışır; upstream oturumunu yenileme garantisi değildir.
    upstream_keepalive_interval_seconds: float = 0.0
    upstream_keepalive_model: str = "max"
    #: İlk probe'un gecikmesi (saniye). Deploy sonrası durumu bir saat bekleyip
    #: öğrenmemek için varsayılan 60 sn: servis ayağa kalktıktan hemen sonra
    #: uçtan uca tek bir kontrol yapılır.
    upstream_keepalive_initial_delay_seconds: float = 60.0
    #: Aralığa eklenecek rastgele sapma (saniye, ±). Tam saatte atılan istek
    #: deseni "insan davranışı"ndan daha düzenli olduğu için jitter uygulanır.
    upstream_keepalive_jitter_seconds: float = 300.0
    #: Günde bir kez `stream: true` ile gerçek akış yolunu dene (0 = kapalı).
    #: Non-stream probe, streaming ucundaki bir kırılmayı (SSE/parçalama) görmez.
    upstream_keepalive_stream_smoke_interval_seconds: float = 86400.0
    #: True ise probe yalnız hesap **boştayken** atılır: aralık, o hesabın son
    #: başarılı gerçek isteğinden sayılır (istek 13.00'te geldiyse ve başka istek
    #: gelmediyse probe 14.00'te atılır; araya istek girdikçe ertelenir). Gerçek
    #: istek oturumu zaten tazelediği için probe kotayı boşuna harcamaz. İki
    #: hesapta her hesabın sayacı ayrıdır.
    upstream_keepalive_skip_when_active: bool = True

    # ---------------------------------------------------------- istek sırası
    #: Upstream'e giden tamamlamalar sıraya alınsın mı? Açıkken **hesap başına**
    #: aynı anda yalnız bir istek işlenir (dış sistem, Telegram mesajları ve probe
    #: aynı sırayı paylaşır); diğerleri geliş sırasıyla bekler. Tek hesapta bu,
    #: bütün trafiğin tek sırası demektir; iki hesapta her hesabın kendi sırası
    #: vardır ve hesaplar birbirini beklemez.
    request_queue_enabled: bool = True
    #: Bir istek bitince sıradaki, bu aralıktan rastgele seçilen süre (saniye)
    #: kadar bekletilir. Yalnız upstream'e gerçekten gidilmiş isteklerden sonra
    #: uygulanır; yerelde reddedilen istek sırayı bekletmez.
    request_gap_min_seconds: float = 15.0
    request_gap_max_seconds: float = 30.0
    #: Bir hesabın sırasında aynı anda bekleyebilecek en fazla istek (0 =
    #: sınırsız). Dolunca yeni istek beklemeden reddedilir (`queue_full`).
    request_queue_max_waiting: int = 20
    #: Emniyet supabı: bir istek sırayı bundan uzun tutarsa (sızmış slot) sıra
    #: kendini kurtarır ve bir sonrakine geçer.
    request_queue_max_hold_seconds: float = 1800.0

    # ----------------------------------------------- /v1/ask (otomatik sistem)
    #: İlk "bekle" işaretinin gecikmesi (saniye). Cevap bu sürede gelirse hiç
    #: "bekle" gönderilmez.
    ask_first_wait_seconds: float = 30.0
    #: İlk işaretten sonra "bekle" tekrarının aralığı (saniye).
    ask_wait_interval_seconds: float = 15.0
    #: **İstek başına** işlem süresi sınırı (saniye). Sıra beklemesi sayılmaz;
    #: saat, isteğin sırası gelip upstream'e gönderildiği anda başlar.
    ask_request_timeout_seconds: float = 180.0
    #: Bir isteğin sırada bekleyebileceği en uzun süre (saniye, 0 = sınırsız).
    #: Aşılırsa karşı sisteme "problem" denir ki alarm süresiz gecikmesin.
    ask_max_queue_wait_seconds: float = 600.0
    #: Ciddi bir arızadan sonra yeni isteklere bu süre boyunca **upstream'e
    #: gitmeden** "problem" denir; süre dolunca tek istek deneme olarak geçer.
    #: 0 = bekletme yok (her istek denenir).
    ask_problem_hold_seconds: float = 300.0
    #: `/v1/ask` isteğinde `model` verilmezse kullanılacak model (boş = varsayılan).
    ask_default_model: str = ""
    #: **Yalnız `/v1/ask` için** geçerli ek anahtarlar (virgülle ayrılmış). Karşı
    #: sisteme buradan anahtar verin: o anahtar sohbet ve yönetim (`/v1/admin/*`)
    #: uçlarında geçersizdir. `API_KEYS` içindeki anahtarlar `/v1/ask`'te de
    #: çalışmaya devam eder. Boş = ek anahtar yok.
    ask_api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---------------------------------------------------------------- alerts
    #: Telegram üzerinden arıza bildirimi. Bot token'ı ve bildirim sohbeti
    #: (TELEGRAM_ALERT_CHAT_ID veya ilk allowlist kaydı) yoksa sessizce kapalıdır.
    telegram_alerts_enabled: bool = True
    #: Aynı arıza kodu bu süre boyunca **tekrar** bildirilmez (saniye).
    #: Durum değişimleri (yeni kod / düzelme) bu sınırı beklemez; bu yalnızca
    #: "aynı arıza sürüyor" hatırlatmasını seyreltir. 0 = her probe'da bildir.
    telegram_alert_repeat_seconds: float = 21600.0

    # ----------------------------------------------------------- health/docs
    #: `/health` çıktısında upstream host'u görünsün mü? Varsayılan kapalı:
    #: uç nokta kimlik doğrulamasızdır ve sağlayıcı adresini sızdırmamalıdır.
    health_expose_upstream: bool = False
    #: `/metrics` için API anahtarı istensin mi? Varsayılan kapalı (mevcut
    #: kurulumları bozmamak için); herkese açık ortamda açılması önerilir.
    metrics_require_auth: bool = False

    # ----------------------------------------------------------------- pools
    max_connections: int = 200
    max_keepalive_connections: int = 50
    #: Upstream'e aynı anda gidebilecek istek sayısı.
    max_concurrent_upstream: int = 32

    # ----------------------------------------------------------------- retry
    retry_max_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0

    # ------------------------------------------------------- circuit breaker
    breaker_enabled: bool = True
    breaker_failure_threshold: int = 5
    breaker_reset_timeout: float = 30.0

    # ------------------------------------------------------------ rate limit
    rate_limit_enabled: bool = True
    #: Dakikadaki istek sayısı (anahtar başına).
    rate_limit_rpm: int = 120
    #: Ani yük için kova kapasitesi.
    rate_limit_burst: int = 30

    # ----------------------------------------------------------------- input
    max_body_bytes: int = 4 * 1024 * 1024
    max_messages: int = 400
    max_prompt_chars: int = 500_000
    unsupported_params: UnsupportedParamPolicy = "ignore"

    # ------------------------------------------------------------- recaptcha
    recaptcha_provider: RecaptchaProviderName = "static"
    recaptcha_static_token: str = ""
    recaptcha_token_ttl: float = 100.0
    #: external sağlayıcı için: token döndüren HTTP uç noktası.
    recaptcha_external_url: str = ""
    recaptcha_external_api_key: str = ""
    recaptcha_external_timeout: float = 60.0
    #: browser (Playwright) sağlayıcı için. Enterprise hedeflerde hem betik
    #: hem de `grecaptcha.enterprise.execute()` API'si kullanılır.
    recaptcha_site_key: str = ""
    recaptcha_version: RecaptchaVersion = "v3"
    recaptcha_action: str = "chat"
    recaptcha_browser_timeout: float = 45.0

    # --------------------------------------------------------------- session
    session_ttl: float = 1800.0
    session_cache_size: int = 1024
    #: True ise her istek için yeni chat_id üretilir (durumsuz mod).
    session_stateless: bool = True
    #: True ise istemci `conversation_id` göndermese bile aynı istemci (API anahtarı)
    #: için upstream sohbeti yeniden kullanılır; `session_stateless` bu modda yok
    #: sayılır. OpenAI-uyumlu istemciler (Continue, Cursor, OpenAI SDK) şema dışı
    #: `conversation_id` alanını gönderemediği için tek sohbetten ilerlemenin yolu bu.
    session_reuse: bool = False
    #: Bir upstream sohbeti en fazla bu kadar istek taşısın; sonra yeni sohbet açılır.
    #: 0 = mesaj sayısı sınırı yok (yalnız `session_ttl` geçerli).
    session_rotate_after_messages: int = 35
    #: Bir upstream sohbeti en fazla bu kadar saniye kullanılsın. 0 = yaş sınırı yok.
    session_rotate_after_seconds: float = 0.0

    # --------------------------------------------------------------- models
    models_file: str = "config/models.yaml"

    # -------------------------------------------------------------- metrics
    metrics_enabled: bool = True

    # ------------------------------------------------------------- autopilot
    #: Otopilot koşu kayıtlarının (fikir, hafıza, tur istatistikleri — secret
    #: içermez) diske yazılacağı klasör. Boşsa kayıtlar yalnız bellekte tutulur
    #: ve süreç yeniden başlayınca kaybolur. Render Free'de restart olağan
    #: olduğu için kalıcı disk bağlandıysa burası verilmelidir; yarıda kalan
    #: koşular `stopped` olarak geri yüklenir ve Telegram'da `/devam` ile aynı
    #: Agent sohbetinde sürdürülebilir (`app/autopilot/manager.py`).
    autopilot_state_dir: str = ""

    # ----------------------------------------------------------- validators
    @field_validator(
        "api_keys",
        "ask_api_keys",
        "cors_origins",
        "upstream_token_cookie_names",
        "upstream_token_cookie_names_2",
        "upstream_token_cookie_names_3",
        "upstream_token_cookie_names_4",
        "upstream_limit_markers",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Virgülle ayrılmış listeyi (veya JSON dizisini) listeye çevirir."""
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("[") and text.endswith("]"):
                import json

                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in text.split(",") if item.strip()]
        return v

    @field_validator(
        "target_domain",
        "target_domain_2",
        "target_domain_3",
        "target_domain_4",
        mode="before",
    )
    @classmethod
    def _strip_scheme(cls, v: object) -> object:
        """Şema, tırnak, sondaki slash ve yol kısmını temizler."""
        if isinstance(v, str):
            v = v.strip().strip('"').strip("'").strip()
            for prefix in ("https://", "http://"):
                if v.startswith(prefix):
                    v = v[len(prefix) :]
            v = v.rstrip("/")
            # Yanlislikla tam URL yapistirilmissa yol kismini at.
            if "/" in v:
                v = v.split("/", 1)[0]
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    # ------------------------------------------------------------ computed
    @property
    def base_url(self) -> str:
        return f"{self.upstream_scheme}://{self.target_domain}"

    @property
    def origin(self) -> str:
        return self.base_url

    @staticmethod
    def _join_url(base: str, path: str, chat_id: str) -> str:
        """Yolu birleştirir; `{chat_id}` varsa yerleştirir, yoksa yolu olduğu gibi bırakır.

        `.format()` kullanılmaz: yoldaki başka `{...}` süslü parantezleri KeyError
        üretmesin ve yer tutucusu olmayan uçlar (create-evaluation) bozulmasın.
        """
        path = (path or "/").strip() or "/"
        if not path.startswith("/") and not path.startswith("?"):
            path = "/" + path
        if "{chat_id}" in path:
            path = path.replace("{chat_id}", chat_id)
        return f"{base}{path}"

    def stream_url(self, chat_id: str) -> str:
        return self._join_url(self.base_url, self.upstream_stream_path, chat_id)

    def referer_url(self, chat_id: str) -> str:
        return self._join_url(self.base_url, self.upstream_referer_path, chat_id)

    # ------------------------------------------- kalıcı tarayıcı girişi
    #: Sunucudaki Chromium siteye mail+şifre ile girip çerezi tazelesin mi?
    #: **Kapalı kalmalı.** Bu hedefte işe yaramadığı ölçüldü: e-posta + şifre
    #: oturumunun istekleri `recaptcha_rejected` alıyor, çalışan oturumlar Google
    #: girişiyle alınıyor (docs/LESSONS §15.1) ve sunucunun girişi çalışan oturumla
    #: çakışır (§1). "Oturum ~3 saatte düşer" varsayımı da ölçümle çürütüldü
    #: (3-28+ saat; docs/ROLLBACK §12.2). Kod kayıt değeriyle duruyor.
    upstream_login_enabled: bool = False
    #: Siteye giriş e-postası (secret değil).
    upstream_login_email: str = ""
    #: Site giriş şifresi. Yalnız env'den okunur, loglanmaz.
    upstream_login_password: str = ""
    #: Giriş ekranının yolu (ör. `/login`). Boşsa hedef kökü kullanılır.
    upstream_login_path: str = ""
    #: Çerez kontrol/tazeleme aralığı (saniye).
    upstream_login_interval_seconds: float = 600.0
    #: Kalıcı Chromium profil dizini (Render'da disk kalıcı değil, boş bırakın).
    upstream_browser_profile_dir: str = ""

    def login_url(self) -> str:
        """Giriş ekranının tam adresi (``UPSTREAM_LOGIN_PATH`` boşsa kök)."""
        path = (self.upstream_login_path or "").strip()
        if not path:
            return self.base_url
        return self._join_url(self.base_url, path, "")

    def parsed_extra_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.upstream_extra_headers.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                out[key] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Süreç ömrü boyunca tek Settings örneği."""
    return Settings()
