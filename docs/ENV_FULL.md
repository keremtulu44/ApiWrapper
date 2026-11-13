# Ortam değişkenlerinin tam listesi

> Bu dosya **üretilir**: `python -m scripts.env_template`. Elle düzenleme;
> `tests/test_env_template.py` sürüklenmeyi yakalar. Liste alfabetiktir ve
> `Settings` + `TelegramSettings` alanlarının tamamını içerir.

İşaretler:

| İşaret | Anlamı |
|---|---|
| ✅ | Bu kurulumda yazılması gereken bir değer var |
| ⬜ | Satır boş bırakılmalı (dolu olması bir şeyi bozar ya da gereksiz) |
| ⛔ | Hiç yazılmamalı — istek/oturum başına üretilir, sabitlenirse 401/403/409 |
| ↺ | Varsayılan doğru; dokunma |

Gizli değer yazan satırlar yoktur: buraya kendi çerezini/token'ını yapıştırma,
o değerler `/v1/admin/auth` (cookie/curl) veya `UPSTREAM_CURL` üzerinden girer.

| Değişken | Ne yazacaksın | Grup |
|---|---|---|
| `ACCOUNT_BUDGET_GROWTH_STREAK` | ↺ `3` — üst üste başarıdan sonra bütçe artışı. | kota |
| `ACCOUNT_COOLDOWN_SECONDS` | ↺ `1200` — kilit yiyen hesabın dinlenme süresi. | kota |
| `ACCOUNT_MAX_SWITCHES` | ↺ `2` — tek istekte en fazla hesap değişimi. | kota |
| `ACCOUNT_MSG_BUDGET` | ↺ `15` — yumuşak bütçe; isteği engellemez, hesap seçimini yönlendirir. | kota |
| `ACCOUNT_QUOTA_WINDOW_SECONDS` | ↺ `1200` — bütçenin sayıldığı pencere. | kota |
| `ADMIN_API_KEYS` | ✅ Oturumu değiştiren `/v1/admin` uçları için ayrı anahtar; boş bırakırsan tüm `API_KEYS` admin sayılır. Telegram botu da bunu kullanır. | sunucu |
| `API_KEYS` | ✅ En az bir anahtar: `sk-local-dev-key`. İstemciler `Authorization: Bearer <bu>` yollar. | sunucu |
| `APP_NAME` | ↺ `ApiWrapper` — servis adı, loglarda görünür. | sunucu |
| `APP_VERSION` | ↺ `1.0.0` — sürüm etiketi. | sunucu |
| `AUTH_DISABLED` | ↺ `false` — `true` yapmak API'yi kimliksiz bırakır. | sunucu |
| `BREAKER_ENABLED` | ↺ `true`. | dayanıklılık |
| `BREAKER_FAILURE_THRESHOLD` | ↺ `5` üst üste hata. | dayanıklılık |
| `BREAKER_RESET_TIMEOUT` | ↺ `30` sn. | dayanıklılık |
| `CONNECT_TIMEOUT` | ↺ `10` sn. | dayanıklılık |
| `CORS_ALLOW_CREDENTIALS` | ↺ `false` — çerezle tarayıcı çağrısı yapmayacaksan. | sunucu |
| `CORS_ORIGINS` | ↺ `*`; tarayıcıdan çağrı yoksa da `*` zararsız. | sunucu |
| `DEBUG` | ↺ `false`. Açarsan hata gövdeleri istemciye ayrıntı sızdırır. | sunucu |
| `DEPLOYMENT_ID_AUTO_RETRY` | ↺ `true` — 409'da damga tazelenip istek bir kez tekrarlanır. | damga |
| `DEPLOYMENT_ID_REFRESH_ENABLED` | ↺ `true` — kanarya damgayı upstream sayfasından okur. | damga |
| `DEPLOYMENT_ID_TTL_SECONDS` | ↺ `1800`. DİKKAT: adında `UPSTREAM_` öneki **yok**. | damga |
| `DEPLOYMENT_PROBE_PATH` | ↺ `/` — damganın okunduğu HTML sayfası. | damga |
| `DEPLOYMENT_PROBE_TIMEOUT` | ↺ `8` saniye. | damga |
| `DEPLOYMENT_STAMP_STRIP_ON_CONFLICT` | ↺ `true` — damga bulunamazsa bayat başlıklar atılıp bir kez denenir. | damga |
| `HEALTH_EXPOSE_UPSTREAM` | ↺ `false` — `/health` upstream hostunu gizler. | gözlem |
| `HOST` | ↺ `0.0.0.0` — konteynerde/Render'da böyle kalmalı. | sunucu |
| `LOG_JSON` | ↺ `true` — Render log panelinde alan adlarıyla görünür. | sunucu |
| `LOG_LEVEL` | ↺ `INFO`. Teşhis sırasında `DEBUG`. | sunucu |
| `MAX_BODY_BYTES` | ↺ `4194304` (4 MB). | dayanıklılık |
| `MAX_CONCURRENT_UPSTREAM` | ↺ `32` — eşzamanlı upstream isteği. | dayanıklılık |
| `MAX_CONNECTIONS` | ↺ `200`. | dayanıklılık |
| `MAX_KEEPALIVE_CONNECTIONS` | ↺ `50`. | dayanıklılık |
| `MAX_MESSAGES` | ↺ `400`. | dayanıklılık |
| `MAX_PROMPT_CHARS` | ↺ `500000`. | dayanıklılık |
| `METRICS_ENABLED` | ↺ `true`. | gözlem |
| `METRICS_REQUIRE_AUTH` | ↺ `false`; herkese açık adreste `true`. | gözlem |
| `MODELS_FILE` | ↺ `config/models.yaml` — `id` ↔ `modelAId` eşlemesi. | oturum |
| `POOL_TIMEOUT` | ↺ `10` sn — bağlantı havuzu beklemesi. | dayanıklılık |
| `PORT` | ↺ `8000`. Render `PORT`'u kendi verir, yerelde 8000. | sunucu |
| `QUOTA_TEXT_SCAN_CHARS` | ↺ `0` = düz metin taraması kapalı; kısıtlama düz metin geliyorsa `300`. | kota |
| `RATE_LIMIT_BURST` | ↺ `30`. | dayanıklılık |
| `RATE_LIMIT_ENABLED` | ↺ `true`. | dayanıklılık |
| `RATE_LIMIT_RPM` | ↺ `120` istek/dakika. | dayanıklılık |
| `READ_TIMEOUT` | ↺ `300` sn — akış için üst sınır. | dayanıklılık |
| `RECAPTCHA_ACTION` | ✅ `chat_submit` — token'daki action bu olmalı. | captcha |
| `RECAPTCHA_BROWSER_TIMEOUT` | ↺ `45` sn — tarayıcıda token üretme süresi. | captcha |
| `RECAPTCHA_EXTERNAL_API_KEY` | ⬜ Yalnız `external` sağlayıcısı için. | captcha |
| `RECAPTCHA_EXTERNAL_TIMEOUT` | ↺ `60` sn — `external` için. | captcha |
| `RECAPTCHA_EXTERNAL_URL` | ⬜ Yalnız `external` sağlayıcısı için. | captcha |
| `RECAPTCHA_PROVIDER` | ✅ `browser` — Enterprise token'ını gerçek tarayıcıda üretir. `static`/`noop` sabit token ister. | captcha |
| `RECAPTCHA_SITE_KEY` | ✅ `RECAPTCHA_SITE_KEY` = gözlenen sitedeki public site key (gizli değil, sayfada açık). | captcha |
| `RECAPTCHA_STATIC_TOKEN` | ⛔ Boş bırak. Tek kullanımlık ve saniyeler içinde ölür. | captcha |
| `RECAPTCHA_STATIC_TOKEN_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | captcha |
| `RECAPTCHA_STATIC_TOKEN_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | captcha |
| `RECAPTCHA_STATIC_TOKEN_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | captcha |
| `RECAPTCHA_TOKEN_TTL` | ↺ `100` sn — üretilen token'ın kullanım ömrü. | captcha |
| `RECAPTCHA_VERSION` | ✅ `enterprise` — gözlenen hedef Enterprise ve `chat_submit` kullanıyor. | captcha |
| `RENDER_API_KEY` | ⬜ Yalnız Render'da restart sonrası oturum korunsun istiyorsan yaz. | yenileme |
| `RENDER_SERVICE_ID` | ⬜ Render kendisi verir; elle yazma. | yenileme |
| `RETRY_BASE_DELAY` | ↺ `0.5` sn. | dayanıklılık |
| `RETRY_MAX_ATTEMPTS` | ↺ `3`. | dayanıklılık |
| `RETRY_MAX_DELAY` | ↺ `8` sn. | dayanıklılık |
| `ROOT_PATH` | ⬜ Ters proxy alt yolda sunmuyorsa boş. | sunucu |
| `SESSION_CACHE_SIZE` | ↺ `1024`. Tek worker ile çalıştır; sayaç worker'lar arasında paylaşılmaz. | oturum |
| `SESSION_REUSE` | ↺ `false`. `true` yaparsan sohbet API anahtarına bağlanır ve tek upstream sohbetinden ilerlenir. | oturum |
| `SESSION_ROTATE_AFTER_MESSAGES` | ↺ `35` — sohbet bu sayıdan sonra yenilenir (`0` = sınırsız). | oturum |
| `SESSION_ROTATE_AFTER_SECONDS` | ↺ `0` = yaş sınırı yok; kota penceresine hizalamak için `1200`. | oturum |
| `SESSION_STATELESS` | ↺ `true` — istemci `conversation_id` taşımıyorsa. | oturum |
| `SESSION_TTL` | ↺ `1800` sn. | oturum |
| `STREAM_IDLE_TIMEOUT` | ↺ `120` sn — bu süre veri gelmezse akış düşer. | dayanıklılık |
| `TARGET_DOMAIN` | ✅ Gerçek host, **şemasız** (`ornek-servis.com`). Boş kalırsa `/readyz` 503 `target_domain_not_configured`. | kimlik |
| `TARGET_DOMAIN_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `TARGET_DOMAIN_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `TARGET_DOMAIN_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `TELEGRAM_ALERTS_ENABLED` | ↺ `true` — arıza ve düzelme bildirimi. | telegram |
| `TELEGRAM_ALERT_CHAT_ID` | ⬜ Boşsa allowlist'in ilk kaydı arıza bildirimi hedefi olur. | telegram |
| `TELEGRAM_ALERT_REPEAT_SECONDS` | ↺ `21600` — aynı arıza sürerken hatırlatma aralığı. | telegram |
| `TELEGRAM_ALLOWED_CHAT_IDS` | ⬜ İlk `/start` chat ID'yi gösterir; sonra buraya yaz. | telegram |
| `TELEGRAM_BOT_TOKEN` | ⬜ Bot kullanmayacaksan boş; doluysa bot aynı servis içinde başlar. | telegram |
| `TELEGRAM_MAX_REPLY_CHARS` | ↺ `12000` — Telegram mesaj bölme sınırı. | telegram |
| `TELEGRAM_MODEL` | ↺ `max` — wrapper üzerinden çağrılacak model adı. | telegram |
| `TELEGRAM_POLL_TIMEOUT` | ↺ `30` sn. | telegram |
| `TELEGRAM_REQUEST_TIMEOUT` | ↺ `180` sn. | telegram |
| `TOTAL_REQUEST_TIMEOUT` | ↺ `600` sn. | dayanıklılık |
| `UNSUPPORTED_PARAMS` | ↺ `ignore`; `error` yaparsan şema dışı alan 400 döner. | dayanıklılık |
| `UPSTREAM_ACCEPT_LANGUAGE` | ✅ Yakalandığı cihazınkiyle birebir: `tr,en;q=0.9,en-GB;q=0.8,en-US;q=0.7`. | upstream |
| `UPSTREAM_ACCEPT_LANGUAGE_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_ACCEPT_LANGUAGE_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_ACCEPT_LANGUAGE_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_ACCESS_TOKEN` | ⬜ Boş bırak. Yer tutucu (`APIWRAPPER_EMPTY_VALUE_V1`) yazarsan yüklenirken boşaltılır ve 401 sürer. | kimlik |
| `UPSTREAM_ACCESS_TOKEN_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_ACCESS_TOKEN_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_ACCESS_TOKEN_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_ACCOUNT_2_NAME` | ⬜ Boş bırak. | upstream |
| `UPSTREAM_ACCOUNT_3_NAME` | ⬜ Boş bırak. | upstream |
| `UPSTREAM_ACCOUNT_4_NAME` | ⬜ Boş bırak. | upstream |
| `UPSTREAM_ACCOUNT_NAME` | ↺ `hesap-1` — loglarda hangi hesabın kullanıldığını gösterir. | upstream |
| `UPSTREAM_AUTH_FROM_COOKIE` | ✅ Tarayıcı isteğinde `authorization` başlığı **yoksa** `false`; 401 alıyorsan `true` (token çerezden ayıklanır). | kimlik |
| `UPSTREAM_AUTH_FROM_COOKIE_2` | ↺ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `False`. | kimlik |
| `UPSTREAM_AUTH_FROM_COOKIE_3` | ↺ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `False`. | kimlik |
| `UPSTREAM_AUTH_FROM_COOKIE_4` | ↺ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `False`. | kimlik |
| `UPSTREAM_AUTH_SCHEME` | ✅ `Bearer` — tarayıcı başlığında şema yoksa boş bırak. | kimlik |
| `UPSTREAM_AUTH_SCHEME_2` | ↺ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `Bearer`. | kimlik |
| `UPSTREAM_AUTH_SCHEME_3` | ↺ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `Bearer`. | kimlik |
| `UPSTREAM_AUTH_SCHEME_4` | ↺ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `Bearer`. | kimlik |
| `UPSTREAM_CAPTURED_MODEL_ID` | ⬜ cURL içe aktarılınca kendisi dolar (`modelAId` UUID). Elle yazma. | upstream |
| `UPSTREAM_COOKIE` | ⬜ `UPSTREAM_CURL` kullanıyorsan boş. Yalnız çerezle gideceksen **tam** `Cookie` başlığı, parçalı `.0/.1` dahil. | kimlik |
| `UPSTREAM_COOKIE_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_COOKIE_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_COOKIE_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | kimlik |
| `UPSTREAM_CURL` | ✅ Tam `create-evaluation` cURL'ü. **Tek kaynak bu olsun**; doldurursan `UPSTREAM_COOKIE`'yi boş bırak (açılışta cURL çerezi ezer). | upstream |
| `UPSTREAM_CX` | ⛔ Boş bırak. İstek başına üretilir; sabit değer ikinci sohbette reddedilir. | upstream |
| `UPSTREAM_CX_SEND` | ✅ `false` — canlı gözlemde bu alan olmadan da istek geçti. | upstream |
| `UPSTREAM_EXTRA_HEADERS` | ⬜ `dnt=1` yeterli. `x-deployment-id` / `x-arena-web-deployment` **buraya yazma** (her deploy'da değişir). | upstream |
| `UPSTREAM_HTTP2` | ↺ `true` — tarayıcı parmak izine uyar. | upstream |
| `UPSTREAM_KEEPALIVE_INITIAL_DELAY_SECONDS` | ↺ `60` — deploy sonrası ilk probe. | keepalive |
| `UPSTREAM_KEEPALIVE_INTERVAL_SECONDS` | ↺ `0` = kapalı. `10800` = üç saatte bir kısa gerçek completion (kota ve captcha harcar). | keepalive |
| `UPSTREAM_KEEPALIVE_JITTER_SECONDS` | ↺ `300` — tam saatte atılan istek desenini kırar. | keepalive |
| `UPSTREAM_KEEPALIVE_MODEL` | ↺ `max` — probe'un kullanacağı model. | keepalive |
| `UPSTREAM_KEEPALIVE_STREAM_SMOKE_INTERVAL_SECONDS` | ↺ `86400` — günde bir gerçek akış yolu denemesi; `0` = kapalı. | keepalive |
| `UPSTREAM_LIMIT_MARKERS` | ↺ Örnekteki virgüllü liste. Ayırt edici ibare seç; `rate limit` gibi genel söz yanlış alarm üretir. | upstream |
| `UPSTREAM_MODE` | ✅ `direct-battle` — gövdedeki `mode` alanı; cURL'den birebir. | upstream |
| `UPSTREAM_PROXY` | ⬜ Proxy yoksa boş; varsa `http://kullanici:sifre@host:port`. | upstream |
| `UPSTREAM_RECAPTCHA_FIELD` | ✅ `recaptchaV3Token` (v2 görürsen `recaptchaV2Token`) — gövdedeki captcha alan adı. | upstream |
| `UPSTREAM_REFERER_PATH` | ✅ `/text/direct` — cURL'ün `referer` yolundan birebir. | upstream |
| `UPSTREAM_REFERER_PATH_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_REFERER_PATH_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_REFERER_PATH_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_REFRESH_API_KEY` | ⬜ Upstream `apikey` istiyorsa; boşsa siteden otomatik keşfedilir. | yenileme |
| `UPSTREAM_REFRESH_CHECK_INTERVAL_SECONDS` | ↺ `60` sn. | yenileme |
| `UPSTREAM_REFRESH_ENABLED` | ↺ `true` — erişim token'ı kendiliğinden yenilenir. | yenileme |
| `UPSTREAM_REFRESH_LEEWAY_SECONDS` | ↺ `600`. JWT ömrünün yarısından büyük olursa her istekte yenileme tetiklenir; `/v1/admin/auth` bunu uyarır. | yenileme |
| `UPSTREAM_REFRESH_TOKEN` | ⬜ Çerezde refresh token varsa boş bırak. Yenilemede dönen yeni değer bellekte tutulur. | yenileme |
| `UPSTREAM_REFRESH_URL` | ⬜ Boş → çerezdeki JWT'nin `iss` alanından türetilir. Yenileme 401 verirse doldur. | yenileme |
| `UPSTREAM_SCHEME` | ↺ `https`; yalnız yerel taklit sunucu için `http`. | upstream |
| `UPSTREAM_SNAPSHOT_VERSION` | ✅ `1` — kimlik + tarayıcı izi dahil tam snapshot. | upstream |
| `UPSTREAM_STATE_FILE` | ✅ `.cache/upstream_state.json` — dönen token ve damga restart'ta buradan geri yüklenir. | upstream |
| `UPSTREAM_STREAM_PATH` | ✅ Yakalanan cURL'ün yolu: `/nextjs-api/stream/create-evaluation`. Yolu uydurma; 404 alırsın. | upstream |
| `UPSTREAM_TOKEN_COOKIE_NAMES` | ⬜ Çerez adını biliyorsan yaz (`arena-auth-prod-v1` gibi); bilmiyorsan boş → sezgisel arama. | upstream |
| `UPSTREAM_TOKEN_COOKIE_NAMES_2` | ↺ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `PydanticUndefined`. | upstream |
| `UPSTREAM_TOKEN_COOKIE_NAMES_3` | ↺ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `PydanticUndefined`. | upstream |
| `UPSTREAM_TOKEN_COOKIE_NAMES_4` | ↺ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. Varsayılan `PydanticUndefined`. | upstream |
| `UPSTREAM_USER_AGENT` | ✅ cURL'deki UA ile birebir; sürüm numarası dahil (`Edg/154` gibi). `sec-ch-ua` bundan türetilir. | upstream |
| `UPSTREAM_USER_AGENT_2` | ⬜ 2. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_USER_AGENT_3` | ⬜ 3. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_USER_AGENT_4` | ⬜ 4. hesap yuvası. Kullanmıyorsan boş bırak; boş yuva hesap havuzuna girmez. | upstream |
| `UPSTREAM_VERIFY_TLS` | ↺ `true` — kapatmak MITM'e açık kapı bırakır. | upstream |
| `UPSTREAM_WARN_ON_EXPIRED_TOKEN` | ↺ `true` — süresi dolmuş JWT'yi loglar, isteği yine dener. | upstream |
| `WRAPPER_API_KEY` | ⬜ Aynı serviste çalışıyorsa yazma; `API_KEYS` ilk değerinden türetilir. | telegram |
| `WRAPPER_BASE_URL` | ⬜ Aynı serviste çalışıyorsa **yazma**; varsayılan yerel adres kullanılır. | telegram |
| `WRITE_TIMEOUT` | ↺ `30` sn. | dayanıklılık |

Toplam **150** anahtar: 19 yazılacak, 43 boş, 88 varsayılan/yazma.
Denetim için: `python -m scripts.env_audit .env`.
