# Arıza el kitabı (Troubleshooting)

Bu belge "ne oldu / nereden anlarım / ne yapmalıyım" sırasıyla yazılmıştır.
Amaç her arızada log okumaya sıfırdan başlamamak.

> Örneklerde sağlayıcı adresi **`upstream.example`** olarak maskelenmiştir.

> ## ⚠️ SÜRÜM NOTU (19 Kasım 2026) — önce bunu oku
>
> Bu el kitabının gövdesi `77d4011` sürümü için yazıldı. Canlıdaki kod bir
> **geri dönüş** sürümüdür (`ROLLBACK_2026_11_16.md`): Telegram'da yalnız
> `/start`, `/help`, `/ping` var; çalışan servise cURL/çerez yüklenemez; olay
> defteri, otomatik token yenileme ve "damga sıyırma" yok. Bu yüzden aşağıdaki
> **"Yapılacak" adımlarının bir kısmı uygulanamaz.** Bölüm bölüm durum:
>
> | Bölüm | Bu sürümde |
> |---|---|
> | §0 Hızlı teşhis | ✅ 19 Kasım'da güncellendi |
> | §1 `409 refresh_required` | ⚠️ Kanarya ve tek taze-damga denemesi **var**; 2. kademe (damga sıyırma) **yok**. Ayar adları `DEPLOYMENT_…` (düzeltildi) |
> | §1b `409 → 401` | ⚠️ Teşhis geçerli; `/yenile`, `/cookie`, `/curl`, `/zaman` çareleri **yok** → tek çare Render env + yeniden deploy |
> | §1c `create-evaluation` görünmüyor | ⚠️ Tablo geçerli; "Kısayol" (`/cookie`) **yok** |
> | §2 `401` | ⚠️ **Başındaki 19 Kasım kuralını uygula.** Gövdedeki `/zaman`, `/cookie`, `UPSTREAM_REFRESH_API_KEY` maddeleri yok |
> | §2b tüketici `401 invalid_api_key` | ⚠️ `/v1/admin/keys` yok; anahtar listesi Render env'indeki `API_KEYS` / `ASK_API_KEYS`'tir |
> | §3 `429` | ✅ **EK (18 Kasım)** önceliklidir |
> | §3b görsel modu | ❌ mod yok |
> | §4 CAPTCHA | ✅ yalnız **DÜZELTME (17 Kasım)** kutusu; üstündeki tablo çürütüldü |
> | §5 zaman aşımı · §6 Telegram bildirimleri · §7 dağıtım sonrası | ✅ |
> | §8 `/v1/ask` · §9 iki hesap | ✅ PR #56 (kod canlıda değil) |
>
> **Bütün bölümlerin üstündeki dört kural** (`LESSONS_2026_11_14.md §19.1`):
>
> 1. **`429`, `recaptcha_rejected`, ≈180 sn süren probe uyarısı ⇒ BEKLE**, 2 saate
>    kadar hiçbir şeye dokunma. Üçü de ölçümde kendiliğinden geçti.
> 2. **Süren `401` ⇒ oturum öldü**, kendiliğinden dönmez. Tek 401'de önce doğrula
>    (bir sonraki probe / bota düz mesaj).
> 3. **Her çare bir restart'tır ve restart oturumu bitirir.** cURL'ü tazelemek =
>    Render env + yeniden deploy. Küçük düzeltmeleri biriktir.
> 4. **Uyarının etiketine değil `upstream_http_error … body=` log satırına güven.**

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
| `POST /v1/admin/recaptcha/invalidate` | reCAPTCHA token'ını zorla yenile (`static` sağlayıcıda **etkisiz**: token sabit) |
| `GET /v1/admin/queue` *(PR #56)* | Sıra, upstream'in bilinen durumu (`upstream_health`), hesap hatları, `/v1/ask` ayarları |
| Telegram `/ping` | Yalnız wrapper `/healthz`: **süreç ayakta mı.** Upstream'i göstermez |
| Bota **düz mesaj** | Gerçek soru: upstream'in cevap verdiğinin tek hızlı kanıtı |

> *(19 Kasım düzeltmesi: `/ping` satırı "+ son upstream kontrolünün saati/sonucu"
> diyordu ve bir `/tani` satırı vardı. İkisi de `77d4011`'e aitti; bu sürümde
> `/ping` yalnız `/healthz`'i çağırır, `/tani` yoktur.)*

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
(`recaptcha_rejected_retrying` `static` sağlayıcıda hiç basılmaz. PR #56'da
`account_switched` bir isteğin öbür hesabın sırasına taşındığını gösterir;
`/v1/ask` isteklerinde yanında `ask_rerouted` da basılır — §9.)

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

1. **Kanarya:** `DEPLOYMENT_ID_TTL_SECONDS` (vars. 1800 sn) boyunca
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
   Kapalı tutmak için: `DEPLOYMENT_STAMP_STRIP_ON_CONFLICT=false`.
   ⚠️ **Bu kademe bu sürümde YOK** (geri dönüşle kalktı; ayar, log ve metrik de
   yok). Bu sürümde sıra: kanarya → tek taze-damga denemesi → son kademe.
4. **Son kademe:** ikisi de işe yaramazsa oturum çerezini tazelemek için tek bir
   sayfa `GET`'i yapılır (`upstream_warmup`) ve hata mevcut net mesajla
   (`503 upstream_refresh_required`) döner. Bu `GET` aynı zamanda sayfada
   görünen damgayı yakalar (`deployment_id_detected`), yani bir sonraki istek
   taze damgayla gider.

İlgili ayarlar (hepsi opsiyonel):

```env
DEPLOYMENT_ID_REFRESH_ENABLED=true   # kanaryayı tamamen kapat: false
DEPLOYMENT_ID_TTL_SECONDS=1800       # ne sıklıkla okunacak
DEPLOYMENT_PROBE_PATH=/              # okunacak sayfa
DEPLOYMENT_PROBE_TIMEOUT=8
DEPLOYMENT_ID_AUTO_RETRY=true        # 409 sonrası tek taze-damga denemesi
```

> **19 Kasım düzeltmesi — bu adların başında `UPSTREAM_` YOK.** Bu bölüm (ve
> `.env.example`, `RENDER.md`) adları `UPSTREAM_DEPLOYMENT_…` diye veriyordu;
> uygulama o adları **okumaz**, değer sessizce yok sayılır
> (`app/core/config.py`: `deployment_id_refresh_enabled` …). Env'inde
> `UPSTREAM_DEPLOYMENT_…` varsa etkisizdir; varsayılanlar zaten yukarıdaki
> değerlerdir, yani davranış değişmemiştir.

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
4. Doğrula: bota düz bir mesaj ya da `POST /v1/chat/completions` (`/ping` yetmez).
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
*(Bu sürümde `/ping` upstream'i göstermez; kesintiyi probe'un Telegram
bildirimi haber verir.)*
Kalıcı çözüm yukarıdaki otomatik kurtarma kademeleridir; ikisi de tutmazsa
`deployment_stamp_unchanged` logu ile yeni cURL gerekir.

**409 gövdesinde `cx` geçiyorsa**

Canlı cURL'lerde stabil bir `cx` değeri var; wrapper yakalıyor ama varsayılan
olarak göndermiyor, istekler çalışıyor. Bir 4xx gövdesi `cx` alanına atıfta
bulunmaya başlarsa logda `upstream_cx_flag` +
`apiwrapper_upstream_cx_flags_total` metriği görünür: bu, alanın zorunlu hâle
geldiği anlamına gelir. O anda `UPSTREAM_CX_SEND=true` yapmak yeterlidir.
*(`UPSTREAM_CX_SEND` bu sürümde **yok**; `cx` gönderilmez.)*

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

> ⚠️ **Bu sürümde (19 Kasım notu):** aşağıdaki 1. ve 2. adım, "API
> karşılıkları" ve "Ölçüm" paragrafı **uygulanamaz** — `/yenile`, `/cookie`,
> `/curl`, `/zaman`, `/v1/admin/upstream/{curl,cookie,capture,refresh,apikey}` ve
> `/v1/admin/timeline` yok; log
> tablosundaki `upstream_session_dead` ve `upstream_identity_forensic_*`
> satırları da basılmaz. **Geçerli tek adım 3'tür:** Google ile giriş yapılmış
> tarayıcıdan taze cURL → Render'da `UPSTREAM_CURL` → yeniden deploy. Önce §2'nin
> başındaki doğrulama kuralı.

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
| `upstream_session_dead` | 409'dan sonra 401 "user not found": oturum ölmüş, yeni cURL şart. ⚠️ Bu satır **yalnızca damga sıyırma denendiyse** yazılır (`stamp_strip_used`); düz bir 401'de görünmez |
| `upstream_identity_forensic_retry` | 401 sonrası çerezdeki taze token'la tek doğrulama denemesi başladı |
| `upstream_identity_forensic_result` | `outcome=recovered` → suçlu bayat `UPSTREAM_ACCESS_TOKEN` (H3); `outcome=rejected` → oturum ölü (H2) |
| `upstream_identity_forensic_skipped` | `reason=no_fresh_token`: çerezde taze token yok, rotasyon durmuş |
| `upstream_refresh_required` | Yalnızca damga sorunu, oturum sağlam görünüyor |
| `deployment_stamp_refreshed` | Otomatik kurtarma çalıştı, elle müdahale gerekmez |

**Ölçüm:** log satırları tek tek olayı anlatır, "neyin ne zaman olduğu"nu değil.
Token rotasyonunun ne zaman durduğunu, isteğin hangi kimlikle gittiğini, süresi
geçmiş bir token'la başarılı istek olup olmadığını ve 401 anında çerezde taze
token bulunup bulunmadığını Telegram **`/zaman`** (veya `GET /v1/admin/timeline`)
tek raporda verir. Dört hipotezin (H1 hedef yaşı denetliyor · H2 oturum hedefte
düştü · H3 sabitlenmiş token gölgeliyor · H4 ikinci süreç aynı çerezi kullanıyor)
hangisinin geçerli olduğuna dair karar tablosu ve 24 saatlik deney planı:
**[`SESSION_FORENSICS.md`](SESSION_FORENSICS.md)**.

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

> ⚠️ **Bu sürümde (19 Kasım notu):** aşağıdaki kısayol ve tablodaki `/yenile` /
> `/cookie` çareleri **yok**. Bu sürümde tam `create-evaluation` cURL'ü
> **şarttır**: `UPSTREAM_CURL` ondan akış yolunu, `mode`'u ve captcha token'ını
> da okur.

**Kısayol:** `create-evaluation` şart değil. Wrapper'ın asıl ihtiyacı `Cookie`
başlığıdır:

* **En hızlı:** Cookie-Editor → Export → *Header String* → `/cookie <satır>`
* cURL'de `cookie` varsa çalışır; yol sohbet ucu değilse wrapper **canlı akış
  yolunu korur** ve yanıtta `⚠` uyarı döner (`allow_path_change: true` ile
  bilinçli olarak değiştirilebilir). Yani yanlış isteğin cURL'ü servisi bozmaz.

## 2. `HTTP 401` — `upstream_unauthorized`

> ### 🔴 GEÇERLİ KURAL (19 Kasım) — aşağıdaki maddelerden önce
>
> Bu belgede ve README'de 401 için iki zıt cümle vardı: *"bekle, genelde kendi
> döner"* ve *"kendiliğinden düzelmez, taze cURL al"*. İkisinin de arkasında bir
> ölçüm var; kural ikisini birleştirir:
>
> | Ölçüm | Ne gösterdi |
> |---|---|
> | 14-15 Kasım (`LESSONS §1`) | Tarayıcı girişinden sonra 401 başladı; **dokunulmadı**, aynı cURL ertesi gün çalışıyordu. 401 döneminin süresi kayıtlı değil |
> | 16 Kasım (`ROLLBACK §2`) | **Art arda 6 probe** (~50 dk) 401 aldı; oturum dönmedi, token rotasyonu durmuştu |
>
> 1. **O hesapla tarayıcıdan giriş yapma.** Giriş, hâlâ yaşayan oturumu kesin
>    bitirir; geri alınamaz tek adım budur.
> 2. **Bir kez doğrula:** bir sonraki probe'u bekle (iki hesapta o hesabın kendi
>    probe'u; "yeniden çalışıyor" bildirimi gelir) ya da tek hesapta bota düz bir
>    mesaj yaz. Cevap geldiyse geçiciydi — dokunma.
> 3. **Hâlâ 401 ise oturum ölmüştür ve kendiliğinden dönmez.** Google ile giriş
>    yapılmış tarayıcıdan taze cURL al → **sekmeyi kapat, çıkış yapma** → Render
>    env'inde `UPSTREAM_CURL` → yeniden deploy.
>
> Aşağıdaki maddelerden bu sürümde **geçerli olmayanlar:** `/cookie` (1. madde),
> `/zaman` ve `forensic_retry` (5.-6. madde), `UPSTREAM_REFRESH_API_KEY` (son
> madde). 4.-5. maddedeki *"`exp` pratikte bağlayıcı değil, hedef token yaşını
> denetlemiyor"* iddiasının dayanağı 16 Kasım ölçümüyle **çürütüldü** (`LESSONS §13`): token
> ~20 dakikada bir yenileniyor; 15 saat yaşayan şey çerezdi, token değil.

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
* Token sabit süreli değildir: ölçülen `expires_in` değerleri 3600 s ve 1200 s
  olarak değişti (`docs/CAPTURE_TIMELINE.md`). Karar `exp` alanına dayanır.
* **Ama `exp` pratikte bağlayıcı değil:** hedef token yaşını denetlemiyor. Aynı
  çerez ve token'la **15+ saat** çalıştı (15 Kasım 2026 ölçümü). Yani 401'in
  sebebi genelde süre dolması değil, **aynı hesapla tarayıcıda oturum açılması**
  (`docs/LESSONS_2026_11_14.md`).
  Bu iddia artık **sürekli ölçülüyor**: `/zaman` raporundaki
  `En yaşlı ÇALIŞAN token` ve `exp aşımıyla çalışan istek` satırları, süresi
  geçmiş bir token'la istek geçtiğini gösterir. Ölçüm bir gün tersini söylerse
  bu maddeyi düzelt (`docs/SESSION_FORENSICS.md` Faz 1).
* **401'in kimin yüzünden olduğunu ölç:** `/zaman` (ya da `GET /v1/admin/timeline`)
  `forensic_retry` sonucunu raporlar — `recovered` ise suçlu bayat
  `UPSTREAM_ACCESS_TOKEN`'dır (env'den **sil**), `rejected` ise oturum gerçekten
  ölmüştür (taze cURL). Ayrıntı: [`SESSION_FORENSICS.md`](SESSION_FORENSICS.md).
* **Yer tutucu token tuzağı:** `UPSTREAM_ACCESS_TOKEN` içine "boş" anlamında
  `APIWRAPPER_EMPTY_VALUE_V1` gibi bir metin yazıldıysa istek
  `Authorization: Bearer APIWRAPPER_EMPTY_VALUE_V1` ile gider ve çerez ne kadar
  taze olursa olsun **her istek 401 alır** (açık token çerezden önce tercih
  edilir). Bu sürümden sonra bu tür yer tutucular yüklenirken boşaltılır
  (`env_placeholder_ignored` uyarısı verilir); yine de alanı tamamen silmek
  doğrusudur.
* **İki kaynak çelişkisi:** `UPSTREAM_CURL` doluysa açılışta çerez ve hedef alan
  adı **cURL'den** okunur; ayrı verilen `UPSTREAM_COOKIE` ve `TARGET_DOMAIN` yok
  sayılır (log: `upstream_cookie_shadowed_by_curl`). Taze çerez verip "hiçbir
  şey değişmedi" görüyorsanız güncellenmesi gereken alan `UPSTREAM_CURL`'dür.
* `UPSTREAM_REFRESH_API_KEY` boşsa Supabase tarzı token uç noktası `apikey`
  ister: yenileme `No API key found in request` ile döner. Public anon anahtarı
  bir kez elle yazın (sır değildir).

### 401 mi 429 mu? (karıştırılan iki arıza)

| Gördüğünüz | Gerçek sebep | Yapılacak |
|---|---|---|
| `502 upstream_unauthorized` | Oturum öldü (401 / `user not found`) | Kimliği tazeleyin: ~~`/cookie`, `/curl` veya `/yenile`~~ *(bu sürümde yok)* → bölümün başındaki 3 adım |
| `429 upstream_quota_reached` + `retry_after` | Hesap gerçekten kilitli, upstream bunu **metinle** söyledi | Bekleyin; `Retry-After` süresi dolmadan denemeyin |
| `429` + `"All upstream accounts are temporarily locked ... Earliest retry in 0s"` | **Eski sürüm hatası**: ölü oturum kota gibi raporlanıyordu | Güncel sürümde bu mesaj 401 aldığınızda görünmez; görüyorsanız önce kimliği tazeleyin |

> 2026-11 sürümünde düzeltildi: tek hesaplı kurulumda 401 alındığında hesap havuzu
> boş kaldığı için orijinal hata `UpstreamQuotaExceeded` ile eziliyor ve istemci
> "kota doldu" görüyordu. Artık 401 her zaman `upstream_unauthorized` döner.

---

## 2b. Tüketici `401 invalid_api_key` alıyor (anahtar tarafı)

> ⚠️ **Bu sürümde (19 Kasım notu):** aşağıdaki tablonun `/v1/admin/keys`,
> `is_admin`, `project_key_revoked` ve `upstream_state_restored` satırları
> **yok**. Geçerli anahtarlar Render env'indeki `API_KEYS` (her uç) ve
> `ASK_API_KEYS` (yalnız `/v1/ask`, PR #56) listeleridir; kontrol edilecek tek
> şey tüketicinin anahtarının o listede birebir bulunması ve başlık biçimidir.
> `ASK_API_KEYS` anahtarı sohbet/yönetim uçlarında `401` alır — beklenen davranış.

Yukarıdaki 401 **upstream** oturumuyla ilgiliydi ve istemciye `502
upstream_unauthorized` olarak yansır. Tüketicinin gördüğü düz `401
invalid_api_key` ise **bizim anahtar listemizle** ilgilidir — çerezle ilgisi
yoktur, `/curl` ya da `/yenile` düzeltmez.

| Kontrol | Komut | Ne beklenir |
|---|---|---|
| Anahtar hâlâ listede mi | `GET /v1/admin/keys` | `key_id` görünüyor, `masked_key` tüketicinin elindekiyle örtüşüyor |
| Yanlış anahtar tipi mi | aynı yanıtta `is_admin` | `/v1/admin/*` için `ADMIN_API_KEYS` (boşsa tüm `API_KEYS`) gerekir |
| İptal edilmiş mi | log: `project_key_revoked key_id=…` | Varsa yeni anahtar üret |
| Başlık biçimi | — | `Authorization: Bearer sk-…` (tırnaksız, tek boşluk) |
| Anahtar restart'ta kayboldu mu | log: `upstream_state_restored fields=[…]` | `api_keys` listede olmalı; `runtime_state_empty_api_keys_ignored` görüyorsanız kayıt boştur, env'e bakın |

Tüketicinin kendi tarafında yapması gerekenler ve hata kodlarının tam listesi:
**[`CONSUMERS.md`](CONSUMERS.md)**.

---

## 3. `HTTP 429` — `upstream_quota_reached`

Hesap geçici olarak kilitli; upstream bunu metinle bildiriyor
(`UPSTREAM_LIMIT_MARKERS`). Retry edilmez, `Retry-After` taşınır, hesap
`ACCOUNT_COOLDOWN_SECONDS` boyunca dinlendirilir ve istek diğer hesaba devredilir.

* `/v1/admin/accounts` → `messages_in_window`, `learned_limit`, `cooldown_remaining_seconds`.
* Kilit erken açıldıysa: `POST /v1/admin/accounts/{slot}/reset`.
* Bu hata **devre kesiciyi açmaz** (erişilebilirlik arızası değildir).

> ### 🔴 EK (18 Kasım ölçümü) — kota bu kodla **gelmeyebilir**
>
> Öncelik **`LESSONS_2026_11_14.md §16`**'da. Hedef kotayı
> `429 {"error":"daily spend limit reached"}` diye bildirdi; bu ifade varsayılan
> `UPSTREAM_LIMIT_MARKERS` listesinde **yok**. Sonuç, yukarıdaki davranışın tersi:
>
> | Beklenen (işaret tanınırsa) | Ölçülen (tanınmadı) |
> |---|---|
> | Retry edilmez | 429 **tekrar denendi**, arada 300 sn uyundu |
> | İstemci hemen `429 upstream_quota_reached` alır | İstek **10 dakika asılı** kaldı → `504` (`TOTAL_REQUEST_TIMEOUT`) |
> | Hesap dinlendirilir / devredilir | Cooldown ve devir **tetiklenmedi** |
> | Uyarı kodu `upstream_quota_reached` | Uyarı **`wrapper_unreachable`** geldi ("PORT doğru mu?") |
>
> **Tanı:** uyarı `wrapper_unreachable` + probe süresi ≈180.000 ms ise logda
> `upstream_http_error status=429 body=` ara.
> **Çare:** **bekle** — 1 sa 46 dk'da kendiliğinden kalktı, oturum ölmedi.
> **Restart etme:** bu sürümde restart oturumu öldürür (`ROLLBACK_2026_11_16.md §12.1`).
> **Kalıcı düzeltme — isteğe bağlı, yalnız env** (bir sonraki doğal restart'ta;
> env listesi varsayılanın **yerine geçer**, bu yüzden tamamı yazılır). PR #56'da
> da varsayılan liste canlıdakiyle aynıdır: ifade koda eklenmedi, çünkü liste
> upstream'e bir isteğin kaç kez gönderileceğini belirler ve gönderme tarafı
> operatör kararı olmadan değişmez (`LESSONS §20`). PR #56'da fark yalnız
> etikette: uyarı `wrapper_unreachable` yerine `upstream_timeout` gelir.
>
> ```env
> UPSTREAM_LIMIT_MARKERS=upstream limit reached,message limit reached,conversation limit reached,daily limit reached,usage limit reached,quota exceeded,rate limit exceeded,too many requests,daily spend limit reached
> ```

### 3b. `429 {"error":"prompt failed"}` — yalnız görsel modunda (ikincil mod)

> ⚠️ **Bu sürümde görsel modu yok** (`model=image` → `model_not_found`). Kayıttır.

Belirti: `model=image` isteğinde üç kez
`upstream_http_error status=429 body={"error":"prompt failed"}`, ardından
`stream_failed error_code=upstream_rate_limited content_chars=0`. Aynı dakikada
`max`/`web` çalışıyorsa kimlik (çerez/token/`mode`/`modelAId`) sağlıklıdır; red
**moda özgüdür** — upstream tarafında görsel üretimi bu hesap/izinle kapalı
görünüyor. Bu bir kota değildir: `/v1/admin/accounts` pencereleri normaldir ve
yanıtta `Retry-After` gelmez; beklemek sonucu değiştirmez.

Karar (15 Kasım ölçümü): mod **silinmedi, vitrinden indirildi** —
`config/models.yaml`'da `listed: false`. `GET /v1/models` onu listelemez (istemci
açılır listesi çalışmayan modu önermez), `resolve()` çalışmaya devam eder.
Görsel modunu ana akışa almadan önce upstream'in kabul ettiğini ölç: tarayıcıda
gerçek bir görsel üret, `create-evaluation` isteğinin 200 döndüğünü ve akışta
`a2:[{"type":"image",…}]` parçasının geldiğini gör, sonra `listed: true` yap.
Ayrıntı: `LESSONS_2026_11_14.md §10`.

---

## 4. CAPTCHA — `recaptcha_unavailable` / `recaptcha_rejected`

> ⚠️ **Önce aşağıdaki "DÜZELTME (17 Kasım ölçümü)" kutusunu oku.** Bu tablonun
> "Yapılacak" sütunu çürütüldü; `recaptcha_rejected` için geçerli çare
> **beklemektir** (ölçülen 10-15 dk; 2 saate kadar). Tablo kayıt olarak duruyor.

| Belirti | Olası neden | Yapılacak |
|---|---|---|
| `recaptcha_unavailable`, browser kipinde timeout | Chromium açılamıyor, Google'a çıkış yok, bellek yetersiz | Render log'unda `recaptcha_startup_failed`; planı büyütün veya `external` kipe geçin |
| `recaptcha_rejected` | Token yanlış/eksik, `UPSTREAM_RECAPTCHA_FIELD` alan adı tarayıcıyla eşleşmiyor, ya da token başka bir mod/action için üretilmiş | Taze **maskesiz** cURL (`/curl`); alan adını `scripts/compare_curl.py` ile doğrula. `browser` bu hedefte çözüm **değil** (bkz. `LESSONS_2026_11_14.md` §3-§4) |
| `static` kipte ara sıra 403 | ~~Sabit token ~2 dakikada ölür~~ — ölçümde **2+ saat** geçerli, hedef yaşı denetlemiyor | Token'ı taze cURL ile yenile; red sürüyorsa aşağıdaki "moda özgü red" satırına bak |
| Red yalnız `web`/`image`'da, sohbet çalışıyor | Token o modun `action`'ı için üretilmemiş, ya da `UPSTREAM_CX_SEND=true` ile sohbet `cx`'i tüm modlara gönderiliyor (`cx` ölçümde her istekte değişiyor) | O modun sayfasından ayrı cURL al; `UPSTREAM_CX_SEND=false` dene; `upstream_recaptcha_rejected_body` log satırındaki gövde önizlemesi kesin sebebi söyler |
| Modlar eklendikten sonra **sohbet** 403/401 almaya başladı | `config/models.yaml` içindeki sohbet kaydına `referer_path` yazılmış; kayıt düzeyi değer env'i ezer ve cURL'den gelen `/c/{chat_id}` parmak izini bozar | Sohbet kaydından `referer_path`'ı sil → `UPSTREAM_REFERER_PATH` (cURL'den gelen) kullanılır |

