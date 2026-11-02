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
| Telegram `/ping` | Wrapper `/healthz` **+ son upstream kontrolünün saati/sonucu** |
| Telegram `/tani` | Çerez adları, oturum adayı, çerezdeki/kullanılan token, refresh token **+ tek satır "ne yapmalı"** |

Genel zihinsel model (hangi katman kendiliğinden tazelenir):
**[`docs/BIG_PICTURE.md`](BIG_PICTURE.md)**.

Çerezlerin hangisi ne kadar yaşar, hangisi elle tazelenmeli:
**[`docs/COOKIE_LIFECYCLE.md`](COOKIE_LIFECYCLE.md)**.

Loglarda olay adı (event) ile arayın: `upstream_request`, `upstream_http_error`,
`upstream_refresh_required`, `upstream_quota_exceeded`, `recaptcha_rejected_retrying`,
`account_switched`, `stream_completed`, `stream_failed`, `request_failed`.
Sürüm damgası/oturum tarafı: `deployment_id_detected`, `deployment_id_changed`,
`deployment_stamp_refreshed`, `deployment_stamp_unchanged`, `upstream_set_cookie`,
`upstream_cx_flag`, `upstream_referer_path_default`.

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

**Otomatik kurtarma (artık çoğu 409 kendiliğinden geçer)**

Wrapper, sürüm damgasını **dondurulmuş** tutmak yerine upstream'in kendi
sayfasından okuyor (`app/upstream/deployment.py`):

1. **Kanarya:** `UPSTREAM_DEPLOYMENT_ID_TTL_SECONDS` (vars. 1800 sn) boyunca
   geçerli olacak şekilde ana sayfa bir `GET` ile okunur. Değer önce yanıt
   başlıklarından (`x-deployment-id`, `x-arena-web-deployment`), olmazsa HTML
   içindeki `dpl_...` deseninden alınır.
   * Bu okuma **kota harcamaz** (completion değil, sayfa isteği).
   * Bot koruması (403/challenge), ağ hatası ya da sayfa değişikliği olursa
     kanarya sessizce devre dışı kalır ve `UPSTREAM_CURL`'deki değer kullanılır.
     **Kanarya hiçbir zaman isteği bozmaz.**
   * Damga değiştiğinde logda `deployment_id_changed` görünür.
2. **409 kurtarma (1. kademe):** 409 `refresh_required` alınırsa damga **zorla**
   yeniden okunur; gerçekten farklıysa istek **bir kez** taze damgayla tekrar
   edilir (normal retry bütçesinden yemez). Logda `deployment_stamp_refreshed`.
3. **409 kurtarma (2. kademe):** yeni damga bulunamadıysa istek, eskimiş
   `x-deployment-id` / `x-arena-web-deployment` başlıkları **tamamen
   çıkarılarak** bir kez daha denenir. Bu kademe hiçbir değer bilmeyi
   gerektirmez; skew koruması olmayan istek genelde kabul edilir. Logda
   `deployment_stamp_stripped`, metrikte `apiwrapper_deployment_stamp_strip_total`.
   Kapalı tutmak için: `UPSTREAM_DEPLOYMENT_STAMP_STRIP_ON_CONFLICT=false`.
4. **Son kademe:** ikisi de işe yaramazsa oturum çerezini tazelemek için tek bir
   sayfa `GET`'i yapılır (`upstream_warmup`) ve hata mevcut net mesajla
   (`503 upstream_refresh_required`) döner. Bu `GET` aynı zamanda sayfada
   görünen damgayı yakalar (`deployment_id_detected`), yani bir sonraki istek
   taze damgayla gider.

İlgili ayarlar (hepsi opsiyonel):

```env
UPSTREAM_DEPLOYMENT_ID_REFRESH_ENABLED=true   # kanaryayı tamamen kapat: false
UPSTREAM_DEPLOYMENT_ID_TTL_SECONDS=1800       # ne sıklıkla okunacak
UPSTREAM_DEPLOYMENT_PROBE_PATH=/              # okunacak sayfa
UPSTREAM_DEPLOYMENT_PROBE_TIMEOUT=8
UPSTREAM_DEPLOYMENT_ID_AUTO_RETRY=true        # 409 sonrası tek taze-damga denemesi
```

Kanarya yalnızca istekte **zaten bulunan** deployment başlıklarını tazeler;
tanımadığı upstream'lere yeni başlık eklemez. `UPSTREAM_CURL` içinde hiç
deployment başlığı yoksa kanarya hiç çalışmaz (`http_409` gibi jenerik 409'lar
damga sorunu sayılmaz).

**Hâlâ elle müdahale gereken durum**

Logda `deployment_stamp_unchanged` görünüyorsa upstream damgayı değiştirmemiş ama
isteği yine reddediyor: bu durumda oturum anlık görüntüsü (cookie/token) eskimiş
olabilir. O zaman:

