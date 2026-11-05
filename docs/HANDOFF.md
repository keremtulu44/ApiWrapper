# Oturum devir dosyası (HANDOFF)

Bu dosya, yeni bir oturumun (veya başka bir modelin) depoyu **kendi başına**
anlaması için gereken dosya yollarını ve satır konumlarını listeler.

Kurallar:
- Satır numaraları **`arena/cd32810d-apiwrapper` / `635d502`** anına aittir.
- Kod değiştikçe satırlar kayar; arama için fonksiyon/sabit adları verilmiştir.
- Bu dosyada yorum, tavsiye veya yönlendirme yoktur; yalnız konum ve olgu vardır.

---

## 1. Dal ve depo durumu

| Alan | Değer |
|---|---|
| Çalışma dalı | `arena/cd32810d-apiwrapper` |
| HEAD | `635d502` |
| Merge hedefi (zorunlu) | `arena/c55e601b-apiwrapper` |
| Merge hedefi son commit | `8e1bec9` |
| `main` | `df2577b` — dokunulmaz |
| Açık PR | **#28** `Stop reporting generic 403s as CAPTCHA rejections`, MERGEABLE |
| Açık PR değişen dosya | 9 dosya, +129 / −25 |

Merge kuralları dosyası: **`docs/MERGE_TARGET.md`** (82 satır).
Kritik kural: merge = Render deploy, kullanıcı o turda açıkça onaylamadan merge yapılmaz (§"Onay kuralı").

---

## 2. Okunması gereken belgeler (öncelik sırası)

| # | Dosya | Satır | İçerik |
|---|---|---|---|
| 1 | `README.md` | 586 | Genel bakış; §27 "Hızlı Belge ve Operasyon Rehberi"; §380 "reCAPTCHA sağlayıcıları" (`### Render / Docker` 403); §282 "UPSTREAM_CURL"; §94 kimlik doğrulama; §165 cURL ayarlama |
| 2 | `docs/MERGE_TARGET.md` | 82 | Merge/onay kuralları ve merge geçmişi tablosu |
| 3 | `docs/BIG_PICTURE.md` | 113 | Üç ömür katmanı: oturum kimliği / türev değerler / telemetri |
| 4 | `docs/COOKIE_LIFECYCLE.md` | 152 | Çerez ömrü; §0 üç katman tablosu; §2 "Token yok" ne demek; §5 refresh token |
| 5 | `docs/TROUBLESHOOTING.md` | 330 | §0 hızlı teşhis; §1 409; §1b 401 User not found; §4 CAPTCHA; §141-147 `cx` notu |
| 6 | `docs/RENDER.md` | 347 | §2 bir istek nasıl yürür; §5 belirti→aşama; §7 `cx` notu (satır 265-270); §8 JSON logları |
| 7 | `docs/ARCHITECTURE.md` | 224 | §3 "Dosya Mimarisi" (satır 106-153); §4 reCAPTCHA stratejisi (satır 154-170) |
| 8 | `docs/MOBILE_CURL.md` | 202 | Telefondan çerez/cURL alma sınırları |
| 9 | `docs/AUDIT_2026_11.md` | 84 | Denetim kaydı |
| 10 | `docs/DELIVERY_REPORT.md` | 1219 | Uzun teslimat raporu |

---

## 3. Uygulama giriş noktası

| Dosya | Satır | Öğe |
|---|---|---|
| `app/main.py` | 119 | `lifespan()` — tüm servislerin kurulumu |
| `app/main.py` | 231 | `create_app()` |
| `app/main.py` | 316 | `app.include_router(health.router)` |
| `app/core/config.py` | 27 | `class Settings` |
| `app/core/config.py` | 447 | `get_settings()` |

### HTTP uçları
| Dosya | Satır | Uç |
|---|---|---|
| `app/api/v1/chat.py` | 23 | `POST` sohbet tamamlama |
| `app/api/v1/chat.py` | 56 | `POST` text completion |
| `app/api/v1/health.py` | 25 | `/health` |
| `app/api/v1/health.py` | 108 | `/healthz` |
| `app/api/v1/health.py` | 114 | `/readyz` |
| `app/api/v1/health.py` | 130 | `/metrics` |
| `app/api/v1/admin.py` | 94 | `POST /recaptcha/invalidate` |
| `app/api/v1/admin.py` | 327 | `POST /upstream/curl` (decorator 326) |
| `app/api/v1/admin.py` | 498 | `GET /config` |
| `app/api/v1/admin.py` | 552 | `GET /auth` — `/tani` bunu okur |
| `app/api/v1/admin.py` | 636-673 | `recaptcha` bloğu (`provider`, `site_key`, `version`, `action`, `field`, `production`, `origin`, `hint`) |

---

## 4. cURL ayrıştırma zinciri