Bildirim metni bu durumda "captcha token'ı üretilemedi/yenilenemedi" der ve
site key / provider ayarını işaret eder.

> ### 🔴 DÜZELTME (17 Kasım ölçümü) — yukarıdaki "Yapılacak" sütununa güvenme
>
> Öncelik **`LESSONS_2026_11_14.md §15`**'te. Ölçülen gerçek:
>
> 1. **`recaptcha_rejected` GEÇİCİDİR.** `RECAPTCHA_PROVIDER=static` iken
>    gönderilen token **bayt-özdeş ve değişmez** (`recaptcha/static.py:26-38`,
>    cache bile yok) ve static'te **retry yok**
>    (`completion_service.py:455-460` → `raise`). Yani wrapper iyileşmek için
>    hiçbir şey yapmıyor; yine de red **10-15 dakikada kendiliğinden** geçti.
>    ⇒ **Sorun token değil.**
> 2. **Doğru çare: BEKLE.** 10 saatlik kesintisiz koşuda red bir kez görüldü ve
>    dokunulmadan geçti.
> 3. **"Taze cURL al" çaresi ZARARLIDIR.** Taze cURL tarayıcıya giriş ister;
>    giriş **sunucu oturumunu öldürür** (`LESSONS §1`). Belgedeki çareyi
>    uygulamak sağlıklı oturumu kendi elinle öldürmek demekti. Ayrıca **`/curl`
>    bu sürümde yok** (Telegram'da 4 komut: `/healthz /help /ping /start`).
> 4. **`static.py:14` ("token ~2 dk") ve `diagnostics.py:56-67` ("yenileyin",
>    "browser/external kalıcı çözümdür") çürütüldü** — aynı token 10 saat
>    (önce 15 saat) çalıştı, `browser` bu hedefte çözüm değil (§3-§4).
> 5. **"captcha" etiketi yanıltıcı olabilir:** `client.py:343`'teki işaret
>    listesinde **`"forbidden"`** var ve `AUTH_STATUS` yalnız `{401}` (`:43`) ⇒
>    her 403 ya captcha ya düz HTTP hatası sayılıyor; karar sırası captcha
>    lehine (`:651-653`). **Kesin sebep için `upstream_http_error` log
>    satırının `body=` alanına bak** (`client.py:533-537`) — sınıflandırmadan
>    *önce* yazılır.
> 6. **`upstream_recaptcha_rejected_body` log satırı bu sürümde YOK** (yukarıdaki
>    tablonun 4. satırı onu işaret ediyor — bayat referans, `git grep` boş).
> 7. **cURL'ü yalnız Google ile giriş yapılmış oturumdan al.** E-posta + şifre
>    ile girilmiş oturumun cURL'ü `recaptcha_rejected` veriyor (`LESSONS §15.1`).
>
> **19 Kasım eki.** (a) Bekleme sınırı: ölçülen süre 10-15 dakikadır; kural
> olarak **2 saate kadar** beklenir, 2 saati aşarsa taze cURL. (b) 3. maddedeki
> komut sayısı **3**'tür (`/start`, `/help`, `/ping`); `/healthz` bir HTTP ucudur.
> (c) 4. maddede çürütüldüğü yazılan iki kod metni (`static.py`,
> `diagnostics.py`) ve API hata mesajı **PR #56'da düzeltildi**; canlı
> (`03aca2c`) kodda eski hâlleriyle duruyor, yani canlıdaki Telegram uyarısı
> hâlâ "token'ı yenileyin / browser sağlayıcıya geçin" der — **uygulama**.

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
2. Telegram `/ping` → `HTTP 200` (yalnız süreç ayakta; upstream'i kanıtlamaz).
3. Tek bir gerçek mesaj: bot üzerinden veya `POST /v1/chat/completions` —
   **cURL'ün çalıştığının kanıtı budur.**
4. Live tail'de `application_started` + `upstream_curl_loaded` satırları.
5. ~1 dakika sonra ilk probe: `upstream_keepalive_completed` (veya hata kodu).

---

## 8. `/v1/ask` ve istek sırası (PR #56 — henüz canlıda değil)

> Bu bölüm PR #56 canlıya alındıktan sonra geçerlidir (`ROLLBACK_2026_11_16.md §13`).
> Sözleşme ve Telegram mesajlarının okunuşu: [`ASK_API.md`](ASK_API.md).

**İlk bakılacak yer** (upstream'e istek atmaz, kota harcamaz):

```bash
curl -s https://<servis>.onrender.com/v1/admin/queue -H "Authorization: Bearer sk-..."
```

| Belirti | Olası neden | Nereye bak / ne yap |
|---|---|---|
| Karşı sistem **her istekte anında** `problem` alıyor | Arıza biliniyor, bekletme sürüyor | `upstream_health.failing: true`, `hold_remaining_seconds`. Telegram'daki ilk mesaj nedeni söyler. Geçiciyse **bekle**; süre dolunca tek istek kendiliğinden dener |
| Karşı sistem `problem` alıyor ama **Telegram'a mesaj gelmedi** | (a) Aynı arıza için mesaj daha önce geldi — sonrakiler sayılır. (b) Karşı sistemin isteği hatalı (HTTP 4xx): "REDDEDİLDİ" mesajı tür başına saatte bir kez gelir, sonrakiler sayılır. (c) Telegram uyarıları yapılandırılmamış | (a) `alerts.ask_problems` sayacı. (b) Live tail: `ask_rejected code=…`. (c) `alerts.enabled: false` → §6 |
| Telegram'a **"dış istek REDDEDİLDİ … KURULUM HATASI"** geldi | Karşı sistem yanlış anahtar, bilinmeyen model ya da boş `prompt` gönderiyor; her sorusu yedeğe gidiyor | Mesajdaki `Yapılacak` ve `Sunucunun cevabı` satırları. Upstream'de sorun yok; karşı sistemin ayarını düzelt |
| Karşı sistem **45 sn'de vazgeçiyor**, logda `ask_abandoned` | `wait` satırları ona ulaşmıyor (ara vekil tamponluyor) ya da kendi toplam süre sınırı kısa | `curl -N …/v1/ask` ile dene: satırlar aktıkça geliyorsa sorun karşı sistemin HTTP ayarında (`ASK_API.md §2` "zaman aşımı ayarları") |
| `problem`, logda `ask_problem code=upstream_timeout` | Upstream 180 sn'de bitiremedi | Tek seferlikse yok say. Sürekliyse upstream yavaş; `ASK_REQUEST_TIMEOUT_SECONDS` karşı sistemin sabrıyla birlikte düşünülmeli |
| `problem`, `reason=overload`, `code=queue_full` | Sırada 20 istek birikti | Karşı sistem çok hızlı gönderiyor ya da öndeki istekler yavaş. `queue.waiting_labels` kimin beklediğini gösterir |
| `problem`, `code=queue_timeout` | İstek sırada 600 sn bekledi | Aynı; `ASK_MAX_QUEUE_WAIT_SECONDS` |
| `problem`, `code=empty_response` | Upstream boş cevap döndü | Live tail: `completion_completed content_chars=0`. Tekrarlıyorsa akış biçimi değişmiş olabilir |
| Telegram botu **"Wrapper yanıtı zaman aşımına uğradı"** yazıyor | (a) Mesajın sırada, önündeki isteklerin arkasında 180 sn'den uzun bekledi (istek sıradan çıkarıldı, upstream'e gitmedi). (b) Mesaj upstream'e gitti ama cevap 180 sn'de gelmedi: istek arkada **sürer** (kesilmez), cevabı atılır | (a) Yeniden yaz. (b) Önce `queue.active` boşalsın (`active_seconds` büyüyorsa o istek hâlâ sürüyor); sonra yeniden yaz. Log: `client_disconnected … upstream=in_flight action=finish` |
| `queue.active` bir süredir `telegram` / `api` / `keepalive`, ama o istemci çoktan gitti | **Beklenen davranış**: upstream'e gönderilmiş akışsız istek, istemcisi kopsa da sonuna kadar sürer (canlı sürümdeki gibi; `LESSONS §20.3`). En çok `TOTAL_REQUEST_TIMEOUT` (600 sn) | Bekle; sıra kendiliğinden boşalır |
| Art arda iki istekten ikincisi 5–10 sn geç cevaplanıyor | **Beklenen davranış** (istekler arası rastgele bekleme) | `REQUEST_GAP_MIN/MAX_SECONDS` |
| Saatlik probe logda yok, yerine `upstream_keepalive_deferred` | **Beklenen davranış**: son aralıkta gerçek bir istek başarıyla cevaplandı | Sabit düzen istenirse `UPSTREAM_KEEPALIVE_SKIP_WHEN_ACTIVE=false` |
| `upstream_keepalive_skipped reason=queue_rejected` | Probe sırada 60 sn bekleyip geri çevrildi; **arıza değil**, 30 sn sonra yeniden dener | — |
| `request_queue_stale_slot` | Bir istek sırayı 30 dk tuttu; emniyet supabı sırayı kurtardı. **Olmaması gerekir** | Aynı dakikadaki `request_id`'yi not et ve bildir |
| `queue.active` saatlerdir aynı etiket, `active_seconds` büyüyor | Sızmış bir istek (yukarıdakinin öncesi) | Emniyet supabını bekle (en çok 30 dk); kalıcı çözüm için bildir |

**Bekletmeyi elle kaldırmanın bir ucu yoktur — gerek de yoktur:** Telegram'dan
bota yazdığın bir mesaj upstream'e gider; başarılı olursa bekletme o anda kalkar.

**`wait` satırlarını elle görmek:** `curl -N` ile uzun süren bir soru gönder;
30. saniyede ilk `data: {"status":"wait",…}` satırı görünmelidir.

---

## 9. İki hesap (`UPSTREAM_CURL_2`) — PR #56, henüz canlıda değil

> Davranışın tamamı: [`TWO_ACCOUNTS.md`](TWO_ACCOUNTS.md). İlk bakılacak yer yine
> `GET /v1/admin/queue` (§8); bu kez `accounts.lanes` bölümü.

| Belirti | Olası neden | Nereye bak / ne yap |
|---|---|---|
| Deploy **başarısız**, logda `UPSTREAM_CURL_2 …` ile başlayan hata | İkinci cURL bozuk (URL/çerez yok) ya da **1. hesapla aynı hesaba ait** | Hata metni hangisi olduğunu söyler. Eski sürüm çalışmaya devam eder. Başka bir hesapla giriş yapıp cURL'ü yeniden al |
| `accounts.count: 1` (iki bekliyordun) | `UPSTREAM_CURL_2` boş ya da yanlış adla girilmiş | Render → Environment. Log: `upstream_curl_loaded … accounts=[1]` |
| Bir hatta `usable: false` | O hesap arızalı (`upstream_health.code`) ya da kota dinlenmesinde (`cooldown_remaining_seconds`) | Telegram'daki `Hesap: …` mesajı nedeni söyler. Geçiciyse **bekle**: hesap kendi probe'u başarılı olunca döner |
| `assigned_total` sayıları birbirinden çok farklı | Bir hesap bir süre devre dışı kaldı; ya da trafiğin çoğu **sohbet yeniden kullanan** bir istemciden (`SESSION_REUSE`) | `account_switched` / `upstream_hold_started` logları. Sohbet bağlılığı beklenen davranıştır (`TWO_ACCOUNTS.md §3`) |
| Telegram: **"bir hesap cevap veremedi; istek diğer hesaba taşındı"** | Hesabın oturumu düştü (`401`) ya da kotası doldu; istek öbür hesaptan cevaplandı | Karşı sistem etkilenmedi. `KALICI` ise: iki cURL'ü **birlikte** tazeleyip deploy et; acelesi yok |
| Bir hesap **geri dönmüyor** | (a) Hâlâ arızalı: probe'u da başarısız. (b) Keepalive kapalı: 30 dk'da bir gerçek istekle denenir | (a) `upstream_keepalive_failed account=…`. (b) Beklemeden: `POST /v1/admin/accounts/<N>/resume` |
| Karşı sistem **her istekte anında** `problem` alıyor | **İki hesap da** arızalı; bekletme sürüyor | `accounts.lanes[*].upstream_health`. Mesajdaki `Hesaplar:` satırı |
| İsteklerin **yarısı** `problem` alıyor | `ACCOUNT_FAILOVER=false` ve bir hesap arızalı | Beklenen davranış (katı bölüşüm). İşin öbür hesaba gitmesini istiyorsan `ACCOUNT_FAILOVER=true` |
| `problem`, `code=queue_timeout`, hemen ardından "istek diğer hesaba taşındı" mesajı | Taşınan istek öbür hesabın sırasında beklerken 180 sn'si doldu | Öbür hesap o an uzun bir işle meşguldü. Tek seferlikse yok say |
| `400 … x-apiwrapper-account` | Sabitleme başlığında olmayan yuva ya da sayı olmayan değer | `1` ya da `2` yaz. Başlık `/v1/ask`'te yok sayılır |

**Tek bir hesabı sınamak** (öbürüne dokunmadan): `TWO_ACCOUNTS.md §8`.