1. Tarayıcıda uygulamayı **Ctrl+Shift+R** ile sert yenile ve giriş yap.
2. Yeni bir sohbet mesajı gönder, isteği **Copy as cURL** ile kopyala.
3. Render'da `UPSTREAM_CURL` secret'ını bu değerle güncelle → **Manual Deploy**.
4. Doğrula: Telegram `/ping` sonrası bir mesaj ya da `POST /v1/chat/completions`.
5. Farkı görmek istersen: `python scripts/compare_curl.py request.txt`.

**Bu hata sınıfı:**

* devre kesiciyi **açmaz** (upstream sağlıklı; açsaydı diğer hesaplar da
  bloklanırdı),
* otomatik kurtarma tükendikten sonra `503` + `upstream_refresh_required` +
  `Retry-After: 300` olarak döner.

**Saatte bir tekrarlayan 409 (`forced_update`) ne demek?**

Canlıda gözlendi (2026-11-02, saatlik keepalive): her probe `409
{"error":"refresh_required","reason":"forced_update"}` alıyor, yani
**upstream yeni bir sürüm yayınladı ve elimizdeki damga geçersiz**. Bu durumda
`/healthz` hâlâ `200` döner — süreç ayaktadır ama **hiçbir istek çalışmıyor**.
`/ping` artık son upstream kontrolünü de yazdığı için bu sessiz kesinti
görünür olur; PR sonrası ayrıca Telegram'a tek bir özet bildirim gider.
Kalıcı çözüm yukarıdaki otomatik kurtarma kademeleridir; ikisi de tutmazsa
`deployment_stamp_unchanged` logu ile yeni cURL gerekir.

**409 gövdesinde `cx` geçiyorsa**

Canlı cURL'lerde stabil bir `cx` değeri var; wrapper şimdilik göndermiyor ve
istekler çalışıyor. Bir 4xx gövdesi `cx` alanına atıfta bulunmaya başlarsa logda
`upstream_cx_flag` + `apiwrapper_upstream_cx_flags_total` metriği görünür: bu,
alanın zorunlu hâle geldiği anlamına gelir.

---

## 1b. `409` → `401 {"message":"User not found"}` — oturum gerçekten ölmüş

**Belirti (canlı, 2026-11-02)**

```
{"status":409,"marker":"refresh_required","deployment_id":"dpl_...","source":"configured",
 "event":"deployment_stamp_unchanged"}
{"status":409,"marker":"refresh_required","event":"deployment_stamp_stripped"}
{"status":401,"body":"{"message":"User not found"}","event":"upstream_http_error"}
{"error":"Upstream request failed with HTTP 401.","status_code":502,
 "code":"upstream_failure","event":"request_failed"}
```

**Ne anlama gelir**

409 (`forced_update`) **ilk kapıdır**, gerçek arıza değildir. Damga başlıkları
çıkarılınca istek yeni deployment'a ulaşıyor ama upstream **"bu kullanıcı yok"**
diyor: yakalanan cookie/access_token o deployment'ta geçersiz. Yani 409 damga
sorununu, 401 ise asıl sorunu söylüyor — **oturum ölmüş**.

Bu senaryoda damga kanaryası işe yaramaz: ana sayfa oturum olmadan `dpl_`
kimliğini yayınlamıyor (`source: configured`). Sıralama doğru çalışıyor:
tazeleme → sıyırma → hata.

**Yapılacak — telefonda DevTools gerekmez**

1. **`/yenile`** (veya `/refresh`) → servisteki Chromium hedef sayfayı açar,
   gerçek bir `create-evaluation` isteğini yakalar ve cookie + sürüm damgası +
   `cx` + referer yolunu **anında** uygular. Deploy yok, cURL yok.
   * `✓ Oturum yakalandı` → bir mesaj atıp test edin.
   * `⚠ Oturum yakalanamadı` → site oturumu tamamen düşmüş; 2. adıma geçin.