`UPSTREAM_CURL` → `apply_upstream_curl()` → `apply_curl_to_settings()` → `build_settings()` → `parse_curl()`

| Dosya | Satır | Fonksiyon / sabit |
|---|---|---|
| `app/upstream/curl_runtime.py` | 22 (`_CURL_FIELDS`) | env adı → Settings alanı eşlemesi |
| `app/upstream/curl_runtime.py` | 40 | `_runtime_fields()` — yazılabilir alan allowlist'i |
| `app/upstream/curl_runtime.py` | 70 | `apply_curl_to_settings()` |
| `app/upstream/curl_runtime.py` | 131 | `apply_upstream_curl()` |
| `app/upstream/curl_import.py` | 19 | `normalize_curl()` |
| `app/upstream/curl_import.py` | 32 | `_split_top_level()` |
| `app/upstream/curl_import.py` | 81 | `parse_curl()` — `--url`, `-b`, `-H`, `--data-raw` |
| `app/upstream/curl_import.py` | 110 | `load_body()` |
| `app/upstream/curl_import.py` | 120 | `CAPTCHA_FIELDS` |
| `app/upstream/curl_import.py` | 134 | `PASSTHROUGH_HEADERS` |
| `app/upstream/curl_import.py` | 155 | `_RECAPTCHA_SITE_KEY_RE` = `\b6L[A-Za-z0-9_-]{30,}\b` |
| `app/upstream/curl_import.py` | 202 | `extract_captcha()` |
| `app/upstream/curl_import.py` | 215 | `extract_authorization()` |
| `app/upstream/curl_import.py` | 245 | `build_settings()` (captcha dalı: 406-432; `cx`: 443-445; site key: 452-457) |
| `app/upstream/curl_import.py` | 406-432 | `extract_captcha()` çağrısı, sağlayıcı kararı ve uyarı metni |
| `app/upstream/curl_import.py` | 474 | `looks_like_stream_path()` |

---

## 5. Kimlik / token okuma

| Dosya | Satır | Fonksiyon |
|---|---|---|
| `app/upstream/auth.py` | 100 | `NEVER_TOKEN_COOKIES` (`cf_clearance`, `__cf_bm`, `_ga*` …) |
| `app/upstream/auth.py` | 122 | `parse_cookie_header()` |
| `app/upstream/auth.py` | 144 | `merge_chunked_cookies()` — `.0`/`.1` birleştirme |
| `app/upstream/auth.py` | 173 | `_session_cookie_is_richer()` |
| `app/upstream/auth.py` | 216 | `token_expiry()` |
| `app/upstream/auth.py` | 232 | `is_token_expired()` |
| `app/upstream/auth.py` | 442 | `extract_refresh_token()` |
| `app/upstream/auth.py` | 527 | `extract_access_token()` |
| `app/upstream/auth.py` | 596 | `resolve_bearer_token()` |

---

## 6. Upstream isteği ve hata sınıflandırma

| Dosya | Satır | Öğe |
|---|---|---|
| `app/upstream/headers.py` | 138 | `build_stream_headers()` |
| `app/upstream/headers.py` | 184 | `build_page_headers()` |
| `app/upstream/headers.py` | 35 | `_STREAM_ONLY_EXTRA_HEADERS` |
| `app/upstream/headers.py` | 29 | `DEPLOYMENT_HEADERS` |
| `app/upstream/client.py` | 49 | `RETRYABLE_STATUS` |
| `app/upstream/client.py` | 51 | `CAPTCHA_STATUS = {401, 403}` |
| `app/upstream/client.py` | 56 | `CAPTCHA_MARKERS` |
| `app/upstream/client.py` | 338 | `_cx_flag()` |
| `app/upstream/client.py` | 543 | `warmup()` |
| `app/upstream/client.py` | 578 | `_CLOUDFLARE_MARKERS` |
| `app/upstream/client.py` | 588 | `_is_cloudflare_challenge()` |
| `app/upstream/client.py` | 596 | `_is_captcha_body()` |
| `app/upstream/client.py` | 613 | `_is_quota_body()` |
| `app/upstream/client.py` | 626 | `_refresh_marker()` |
| `app/upstream/client.py` | 643 | `_is_user_missing()` |
| `app/upstream/client.py` | 668 | `_is_auth_body()` |
| `app/upstream/client.py` | 714 | `stream_completion()` |
| `app/upstream/client.py` | 729-1090 | `_stream_completion_with_client()` — hata sınıflandırma merkezi |
| `app/upstream/client.py` | 1010-1044 | sıra: auth (1010) → Cloudflare (1027) → CAPTCHA (1039) |
| `app/upstream/client.py` | 1092 | `_read_error_body()` |

