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
    #: Gövdeye yazılacak `mode` alanı (örn. "direct"). Boşsa alan gönderilmez.
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

    # --------------------------------------------------------------- quota
    #: Upstream'in hesap kısıtlamasını bildiren metin işaretleri (virgülle ayrılmış).
    #: İşaretler **ayırt edici ifadeler** olmalı: "rate limit" gibi genel sözler bir
    #: modelin meşru cevabında da geçebileceği için düz metin taramasında yanlış
    #: alarm üretir. Hedef servisin gerçek mesajını gözlemlediğinizde buraya birebir
    #: yazın (örn. "upstream limit reached").
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
    #: Bu **yumuşak** bir bütçedir: hangi hesabın tercih edileceğini belirler,
    #: isteği engellemez. Gerçek sınır öğrenilince (`_learned_limit`) bu değer
    #: yalnızca başlangıç tahmini olarak kalır.
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

    # -------------------------------------------------------------- timeouts
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    write_timeout: float = 30.0
    pool_timeout: float = 10.0
    #: Tek bir upstream isteği için toplam üst sınır (saniye).
    total_request_timeout: float = 600.0
    #: İki token arasında izin verilen en uzun sessizlik.
    stream_idle_timeout: float = 120.0

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

    # ----------------------------------------------------------- validators
    @field_validator(
        "api_keys",
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