2. **Elinde çerez satırı varsa:** `/cookie ad=değer; ad2=değer2` — cURL'ün
   tamamı gerekmez, wrapper `access_token`'ı kendisi ayıklar.
   Tam cURL varsa `/curl <cURL>`.
   Mobilde bu satırı nasıl alacağın: **[`MOBILE_CURL.md`](MOBILE_CURL.md)**
   (Chrome/Safari'de DevTools yok; eklenti çalıştıran Android tarayıcı gerekir).
3. Son çare **Render:** `UPSTREAM_CURL` secret'ını güncelleyip Manual Deploy.
4. Doğrula: bot'a bir mesaj atın ya da `/ping` → `✓ Son upstream kontrolü: ...`.

API karşılıkları: `POST /v1/admin/upstream/capture` (gövdesiz) ve
`POST /v1/admin/upstream/curl {"curl": "..."}`. Gizli değerler ne yanıtta ne de
loglarda görünür.

**Logda ayırt etme**

| Olay | Anlamı |
|---|---|
| `upstream_session_dead` | 409'dan sonra 401 "user not found": oturum ölmüş, yeni cURL şart |
| `upstream_refresh_required` | Yalnızca damga sorunu, oturum sağlam görünüyor |
| `deployment_stamp_refreshed` | Otomatik kurtarma çalıştı, elle müdahale gerekmez |

---

## 1c. Geliştirici araçlarında `create-evaluation` **görünmüyor**

Şikâyet: "Network sekmesinde mesaj attım ama `create-evaluation` isteği yok."

| Sebep | Nasıl anlaşılır | Çözüm |
|---|---|---|
| **Akış zaten açık** — ilk mesajdan sonra sohbet tek bir açık SSE bağlantısı üzerinden yürür, yeni HTTP isteği doğmaz | İlk mesajda istek vardı, sonrakilerde yok | Sayfayı **tam yenile** (F5) → **ilk** mesajı gönder |
| Filtre kutusunda eski metin / `Fetch/XHR` dışı tür seçili | Ağ listesi neredeyse boş | Filtre kutusunu temizle, **All** seç; EventSource ise **Other** altında çıkar |
| Yanlış sekme/frame | — | "Preserve log" açık olsun; iframe/Service Worker varsa "Group by frame" kapat |
| İstek engellendi | Kırmızı `ERR_BLOCKED_BY_CLIENT` | Reklam engelleyiciyi bu site için kapat |
| Uç nokta farklı adlandırılmış | `post-to-evaluation`, `evaluation`, `stream` arayın | Arama kutusuna `evaluation` yazın |
| Mobil tarayıcı | Network paneli yok | `/yenile` ya da Cookie-Editor → `/cookie` |

**Kısayol:** `create-evaluation` şart değil. Wrapper'ın asıl ihtiyacı `Cookie`
başlığıdır:

* **En hızlı:** Cookie-Editor → Export → *Header String* → `/cookie <satır>`
* cURL'de `cookie` varsa çalışır; yol sohbet ucu değilse wrapper **canlı akış
  yolunu korur** ve yanıtta `⚠` uyarı döner (`allow_path_change: true` ile
  bilinçli olarak değiştirilebilir). Yani yanlış isteğin cURL'ü servisi bozmaz.

## 2. `HTTP 401` — `upstream_unauthorized`

Oturum cookie'si/token'ı ölmüş. `/v1/admin/auth?slot=N` ile hangi hesabın
düştüğüne bakın (`expires_in_seconds: -1` = süresi dolmuş).

* `UPSTREAM_ACCESS_TOKEN` doluysa `Authorization` başlığı **çerezden önce** tercih
  edilir. Bu yüzden `/cookie` uygulandığında çerezdeki token aynı anda
  `upstream_access_token` alanına da yazılır; yanıtta
  `fields: ["upstream_cookie","upstream_access_token"]` görürsünüz. Yalnızca
  `upstream_cookie` görüyorsanız çerezde token yok (çerez adıyla oturum
  taşınıyor) ve elle verilmiş token korunur.
* Çok hesaplı kurulumda istek otomatik olarak **sonraki hesaba devredilir**
  (`account_switched ... reason=auth_rejected`); tek hesapta kimlik tazelemek gerekir.
* Token sabit süreliyse (genelde ~1 saat) `UPSTREAM_CURL`'ü yenilemek tek çözümdür.

### 401 mi 429 mu? (karıştırılan iki arıza)

| Gördüğünüz | Gerçek sebep | Yapılacak |
|---|---|---|
| `502 upstream_unauthorized` | Oturum öldü (401 / `user not found`) | Kimliği tazeleyin: `/cookie`, `/curl` veya `/yenile` |
| `429 upstream_quota_reached` + `retry_after` | Hesap gerçekten kilitli, upstream bunu **metinle** söyledi | Bekleyin; `Retry-After` süresi dolmadan denemeyin |
| `429` + `"All upstream accounts are temporarily locked ... Earliest retry in 0s"` | **Eski sürüm hatası**: ölü oturum kota gibi raporlanıyordu | Güncel sürümde bu mesaj 401 aldığınızda görünmez; görüyorsanız önce kimliği tazeleyin |

> 2026-11 sürümünde düzeltildi: tek hesaplı kurulumda 401 alındığında hesap havuzu
> boş kaldığı için orijinal hata `UpstreamQuotaExceeded` ile eziliyor ve istemci
> "kota doldu" görüyordu. Artık 401 her zaman `upstream_unauthorized` döner.

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