### Gövde üretimi
| Dosya | Satır | Öğe |
|---|---|---|
| `app/schemas/upstream.py` | 24 | `class UpstreamPayload` |
| `app/schemas/upstream.py` | 41 | `cx` alanı |
| `app/schemas/upstream.py` | 43 | `to_wire()` — boş captcha token alanı gönderilmez |
| `app/adapters/openai_to_upstream.py` | 84 | `build_upstream_request()` |
| `app/adapters/openai_to_upstream.py` | 136 | `mode` beslemesi |
| `app/adapters/openai_to_upstream.py` | 139 | `cx` beslemesi (`upstream_cx_send` koşullu) |

---

## 7. reCAPTCHA sağlayıcıları

| Dosya | Satır | Öğe |
|---|---|---|
| `app/services/recaptcha/__init__.py` | 11 | `PROVIDERS` sözlüğü |
| `app/services/recaptcha/__init__.py` | 19 | `create_recaptcha_provider()` |
| `app/services/recaptcha/base.py` | 16 | `class RecaptchaProvider` |
| `app/services/recaptcha/base.py` | 31 | `self.stats` — teşhis sayacı |
| `app/services/recaptcha/base.py` | 51 | `get_token()` — TTL cache |
| `app/services/recaptcha/base.py` | 67 | `invalidate()` |
| `app/services/recaptcha/browser.py` | 19 | `_EXECUTE_SCRIPT` |
| `app/services/recaptcha/browser.py` | 51 | `_BOOTSTRAP_HTML` |
| `app/services/recaptcha/browser.py` | 62 | `BrowserRecaptchaProvider` |
| `app/services/recaptcha/browser.py` | 80 | `startup()` — Chromium `headless=True` |
| `app/services/recaptcha/browser.py` | 152 | `_produce_token()` |
| `app/services/recaptcha/browser.py` | 193 | `_load_page()` |
| `app/services/recaptcha/static.py` | 12 | `StaticRecaptchaProvider` |
| `app/services/recaptcha/static.py` | 42 | `NoopRecaptchaProvider` |
| `app/services/recaptcha/static.py` | 53 | `invalidate()` — saymaz |
| `app/services/recaptcha/external.py` | 17 | `ExternalRecaptchaProvider` |

### reCAPTCHA ayarları
`app/core/config.py` satır 321-334:
`recaptcha_provider` = "static" (322), `recaptcha_static_token` (323), `recaptcha_token_ttl` = 100.0 (324),
`recaptcha_external_url` (326), `recaptcha_external_api_key` (327),
`recaptcha_external_timeout` (328), `recaptcha_site_key` (331),
`recaptcha_version` = "v3" (332), `recaptcha_action` = "chat" (333),
`recaptcha_browser_timeout` = 45.0 (334).

Gövdeyle ilgili upstream alanları: `upstream_mode` (91), `upstream_recaptcha_field` (94), `upstream_cx` (88), `upstream_cx_send` (89).

---

## 8. İstek akışı ve yeniden deneme

| Dosya | Satır | Öğe |
|---|---|---|
| `app/services/completion_service.py` | 270 | `_prepare()` — token seçimi (287-300) |
| `app/services/completion_service.py` | 322 | `_events()` |
| `app/services/completion_service.py` | 349 | `while True` ana döngü |
| `app/services/completion_service.py` | 390 | her denemede `_prepare()` çağrısı |
| `app/services/completion_service.py` | 464 | `UpstreamAuthRejected` kademesi |
| `app/services/completion_service.py` | 516-524 | `RecaptchaRejected` → `invalidate()` + tek retry |
| `app/services/completion_service.py` | 526 | `create_completion()` |

---

## 9. Kalıcılık (state / Render env)

| Dosya | Satır | Öğe |
|---|---|---|
| `app/upstream/state_store.py` | 67 | `PERSISTED_FIELDS` |
| `app/upstream/state_store.py` | 89 | `PERSISTED_BOOL_FIELDS` |
| `app/upstream/state_store.py` | 144 | `save_runtime_state()` |
| `app/upstream/state_store.py` | 198 | `load_runtime_state()` |
| `app/upstream/state_store.py` | 214 | `_should_restore()` |
| `app/upstream/state_store.py` | 267 | `restore_runtime_state()` |
| `app/upstream/state_store.py` | 375-391 | `snapshot_fields` — Render env snapshot alanları |

`PERSISTED_FIELDS` içinde **olmayan** alan: `recaptcha_provider`.

---

## 10. Teşhis yüzeyi

