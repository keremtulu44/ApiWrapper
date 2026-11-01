# Arıza el kitabı (Troubleshooting)

Bu belge "ne oldu / nereden anlarım / ne yapmalıyım" sırasıyla yazılmıştır.
Amaç her arızada log okumaya sıfırdan başlamamak.

> Örneklerde sağlayıcı adresi **`upstream.example`** olarak maskelenmiştir.

---

## 0. Hızlı teşhis

| Komut | Ne söyler |
|---|---|
| `GET /healthz` | Süreç ayakta mı (liveness) |
| `GET /readyz` | Yapılandırma tamam mı (upstream'e **dokunmaz**) |
| `GET /health` | Model/oturum/devre kesici durumu (upstream adresi varsayılan gizli) |
| `GET /v1/admin/config` | Etkin yapılandırmanın gizli olmayan özeti |
| `GET /v1/admin/accounts` | Hesap başına kota penceresi, cooldown, öğrenilen limit |
| `GET /v1/admin/auth?slot=N` | Cookie'den token çıkarılabiliyor mu, süresi dolmuş mu |
| `POST /v1/admin/breaker/reset` | Devre kesiciyi kapat (arıza geçtiyse) |
| `POST /v1/admin/recaptcha/invalidate` | reCAPTCHA token'ını zorla yenile |
| Telegram `/ping` | Bot → wrapper erişimi (yalnız bot katmanı) |

Loglarda olay adı (event) ile arayın: `upstream_request`, `upstream_http_error`,
`upstream_refresh_required`, `upstream_quota_exceeded`, `recaptcha_rejected_retrying`,
`account_switched`, `stream_completed`, `stream_failed`, `request_failed`.

---

## 1. `HTTP 409` + `{"error":"refresh_required","reason":"forced_update"}`

**Belirti**

```
{"status": 409, "body": "{\"error\":\"refresh_required\",\"reason\":\"forced_update\"}",
 "event": "upstream_http_error"}
{"error": "The upstream service rejected the captured client snapshot as outdated ...",
 "code": "upstream_refresh_required", "status_code": 503}
```

Telegram botu aynı arızada şunu yazar:

```
⚠️ ApiWrapper — upstream isteği başarısız.
Ne oldu: Upstream, istemci sürümünü eskimiş buldu (HTTP 409 refresh_required). [upstream_refresh_required]
Yapılacak: Tarayıcıda sayfayı sert yenileyip ... UPSTREAM_CURL değerini güncelleyip yeniden deploy edin.
```

**Ne anlama gelir**

Upstream ayakta, kimlik doğru, kota boş, CAPTCHA geçmiş. Sağlayıcı yeni bir
sürüm yayınladı ve tarayıcıdan alınan cURL'deki **sürüm/deployment damgasını**
(`x-...-deployment`, `x-deployment-id`) eski buluyor. Wrapper bu başlığı
`UPSTREAM_CURL`'den donmuş olarak taşır (`app/upstream/curl_import.py` →
`PASSTHROUGH_HEADERS`), bu yüzden sağlayıcının her sürüm yayınında tekrarlar.

**Yapılacak (2 dakika)**

1. Tarayıcıda uygulamayı **Ctrl+Shift+R** ile sert yenile ve giriş yap.
2. Yeni bir sohbet mesajı gönder, isteği **Copy as cURL** ile kopyala.
3. Render'da `UPSTREAM_CURL` secret'ını bu değerle güncelle → **Manual Deploy**.
4. Doğrula: Telegram `/ping` sonrası bir mesaj ya da `POST /v1/chat/completions`.
5. Farkı görmek istersen: `python scripts/compare_curl.py request.txt` —
   `x-...-deployment` satırı değişmiş olacak.

**Neden otomatik değil?** Token/cookie değil, istemci sürümü eski. Değeri
tahmin etmek yerine tarayıcıdan almak gerekiyor. Bunu bir *kanarya* ile
izliyoruz: keepalive/probe arıza çıkınca Telegram'a yazıyor, böylece kesintiyi
kullanıcıdan önce öğreniyorsunuz.

**Bu hata artık:**

* retry edilmez (aynı cevap döner, kotayı boşa harcar),
* devre kesiciyi açmaz (upstream sağlıklı; açsaydı diğer hesaplar da bloklanırdı),
* `503` + `upstream_refresh_required` + `Retry-After: 300` olarak döner.

---

## 2. `HTTP 401` — `upstream_unauthorized`

Oturum cookie'si/token'ı ölmüş. `/v1/admin/auth?slot=N` ile hangi hesabın
düştüğüne bakın (`expires_in_seconds: -1` = süresi dolmuş).