| Dosya | Satır | Öğe |
|---|---|---|
| `app/api/v1/admin.py` | 552 | `auth_diagnostics()` |
| `app/api/v1/admin.py` | 636 | `recaptcha` bloğu |
| `app/api/v1/admin.py` | 650-660 | `production` alt bloğu (`generated`, `rejected`, `last_error`, `cached`, `token_ttl_seconds`) |
| `app/api/v1/admin.py` | 687 | `_session_cookie_candidates()` |
| `app/core/diagnostics.py` | 59 | `recaptcha_rejected` → `FailureHint` |
| `app/core/diagnostics.py` | 67 | `upstream_unauthorized` → `FailureHint` |
| `app/telegram/bot.py` | 381 | `diagnose()` — `/tani` |
| `app/telegram/bot.py` | 458-491 | CAPTCHA satırları |
| `app/telegram/bot.py` | 543 | `_diagnose_advice()` |
| `app/telegram/bot.py` | 849 | `set_upstream_curl()` — `/curl` |
| `app/services/keepalive.py` | 99 | `describe_probe()` |
| `app/services/keepalive.py` | 46 | `ProbeSnapshot` |

---

## 11. Yapılandırma dosyaları

| Dosya | Satır | İçerik |
|---|---|---|
| `render.yaml` | 43 | Blueprint. `RECAPTCHA_PROVIDER=browser` (34-35), `RECAPTCHA_SITE_KEY=6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0` (37-38), `RECAPTCHA_VERSION=enterprise` (39-40), `RECAPTCHA_ACTION=chat_submit` (41-42), `RECAPTCHA_BROWSER_TIMEOUT=45` (43-44) |
| `Dockerfile` | 65 | `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers` (9), `pip install ".[tokens,browser]"` (52), `playwright install --only-shell chromium` (53) |
| `pyproject.toml` | 57 | `[project.optional-dependencies]`: `browser` (24), `tokens` (25), `dev` (26-34); ruff/mypy/pytest ayarları |
| `config/models.yaml` | — | Model kayıt defteri (`app/services/model_registry.py` okur) |

> `render.yaml` yalnızca blueprint'tir; Render panosundaki gerçek env değerleri
> bu dosyadan farklı olabilir.

---

## 12. Testler

`tests/` — 30 dosya.

| Dosya | Kapsam |
|---|---|
| `tests/conftest.py` | `make_settings()` (36), `settings` fixture (58), `client` fixture (63), `TEST_API_KEY` |
| `tests/test_cloudflare_challenge.py` | Cloudflare vs reCAPTCHA ayrımı (24-41); generic 403 (60); boş token (70); noop red (99) |
| `tests/test_recaptcha_diagnostics.py` | `stats` sayacı (33-61) |
| `tests/test_curl_to_env.py` | `cx` yakalama/gönderme (542), boş `cx` (599); genel cURL→env |
| `tests/test_auth_token.py` | Token/çerez çıkarımı |
| `tests/test_runtime_curl_swap.py` | Canlı cURL değişimi |
| `tests/test_upstream_curl_runtime.py` | `apply_curl_to_settings()` |
| `tests/test_recaptcha.py` | Sağlayıcılar |
| `tests/test_telegram_bot.py` | `/tani`, `/curl` çıktısı |
| `tests/test_keepalive.py` | Probe |
| `tests/test_e2e_regressions.py` | Uçtan uca regresyon |

Çalıştırma:
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # 574 passed
.venv/bin/ruff check app tests
.venv/bin/mypy app           # 61 dosya
```

---

## 13. Açık PR #28'in değiştirdiği dosyalar

| Dosya | Değişiklik |
|---|---|
| `app/upstream/client.py` | `CAPTCHA_MARKERS` (56); `_is_captcha_body` (596); Cloudflare kademesi (1027-1037) |
| `app/schemas/upstream.py` | `to_wire()` (43) — boş token alanı gönderilmez |
| `app/services/recaptcha/static.py` | `NoopRecaptchaProvider.invalidate()` (53) |
| `app/core/config.py` | `upstream_cx` (88), `upstream_cx_send` (89) |
| `app/adapters/openai_to_upstream.py` | `cx` beslemesi (139) |
| `docs/RENDER.md` | §7 `cx` notu (265-272) |
| `docs/TROUBLESHOOTING.md` | `cx` notu (141-148) |
| `tests/test_cloudflare_challenge.py` | +63 satır |
| `tests/test_curl_to_env.py` | `cx` testi güncellendi |

---

## 14. Bilinen açık maddeler

| # | Konu | Konum |
|---|---|---|
| 1 | `recaptcha_provider` `PERSISTED_FIELDS` içinde değil | `app/upstream/state_store.py:67` |
| 2 | `recaptcha_token_ttl` = 100 sn cache | `app/core/config.py:324`, `app/services/recaptcha/base.py:51` |
| 3 | `cx` varsayılan gönderilmiyor | `app/core/config.py:89` |
| 4 | `render.yaml` site key blueprint değeri | `render.yaml:37` |
| 5 | URL-encoded JSON çerez biçimi (`unquote` yok) | `app/upstream/auth.py:122` |
| 6 | `_is_user_missing` / `_is_auth_body` sıralaması | `app/upstream/client.py:643,668` |