* Çok hesaplı kurulumda istek otomatik olarak **sonraki hesaba devredilir**
  (`account_switched ... reason=auth_rejected`); tek hesapta cURL tazelemek gerekir.
* Token sabit süreliyse (genelde ~1 saat) `UPSTREAM_CURL`'ü yenilemek tek çözümdür.

---

## 3. `HTTP 429` — `upstream_quota_reached`

Hesap geçici olarak kilitli; upstream bunu metinle bildiriyor
(`UPSTREAM_LIMIT_MARKERS`). Retry edilmez, `Retry-After` taşınır, hesap
`ACCOUNT_COOLDOWN_SECONDS` boyunca dinlendirilir ve istek diğer hesaba devredilir.

* `/v1/admin/accounts` → `messages_in_window`, `learned_limit`, `cooldown_remaining_seconds`.
* Kilit erken açıldıysa: `POST /v1/admin/accounts/{slot}/reset`.
* Bu hata **devre kesiciyi açmaz** (erişilebilirlik arızası değildir).

---

## 4. CAPTCHA — `recaptcha_unavailable` / `recaptcha_rejected`

| Belirti | Olası neden | Yapılacak |
|---|---|---|
| `recaptcha_unavailable`, browser kipinde timeout | Chromium açılamıyor, Google'a çıkış yok, bellek yetersiz | Render log'unda `recaptcha_startup_failed`; planı büyütün veya `external` kipe geçin |
| `recaptcha_rejected` | Token süresi dolmuş/reddedilmiş | `/v1/admin/recaptcha/invalidate`; kalıcı çözüm `browser`/`external` |
| `static` kipte ara sıra 403 | Sabit token ~2 dakikada ölür | `browser` kipine geçin |

Bildirim metni bu durumda "captcha token'ı üretilemedi/yenilenemedi" der ve
site key / provider ayarını işaret eder.

---

## 5. Zaman aşımı ve akış sorunları

| Kod | Anlamı | Ayar |
|---|---|---|
| `upstream_timeout` | Toplam süre aşıldı | `TOTAL_REQUEST_TIMEOUT` (vars. 600 sn) |
| `upstream_timeout` (akış ortasında) | İki token arası sessizlik | `STREAM_IDLE_TIMEOUT` (vars. 120 sn) |
| `upstream_not_found` | `UPSTREAM_STREAM_PATH` güncel değil | `scripts/curl_to_env.py` |
| `upstream_redirect` | Sağlayıcı 3xx döndürdü | Yolu yönlendirilen adrese güncelleyin |
| `upstream_unavailable` (503) | Devre kesici açık | `POST /v1/admin/breaker/reset` |

Stream'lerde `[DONE]` başarı ve hata yollarında **her zaman** gönderilir;
istemci koptuğunda gönderilmez (bağlantı zaten yok) ve jeneratör hatasız kapanır.

---

## 6. Telegram arıza bildirimleri

Kurulum:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789     # ilk kayıt varsayılan bildirim hedefi
# TELEGRAM_ALERT_CHAT_ID=987654321      # ayrı bir sohbet isterseniz
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_ALERT_REPEAT_SECONDS=21600     # aynı arıza için 6 saatte bir hatırlatma
UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=3600
UPSTREAM_KEEPALIVE_INITIAL_DELAY_SECONDS=60
```

Davranış (spam koruması):

| Durum | Mesaj |
|---|---|
| Yeni arıza (ilk kez görülen kod) | ✅ hemen |
| Arıza türü değişti (kota → captcha) | ✅ hemen |
| Aynı arıza sürüyor | ⏳ en fazla `TELEGRAM_ALERT_REPEAT_SECONDS` aralıkla |
| Düzeldi | ✅ bir kez, arızanın süresiyle birlikte |
| Her şey normal | ❌ sessiz |

Bildirim metni her zaman şu üçünü içerir: **ne oldu**, **hangi kod**, **ne yapmalı**.
Metinlerin tek kaynağı `app/core/diagnostics.py`; API cevapları OpenAI uyumluluğu
için İngilizce kalır, operatöre giden yüzeyler Türkçedir.

---

## 7. Dağıtım sonrası kontrol listesi

1. `GET /readyz` → `{"status":"ok"}` (eksikse `issues` listesine bakın).
2. Telegram `/ping` → `HTTP 200`.
3. Tek bir gerçek mesaj: bot üzerinden veya `POST /v1/chat/completions`.
4. Live tail'de `application_started` + `upstream_curl_loaded` satırları.
5. ~1 dakika sonra ilk probe: `upstream_keepalive_completed` (veya hata kodu).
