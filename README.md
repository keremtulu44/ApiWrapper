# ApiWrapper

Harici bir Web LLM servisini **OpenAI uyumlu** bir REST API'ye dönüştüren, üretime hazır
FastAPI servisi. OpenAI SDK, LangChain, Open WebUI, Cursor, Continue gibi tüm
OpenAI-compatible istemcilerle çalışır.

```
İstemci ──(OpenAI şeması)──▶ ApiWrapper ──(text/plain, tarayıcı taklidi)──▶ Upstream Web LLM
        ◀──(SSE chat.completion.chunk)──          ◀──(AI SDK data-stream)──
```

> ## 🔙 SÜRÜM NOTU — 16/17 Kasım 2026: bu bir **geri dönüş** sürümüdür
>
> Bu ağaç **`e1e8d5a`**'ye (14 Kasım 16:42 UTC) geri sarıldı. Gerekçe: kesintisiz
> **15 saat** çalıştığı ölçülen tek sürüm buydu; sonraki 6 merge (#49–#54) boyunca
> oturum ömrü 3-4 saate indi.
>
> **Aşağıdaki bölümlerin bir kısmı bu sürümde OLMAYAN özellikleri anlatıyor.**
> Dokümanlar bilerek eksiltilmedi (kayıt değeri taşısınlar diye), o yüzden
> okurken şu tabloyu kullan:
>
> | Aşağıda anlatılan | Bu sürümde |
> |---|---|
> | Web arama modu (`model=web`), görsel mod (`model=image`) | ❌ **yok** — kayıt defterinde yalnız `max` var |
> | `Kaynaklar:` bloğu / `extract_citations` | ❌ yok |
> | `/zaman`, olay defteri, forensic retry, `identity_forensics` | ❌ yok |
> | Proje anahtarları (`/v1/admin/keys`), `ADMIN_API_KEYS`, `API_KEY_LABELS` | ❌ yok (admin katmanı bu sürümde hiç yok) |
> | Çalışma-zamanı durum dosyası + **Render env sync** | ❌ yok — bkz. aşağıdaki işletim kuralı |
> | Telegram komutları | ⚠️ **33 → 4:** yalnız `/healthz`, `/help`, `/ping`, `/start` |
> | `docs/` içeriği, CI (`.github/`), Chromium (`browser_login.py`), `Dockerfile` | ✅ aynen korundu |
>
> ### ⚠️ Bu sürümün işletim kuralı (kalıcılık katmanı YOK)
>
> Render free tier'da kalıcı disk yok ve bu sürüm çerezi **hiçbir yere yazmıyor**.
> Dönen çerez/token yalnız bellekte yaşar. Ayakta kalmanın tek yolu açılışta
> `UPSTREAM_CURL`'ün env'de dolu olması:
>
> 1. **`UPSTREAM_CURL`** → Render env'inde her zaman taze dursun. Açılışta
>    `apply_upstream_curl()` uygulanır (`main.py:226`).
> 2. **`UPSTREAM_MODE=direct-battle`** → Render env'ine **elle gir.** Bu sürümün
>    `render.yaml`'ında tanımlı değil; boş kalırsa `mode` gövdeden düşer ve
>    **her istek `400 ZodError · path ["mode"]`** döner.
> 3. **`UPSTREAM_STREAM_PATH`** → env'de tut. Varsayılan (`post-to-evaluation`)
>    bayat; 15 Kasım ölçümüne göre canlı uç `create-evaluation`.
> 4. **Her deploy/restart'tan sonra `/ping` çek.** Çalışmıyorsa `UPSTREAM_CURL`'ü
>    tazele ve yeniden deploy et. **Telefondan `/curl` YOK** — cURL güncellemek
>    Render panosu ister.
> 5. cURL'ü aldıktan sonra **tarayıcıdan çıkış yap** (aşağıdaki 1. kural değişmedi).
> 6. **cURL'ü yalnız Google ile giriş yapılmış oturumdan al.** E-posta + şifre ile
>    giriş yapılmış oturumun cURL'ü wrapper'da `recaptcha_rejected` veriyor
>    (17 Kasım ölçümü) — bkz. `docs/LESSONS_2026_11_14.md §15.1`.
> 7. **`recaptcha_rejected` görürsen 10-15 dakika BEKLE, hiçbir şeye dokunma.**
>    `RECAPTCHA_PROVIDER=static` iken gönderilen token değişmez ve bu sürümde
>    retry yok; yani red **hedef tarafında geçici**. Ölçüldü: bir kez görüldü,
>    kendiliğinden geçti. **Taze cURL almaya kalkma** — tarayıcıya giriş yapmak
>    sağlıklı oturumu öldürür (1. kural). Ayrıntı: `LESSONS §15.3-§15.4`.
> 8. **`429` veya `wrapper_unreachable` (probe ≈180 sn) görürsen BEKLE.** Hedef
>    `429 daily spend limit reached` dönüyor; bu ifade varsayılan işaret
>    listesinde olmadığı için uyarı yanlış kodla ("PORT doğru mu?") geliyor.
>    Ölçüldü (18 Kasım): 1 sa 46 dk'da kendiliğinden kalktı, oturum ölmedi.
>    Ayrıntı ve env düzeltmesi: `docs/LESSONS_2026_11_14.md §16`.
> 9. **Env değişikliği = restart = oturum kaybı.** 18 Kasım'da tek bir env
>    değeri için yapılan redeploy ~28 saatlik sağlıklı oturumu bitirdi.
>    Düzeltmeleri biriktir, cURL'ün zaten yenileneceği âna sakla.
> 10. **4. maddeye düzeltme:** bu sürümde `/ping` yalnız sürecin ayakta olduğunu
>     söyler, cURL'ün çalıştığını **söylemez**. Deploy sonrası bota düz bir
>     mesaj yaz ya da ~1 dakika sonraki ilk probe'u bekle (uyarı yoksa geçti).
> 11. **`/v1/ask` ve tek istek sırası yazıldı — henüz canlıda DEĞİL (PR #56).**
>     Alarm/otomasyon sistemleri için `wait` / `done` / `problem` ucu; bütün
>     istekler tek sıradan, aralarında 5–10 sn ile geçer; arıza ayrıntısı
>     Telegram'a gelir. Canlıya alma bir restart'tır (9. madde): taze cURL ile
>     **aynı anda** yapılır. Sözleşme: **[`docs/ASK_API.md`](docs/ASK_API.md)** ·
>     canlıya alma adımları: `docs/ROLLBACK_2026_11_16.md §13`.
>
> Tam kayıt, ölçümler, kaldırılanların listesi ve ileri dönüş tarifi:
> **[`docs/ROLLBACK_2026_11_16.md`](docs/ROLLBACK_2026_11_16.md)**
> Geri dönüşün **sonucu** (ep3: 10 saat+) ve ileri dönüşün doğru tarifi:
> **`docs/ROLLBACK_2026_11_16.md §11`**
> ep3'ün **kesin** sonucu (~28 saat, doğal ölüm yok) ve ep4 ("3 gün
> dokunmadan", probe aralığı 3600): **`docs/ROLLBACK_2026_11_16.md §12`**

> ## ⚠️ Önce oku (en sık yapılan 3 hata)
>
> **1. Çerezi öldüren şey zaman değil, çakışma.** Çerez **15+ saat** yaşar
> (ölçüldü). Onu öldüren şey **aynı hesapla tarayıcıda oturum açmaktır** —
> platform yeni oturumu tek geçerli sayar, sunucudaki çerez iptal olur.
> `401 User not found` görürsen **hemen cURL arama**, tarayıcıya girmeden bekle;
> kod 401'de çerezi silmez, genelde kendi kendine döner.
>
> **2. `RECAPTCHA_PROVIDER=static` kalıcı.** Sunucudaki Chromium'un ürettiği
> token reddediliyor (`403 recaptcha validation failed`). Aynı çerez/gövde ile
> gerçek tarayıcıdan alınan statik token çalışıyor. Ölçüldü, tartışmaya kapalı.
>
> **3. "Bildirim aldım" ≠ "oturum öldü".** Bildirim tek başına çerezi bozmaz.
> Bozan şey **giriş yapmaktır**.
>
> Tam liste: **[`docs/INDEX.md`](docs/INDEX.md)** · ölçümler:
> **[`docs/LESSONS_2026_11_14.md`](docs/LESSONS_2026_11_14.md)**

**Doküman rehberi (buradan başla): [`docs/INDEX.md`](docs/INDEX.md)**

> **Otomatik tarayıcı girişi (Chromium) bu dalda yoktur.** Kod yazıldı ve test
> edildi ama `main`'e bilerek alınmadı — şu an gerekmiyor (çerez 15+ saat
> yaşıyor) ve kullanıcının tarayıcı oturumuyla çakışabilir. Nerede durduğu,
> bekleme modu ve nasıl devreye alınacağı:
> **[`docs/INDEX.md` § Chromium](docs/INDEX.md#chromium--otomatik-tarayıcı-girişi--nerede-duruyor)**

Mimari ayrıntılar için: **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** ·
büyük resim ve 3 çerez katmanı: **[`docs/BIG_PICTURE.md`](docs/BIG_PICTURE.md)** ·
çerez ömrü ve otomatik yenileme: **[`docs/COOKIE_LIFECYCLE.md`](docs/COOKIE_LIFECYCLE.md)** ·
Render kurulumu: **[`docs/RENDER.md`](docs/RENDER.md)** ·
mobilden çerez/cURL alma: **[`docs/MOBILE_CURL.md`](docs/MOBILE_CURL.md)** ·
arıza/teşhis için: **[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)** ·
son uçtan uca denetim kaydı: **[`docs/AUDIT_2026_11.md`](docs/AUDIT_2026_11.md)** ·
**yeni oturum başlangıç dosyası: [`docs/START_HERE.md`](docs/START_HERE.md)** ·
oturum devir dosyası (dosya/satır konumları): **[`docs/HANDOFF.md`](docs/HANDOFF.md)**

> **Dağıtım notu:** Render, servis ayarlarında seçili production dalı deploy eder;
> `render.yaml` dal seçmez. Canlı servisin hangi dalı izlediğini Render panelinden
> doğrulayın. Geçmiş merge kayıtları: **[`docs/MERGE_TARGET.md`](docs/MERGE_TARGET.md)**.

---

## Hızlı Belge ve Operasyon Rehberi

Sistem üç ayrı ömür katmanını (**Oturum Kimliği**, **Türev/Sürüm Damgası**, **Geçici reCAPTCHA/Anti-bot**) birbirinden ayırarak kendi kendini idame ettirir (`self-healing`):

| Konu | Belge | Kısa Özet |
|---|---|---|
| **Büyük Resim & 3 Katman** | **[`docs/BIG_PICTURE.md`](docs/BIG_PICTURE.md)** | Neden saat başı cURL gerekmez? Hangi değer sabit, hangisi döner, sistem nasıl kendi kendine yeniler? |
| **Çerez Ömrü & Otomatik Refresh** | **[`docs/COOKIE_LIFECYCLE.md`](docs/COOKIE_LIFECYCLE.md)** | `refresh_token` rotasyonu, `apikey` otomatik keşfi, parçalı çerez (`.0`/`.1`) yeniden yazımı ve 401 kurtarma |
| **Yakalama Zaman Çizelgesi (ölçüm)** | **[`docs/CAPTURE_TIMELINE.md`](docs/CAPTURE_TIMELINE.md)** | Üç cURL'ün zaman sırasına göre farkı: hangi değer istek / sayfa / giriş / deploy başına döner; ölçülen `expires_in` (3600 → 1200 s) |
| **Free Render Kurulumu** | **[`docs/RENDER.md`](docs/RENDER.md)** | `UPSTREAM_CURL`, `RENDER_API_KEY` ile restart sonrası kalıcılık, uyku davranışı ve gömülü Telegram botu |
| **Telefondan Yönetim** | **[`docs/MOBILE_CURL.md`](docs/MOBILE_CURL.md)** | Bilgisayar yokken `/tani`, `/zaman`, `/token`, `/apikey`, `/yenile`, `/cookie` ve `/curl` komutları |
| **Arıza & Teşhis Sözlüğü** | **[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)** | 401 / 403 / 409 / 429 hata kodlarının anlamı ve otomatik kurtarma kademeleri |
| **Yeni Oturum Başlangıcı** | **[`docs/START_HERE.md`](docs/START_HERE.md)** | Okuma sırası, kurulum, doğrulama, problem tespit adımları, düzenleme kuralları |
| **Oturum Devri (dosya/satır haritası)** | **[`docs/HANDOFF.md`](docs/HANDOFF.md)** | Hangi davranış hangi dosyada, hangi satırda; doğrulanmış konum listesi |

---

## Özellikler

| Alan | Ayrıntı |
|---|---|
| Uç noktalar | `POST /v1/chat/completions` (stream + non-stream), `POST /v1/completions`, `GET /v1/models`, `GET /v1/models/{id}`, `GET /health`, `GET /metrics`, `/v1/admin/*` |
| Modlar | Sohbet · **web arama**: istemci `model` adıyla seçer (`max` / `web`), gövdede `modality` + referer yolu birlikte değişir. Tek `UPSTREAM_CURL` iki modu da taşır. Görsel modu (`image`) **ikincil/deneysel**: `listed: false`, vitrin dışında |
| Akış | Vercel AI SDK data-stream (`0:`, `f:`, `e:`, `d:`, `3:` …) → OpenAI SSE; UTF-8 sınır güvenli; `[DONE]` garantili |
| Upstream auth | Cookie'den `access_token` ayıklama (JSON/base64/parçalı) → `Authorization: Bearer`; `/v1/admin/auth` teşhisi |
| Güvenlik | Sabit zamanlı API key doğrulama, log maskeleme, gövde/mesaj/prompt limitleri, SSRF'e kapalı sabit hedef |
| Dayanıklılık | Retry + full jitter, `Retry-After` desteği, devre kesici, eşzamanlılık tavanı, idle timeout, istemci kopması iptali |
| Kota tespiti | Hedefin "limit reached" tarzı mesajı işaret tabanlı yakalanır (HTTP gövdesi · akış hata olayı · opsiyonel düz metin taraması) → `429 upstream_quota_reached` + `Retry-After`, **retry edilmez** |
| Hesap havuzu | Çoklu çerez/token; kayan pencere sayacı, kilitte otomatik hesap geçişi, AIMD ile öğrenilen bütçe, hesap başına captcha token'ı |
| Sohbet yönetimi | `SESSION_REUSE=true` ile istemci başına tek upstream sohbeti; `SESSION_ROTATE_AFTER_MESSAGES`/`_SECONDS` eşiğinde yeni sohbete geçilir (Continue gibi `conversation_id` gönderemeyen istemciler için) |
| reCAPTCHA v3 | `static` (varsayılan) · `noop` · `browser` (Playwright) · `external` — TTL cache + tek-uçuş kilidi |
| Gözlemlenebilirlik | structlog JSON, `X-Request-ID`, `completion_completed`/`stream_completed`/`stream_failed` olayları, Prometheus metrikleri (TTFT dahil) |
| Arıza bildirimi | Saatlik probe → Telegram: **ne oldu + hangi kod + ne yapmalı**; yeni arıza/düzelme her zaman, aynı arıza `TELEGRAM_ALERT_REPEAT_SECONDS` aralıkla (vars. 6 saat) — spam yok |
| Sürüm eskimesi | Upstream `409 refresh_required`/`forced_update` → `upstream_refresh_required` (retry yok, devre kesiciye sayılmaz, net eylem metni) |
| Uyumluluk | `stop` dizileri (yerel kesme), `stream_options.include_usage`, alias'lı model kayıt defteri, multimodal içerik parçaları |

---

## Hızlı Başlangıç

```bash
git clone <repo> && cd ApiWrapper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,tokens,browser]"
playwright install chromium

cp .env.example .env      # TARGET_DOMAIN, API_KEYS ve UPSTREAM_COOKIE doldurun
$EDITOR config/models.yaml # Max kaydı hazır; başka modeller için gerçek modelAId girin

make run                  # http://localhost:8000/docs
```

### Zorunlu yapılandırma

| Değişken | Açıklama |
|---|---|
| `UPSTREAM_CURL` (Render önerisi) | Tam `create-evaluation` cURL'ü; domain, yol, mode, cookie/token ve model ID'yi başlangıçta çıkarır |
| `TARGET_DOMAIN` | `UPSTREAM_CURL` yoksa hedef servis domaini, **şemasız** (örn. `example-llm.com`) |
| `API_KEYS` | Yerel istemcilerin kullanacağı anahtarlar (virgülle ayrıştırılmış) |
| `ADMIN_API_KEYS` | Oturum kimliğini değiştiren `/v1/admin` uçlarına izinli anahtarlar; **boşsa tüm `API_KEYS` admin sayılır** (eski davranış) |
| `RECAPTCHA_PROVIDER=static` + `RECAPTCHA_STATIC_TOKEN` | **KALICI seçim.** Sunucudan token üretimi (`browser`) hedefte reddediliyor; gerçek tarayıcıdan alınmış statik token çalışıyor (`docs/LESSONS_2026_11_14.md`) |
| `config/models.yaml` | `id` ↔ `upstream_id` (`modelAId`) eşlemesi + **mod** (`modality`, `referer_path`) |

> Hedef domain ve model kimlikleri çalıştırma koduna gömülü değildir; `.env` ve
> `config/models.yaml` üzerinden okunur. Örnek ayar son cURL'deki
> `/nextjs-api/stream/create-evaluation` yolunu kullanır. Hedef değişirse yolu yeni
> cURL'den içe aktarın; yanlış yol upstream **HTTP 404** üretir.

### `.env` denetimi

Değer değiştirip hiçbir şeyin değişmediği durum neredeyse her zaman **okunmayan
bir addan** kaynaklanır: `Settings` bilinmeyen adı sessizce yok sayar. Denetim
betiği dosyayı uygulamanın gerçek alanlarıyla karşılaştırır ve yalnız durum
bildirir (gizli değer yazdırmaz):

```bash
python -m scripts.env_audit .env          # metin
python -m scripts.env_audit .env --json   # makine okunur
python -m scripts.env_audit .env --strict # ölü/yer tutucu/ezilen varsa çıkış 1
```

Çıktı beş bölümdür: **ölü anahtarlar** (yakın doğru ad varsa önerilir),
**yer tutucu gizliler**, **sabitlenmemesi gerekenler** (istek/oturum başına
üretilen `cx` ve statik captcha token'ı gibi), **birbirini ezen anahtarlar**
(`UPSTREAM_CURL` açılışta `UPSTREAM_COOKIE`'yi ezer) ve **katman eksikleri**
(`/v1/admin/auth` ile aynı karar katmanından gelir).

Sıfırdan dolduruyorsan hazır dosyayı kopyala — beş satır doldurulacak, gerisi hazır:

```bash
cp env.mobile.example .env
$EDITOR .env    # TARGET_DOMAIN, API_KEYS, ADMIN_API_KEYS, UPSTREAM_USER_AGENT, UPSTREAM_CURL
python -m scripts.env_audit .env
```

`env.mobile.example` üretilir (`python -m scripts.env_template --env --out env.mobile.example`),
elle düzenlenmez; üç bölümden oluşur: **doldur**, **hazır (mobil profil)**, **boş bırak / yazma**.

Tam liste (150 anahtar, alfabetik, her satırda "ne yazacaksın") için
[`docs/ENV_FULL.md`](docs/ENV_FULL.md). O dosya üretilir, elle düzenlenmez:

```bash
python -m scripts.env_template          # docs/ENV_FULL.md'yi yeniler
python -m scripts.env_template --env    # kopyalanabilir .env iskeleti
```

---

## Upstream Kimlik Doğrulama (`Authorization: Bearer`)

Kimlik doğrulama hedefe göre yalnızca `Cookie` ile veya ayrıca
`Authorization: Bearer <access_token>` başlığıyla yapılabilir. Son alınan normal sohbet
cURL'ünde Authorization başlığı **yoktu**; bu kurulumda `UPSTREAM_AUTH_FROM_COOKIE=false`
kalır. Hedef gerçekten Bearer başlığı isterse ApiWrapper token'ı cookie'den otomatik
ayıklayıp ekleyebilir.

```bash
# Tarayıcıdan kopyalanan ham Cookie başlığı yeterlidir:
UPSTREAM_COOKIE=theme=dark; sb-abc-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOi...; other=1
```

Desteklenen çerez biçimleri (`app/upstream/auth.py`):

| Biçim | Örnek |
|---|---|
| Düz değer | `access_token=eyJhbGciOi...` |
| URL-encoded | `access_token=eyJ...%3D%3D` |
| JSON nesnesi | `sb-auth-token={"access_token":"eyJ...","refresh_token":"..."}` |
| JSON dizisi | `sb-auth-token=["eyJ...","refresh",null,null]` |
| base64-JSON | `sb-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOi...` |
| Parçalı çerez | `sb-auth-token.0=base64-eyJ...` + `sb-auth-token.1=...` |

### İlgili ayarlar

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `UPSTREAM_AUTH_FROM_COOKIE` | `false` | Cookie'den ayıklanan tokenla ayrıca Authorization üret; yalnız hedef bunu istiyorsa aç |
| `UPSTREAM_ACCESS_TOKEN` | — | Token'ı doğrudan ver (en yüksek öncelik) |
| `UPSTREAM_TOKEN_COOKIE_NAMES` | — | Token'ı taşıyan çerez adları (virgülle); boşsa sezgisel arama |
| `UPSTREAM_AUTH_SCHEME` | `Bearer` | Boş bırakılırsa token ham gönderilir |
| `UPSTREAM_WARN_ON_EXPIRED_TOKEN` | `true` | JWT `exp` dolmuşsa logda uyar |

> `UPSTREAM_EXTRA_HEADERS` en son uygulanır; `authorization=...` yazarak
> otomatik üretilen başlığı bilinçli olarak ezebilirsiniz.
>
> **Otomatik Token Yenileme & Kalıcılık (`app/upstream/refresh.py`, `app/upstream/state_store.py`):**
> Oturum çerezindeki `refresh_token` ayrıştırılır; `access_token` bitmesine 10 dk
> kala (veya istek anında 401 alındığında) otomatik yenilenir, parçalı çereze ve
> aktif `AccountPool` / `UpstreamClient` cookie jar'larına yayılır. Dönen yeni
> `refresh_token` yerel durum dosyasına (`UPSTREAM_STATE_FILE`) ve yapılandırılmışsa
> Render servis ortam değişkenlerine (`RENDER_API_KEY` + `RENDER_SERVICE_ID`)
> sessizce kaydedilir. Ayrıntılar: **[`docs/COOKIE_LIFECYCLE.md`](docs/COOKIE_LIFECYCLE.md)**.

### Teşhis

Token'ın doğru ayıklanıp ayıklanmadığını **token'ı ifşa etmeden** kontrol edin:

```bash
curl -s -H "Authorization: Bearer sk-local-dev-key" \
  http://localhost:8000/v1/admin/auth | jq
```

```json
{
  "authorization_header_will_be_sent": true,
  "source": "cookie",
  "cookie_names_present": ["other", "sb-abc-auth-token", "theme"],
  "token": {"found": true, "masked": "eyJ***yz", "is_jwt": true,
            "expires_in_seconds": 3567, "expired": false}
}
```

Upstream yine de 401 dönerse yanıt artık genel bir hata değil, eyleme dönük olur:
`upstream_unauthorized` + "Refresh UPSTREAM_COOKIE...". Süresi dolmuş bir JWT
gönderilmeden önce log uyarısı düşer (`access_token_expired`, kaç saniye önce dolduğu ile).


---

## Kendi cURL'ünüze Göre Ayarlama (önemli)

Hedef servisler ayrıntılarda farklılaşır. Tarayıcıdan aldığınız isteği bir dosyaya
kaydedip **karşılaştırma aracını** çalıştırın; hangi başlık/gövde alanının uyuşmadığını
size satır satır söyler (Windows `^`, bash `\` ve PowerShell `` ` `` kaçışlarını anlar):

```bash
# DevTools > Network > sağ tık > Copy as cURL  -> request.txt
python scripts/compare_curl.py request.txt
```

```text
=== Gövde alanları (--data-raw) ===
  EŞLEŞTİ id
  EKSİK   recaptchaV2Token  <-- gövdemizde yok!
  FAZLA   recaptchaV3Token  <-- tarayıcı göndermiyor

  !! UPSTREAM_RECAPTCHA_FIELD=recaptchaV3Token ancak tarayıcı 'recaptchaV2Token' gönderiyor.
     .env içine yazın: UPSTREAM_RECAPTCHA_FIELD=recaptchaV2Token
```

### Sık karşılaşılan üç fark

| Belirti | Ayar |
|---|---|
| Gövdede `recaptchaV2Token` var, biz `recaptchaV3Token` yolluyoruz | `UPSTREAM_RECAPTCHA_FIELD=recaptchaV2Token` |
| `accept-language` farklı (örn. `tr-TR,...`) | `UPSTREAM_ACCEPT_LANGUAGE=tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7` |
| cURL'de `authorization` başlığı **yok** | `UPSTREAM_AUTH_FROM_COOKIE=false` (varsayılan) |
| `cx` her istekte değişiyor | Sabit değer yazılmaz. İlk canlı test alan olmadan yapılır; zorunluysa istemci JS'sindeki üretim yöntemi gerekir |

> **Cloudflare:** İsteğinizde `cf_clearance` çerezi varsa onu da `UPSTREAM_COOKIE`
> içine **olduğu gibi** ekleyin. `cf_clearance` yalnızca onu üreten IP + User-Agent
> ikilisiyle geçerlidir; bu yüzden `UPSTREAM_USER_AGENT` tarayıcınızla birebir aynı
> olmalı ve sunucu tarayıcıyla aynı çıkıştan (IP) istek atmalıdır — aksi halde
> Cloudflare yeniden doğrulama ister. `cf_clearance` asla `access_token` sanılmaz.


### Windows / PowerShell

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\run.ps1                       # servisi baslat (pencere 1)
.\scripts\windows\chat.ps1 "Merhaba"            # mesaj gonder  (pencere 2)
.\scripts\windows\chat.ps1 -Interactive         # surekli sohbet
.\scripts\windows\chat.ps1 -Diagnose            # saglik + kimlik teshisi
```

Ayrıntı: [`scripts/windows/README.md`](scripts/windows/README.md)


---

## Kullanım

**cURL — streaming**

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "max",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "Merhaba, kendini tanıt."}]
  }'
```

**OpenAI Python SDK**

`base_url` `/v1` ile veya onsuz verilebilir; her iki biçim de aynı uçlara gider.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-local-dev-key")

stream = client.chat.completions.create(
    model="max",
    messages=[{"role": "user", "content": "FastAPI'yi üç cümlede anlat."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

**Uzantı:** `conversation_id` alanı ile (ve `SESSION_STATELESS=false` iken) aynı upstream
`chat_id` yeniden kullanılır.

---

## Modlar: sohbet · web arama · görsel

Hedef servis üç modu **aynı uçtan** (`create-evaluation`), aynı çerez, aynı
`mode` (`direct-battle`) ve aynı `modelAId` ile sunuyor. Aradaki tek fark
gövdedeki `modality` ve başlıktaki `referer` (15 Kasım ölçümü — iki ayrı
yakalama karşılaştırıldı):

| Mod | `model` | Gövde `modality` | `referer` |
|---|---|---|---|
| Sohbet | `max` | `chat` | cURL'den gelen (`UPSTREAM_REFERER_PATH`) |
| Web arama | `web` | `search` | `/search/direct` |
| Görsel *(ikincil, vitrin dışında)* | `image` | `image` | `/image/direct` |

Bu yüzden **üç ayrı cURL gerekmez**: tek `UPSTREAM_CURL` üç modu da taşır.
Modu seçmek OpenAI şemasını bozmaz — istemci yalnızca `model` adını değiştirir,
böylece Continue / Cursor / Open WebUI / OpenAI SDK olduğu gibi çalışır:

```bash
curl -s "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"web","messages":[{"role":"user","content":"güncel haberler"}]}'
```

Takma adlar da geçerlidir: `search` / `web-search` / `ara` → `web`;
`gorsel` / `görsel` / `img` → `image`.

**Web arama yanıtı**nın sonunda `Kaynaklar:` bloğu görünür: akışın `a2`
kanalındaki atıf parçaları (hedef bugün kaynak **adı** gönderiyor:
`organization`) yanıtın sonuna liste olarak eklenir. Yalnız `modality=search`'te —
7/24 çalışan düz sohbetin biçimi bilerek değişmez (ama atıflar orada da
`sources_seen` olarak loglanır). Hedef ileride adres de gönderirse satırlar
kendiliğinden `- [başlık](adres)` biçimine geçer.

### İkincil mod: görsel (deneysel — vitrin dışında)

Görsel modu **silinmedi, ana akışın dışına alındı**: `config/models.yaml`'da
`listed: false` taşıdığı için `GET /v1/models` listesinde görünmez (Open WebUI /
Cursor açılır listesi çalışmayan bir modu önermez), ama `model=image` açıkça
istendiğinde çözülür ve çalışır. Sebep 15 Kasım canlı ölçümü: görsel akışı
düzeltildikten sonra upstream bu modu üç denemede de `429 {"error":"prompt
failed"}` ile reddetti; sohbet ve web arama aynı kimlikle sorunsuz çalıştı.
Durum değişirse tek satır yeterli: `listed: true`.

İletim yolu hazır duruyor: hedef görseli metin delta'sı olarak değil
`a2:[{"type":"image","image":"<imzalı R2 adresi>","mimeType":"image/jpeg"}]`
parçasıyla gönderiyor (öncesinde `[{"type":"heartbeat"}]` geliyor); wrapper bunu
içeriğe `![görsel](<adres>)` markdown'ı olarak ekliyor — şema ve uç noktalar
değişmiyor. Telegram `/gorsel` adresi `sendPhoto` ile gerçek fotoğrafa çeviriyor,
`scripts\windows\chat.ps1 -OpenImage` tarayıcıda açıyor. Adres **imzalı ve ~1 saat
ömürlü** (`X-Amz-Expires=3600`): saklanmaz, loglanmaz. Kök neden ve kurallar:
`docs/LESSONS_2026_11_14.md §10`.

**Yeni mod eklemek** `config/models.yaml`'a kayıt eklemektir, kod değişikliği
değildir:

```yaml
  - id: web
    modality: search          # gövdeye birebir bu değer yazılır
    referer_path: /search/direct   # modality ile tutarlı olmalı
```

`upstream_id` yazılmazsa belge düzeyindeki ortak `modelAId` miras alınır; böylece
`scripts/curl_to_env.py` tek satırı güncellediğinde **üç mod birden** tazelenir.
Aynı kural çalışma zamanında da geçerlidir: `UPSTREAM_CURL` ile açılış ve
`POST /v1/admin/upstream/curl` (Telegram `/curl`) gövdeden çıkan `modelAId`'i
miras alan kayıtların hepsine yazar — yanıt `models_bound` alanında hangi
modların bağlandığını listeler. İstek gövdesindeki kimlik kayıt defterinden
geldiği için bu adım atlanırsa sohbet tazelenirken `web`/`image` dosyadaki bayat
id'yi gönderir; istemci 200 görür, cevaplayan model yanlış olur.
Bir mod farklı bir `modelAId` isterse o kayda kendi satırını yaz (kayıt düzeyi
önceliklidir). Bilinmeyen bir `modality` değeri reddedilmez, yalnızca uyarı
loglanır: hedef yeni bir mod eklerse kayıt defteri onu olduğu gibi geçirir.

**Doğrulama:** `python scripts/compare_curl.py request.txt` artık yalnız alan
*adlarını* değil `modality` ve `mode` **değerlerini** de karşılaştırır ve
tarayıcının modunu `models.yaml`'daki hangi kaydın karşıladığını söyler.
Eski sürüm `modality` alanını her iki tarafta da gördüğü için "EŞLEŞTİ" diyordu;
`chat` yerine `search` gönderilmesi gerektiği oradan görünmüyordu.

> **Görsel modunun çıktısı henüz iletilmiyor.** Akışta istemciye yalnızca metin
> delta'ları (`0:`) aktarılır (`_CLIENT_VISIBLE_EVENTS`); görsel verisi metin
> olarak gelmiyorsa yanıt boş görünür. Görsel modunu uçtan uca kullanmadan önce
> gerçek bir `create-evaluation` yanıt akışının görülmesi gerekir.

---

## 404 / yanlış uç

İki farklı 404 vardır; karıştırmayın:

| Kaynak | Belirti | Çözüm |
|---|---|---|
| Yerel API | `Unknown endpoint … /chat/completions` | `base_url` olarak `http://localhost:8000` veya `http://localhost:8000/v1` kullanın (ikisi de çalışır). Bilinmeyen model `model_not_found` döner. |
| Upstream | `upstream_not_found` / logda `HTTP 404` | `UPSTREAM_STREAM_PATH` tarayıcıdaki gerçek yolla uyuşmuyor. Mobilde önce Telegram `/yenile` deneyin; bot `Akış yolu: yakalandı` derse yeni yol uygulanmıştır. Yakalanamazsa sohbet POST isteğinin tam cURL'ünü `/curl <cURL>` ile gönderin; bilgisayarda `python scripts/curl_to_env.py request.txt --write` kullanılabilir. Yeni uçlar genelde `/nextjs-api/stream/create-evaluation` kullanır ve `chat_id`'yi URL'ye koymaz. |

`python scripts/compare_curl.py request.txt` artık **URL yolunu** da karşılaştırır; buradaki `FARKLI` satırı 404'ün doğrudan nedenidir.

## Tek hesaplı cURL içe aktarma

Windows `setup-from-curl.ps1` tek hesaplıdır: her içe aktarma eski hesap yuvalarını
temizler ve güncel cURL'ü yalnız 1. hesaba yazar. Aynı davranış CLI'de
`--single-account` ile alınır:

```bash
python scripts/curl_to_env.py request.txt --write --single-account
```

Fresh `.env.example` browser Enterprise CAPTCHA kullanır. Importer `browser`,
`external` veya `noop` seçimini static'e çevirmez ve kısa ömürlü CAPTCHA token'ını
bu kiplerde saklamaz. Eski `.env`'i tamamen yenilemek için önce yeni örneği kopyalayın.

## Render'da tek secret: `UPSTREAM_CURL`

Render'da ayrı ayrı `TARGET_DOMAIN`, `UPSTREAM_COOKIE`, `UPSTREAM_ACCESS_TOKEN`, yol ve
header alanlarını doldurmak yerine aşağıdakini yapın:

1. Max'te bir mesaj gönderin ve yanıtın tamamlanmasını bekleyin.
2. DevTools → Network'te `create-evaluation` (veya kullanılan gerçek sohbet akışı)
   POST isteğini bulup **Copy as cURL** deyin.
3. Render → Environment'ta `UPSTREAM_CURL` secret'ına komutun tamamını yapıştırın.
   Cookie, token veya cURL'ü sohbete, dosyaya ya da `render.yaml` içine kopyalamayın.
4. `API_KEYS` ve CAPTCHA ayarlarını bırakın; yeni deploy/restart ile process açılırken
   cURL ayrıştırılır.

`UPSTREAM_CURL` doluysa ayrı upstream alanlarından önceliklidir. `modelAId` varsayılan
`max` modelinin upstream kimliği olarak kullanılır. `RECAPTCHA_PROVIDER=browser` veya
`external` seçiliyse o seçim korunur ve cURL'deki CAPTCHA token'ı kaydedilmez; `static` seçiliyse cURL'deki `recaptchaV3Token` `RECAPTCHA_STATIC_TOKEN` olarak alınır. Telemetri isteği, eksik URL
veya cookie/authorization içermeyen cURL başlangıçta güvenli bir hatayla reddedilir.
Ham cURL loglanmaz; yalnızca uygulanan alan adları ve model bulunup bulunmadığı loglanır.
Eski `_2`/`_3`/`_4` hesap secret'ları varsa bu tek-cURL kipinde kullanılmaması için silin;
process ayarları sıcak yenilemediğinden env değişikliğinden sonra restart gerekir.

Yerel `.env` için çok satırlı cURL'ü elle yazmak yerine yukarıdaki
`scripts/curl_to_env.py --write --single-account` importer'ını kullanın.

## Birden çok hesabı cURL ile kaydetme

Çoklu hesap gerekiyorsa Python CLI'nin varsayılan yuva davranışı kullanılabilir;
`curl_to_env.py` her çalıştırmada **bir hesap yuvasına** yazar. cURL'leri sırayla atın:
1. cURL soneksiz anahtarlara (`UPSTREAM_COOKIE`, `RECAPTCHA_STATIC_TOKEN`, …),
2. cURL `_2` soneksli anahtarlara (`UPSTREAM_COOKIE_2`, `RECAPTCHA_STATIC_TOKEN_2`, …).
Hiçbir hesap diğerini ezmez; `models.yaml` yalnızca 1. hesapta güncellenir (model
kimlikleri hesap başına değildir).

```bash
python scripts/curl_to_env.py hesap1.txt --write     # -> 1. yuva
python scripts/curl_to_env.py hesap2.txt --write     # -> 2. yuva
python scripts/curl_to_env.py - --list-accounts      # kayıtlı hesapları göster
python scripts/curl_to_env.py hesap1.txt --write --account 1   # 1. yuvayı tazele
python scripts/curl_to_env.py - --reset-accounts     # tüm yuvaları temizle
```

En fazla **4 hesap** desteklenir; dolu havuza 5. hesap eklenmeye çalışılırsa araç dosyaya
yazmadan hata verir. Numarayla tazeleme var olan özel hesap adını korur. Cookie-only bir
cURL ile tazeleme eski `UPSTREAM_ACCESS_TOKEN` değerini, authorization-only cURL ile
tazeleme ise eski cookie'yi temizler; böylece iki oturumun kimlikleri karışmaz.
2. veya sonraki hesapları eklemek global `RECAPTCHA_PROVIDER` seçimini değiştirmez.
Ayar dosyası çalışma sırasında yeniden okunmadığından, değişiklikten sonra servisi
yeniden başlatın.

Araç artık cURL'deki `authorization` başlığını da yazıyor (`UPSTREAM_ACCESS_TOKEN` +
`UPSTREAM_AUTH_SCHEME`, `UPSTREAM_AUTH_FROM_COOKIE=false`). Başlık yoksa istek yalnızca
Cookie ile gider ve bunu belirten bir uyarı üretilir; ne başlık ne oturum çerezi varsa
"istek anonim gider" uyarısı çıkar. `referer`'dan da `UPSTREAM_REFERER_PATH` şablonu üretilir.

Kaydedilen hesaplar hesap havuzu tarafından okunur ve istekler aralarında dağıtılır
(aşağıda). Yuvalar `Settings` içinde açıkça tanımlıdır; 4 hesap desteklenir
(`upstream_account_name`, `_2`, `_3`, `_4`).

## Hesap havuzu ve kota yönetimi

Upstream bir hesabı kayan pencerede çok mesaj atınca kilitliyor ve bunu bir metinle
bildiriyor. Havuz bunu şöyle yönetir:

```bash
ACCOUNT_MSG_BUDGET=15               # hesap başına yumuşak bütçe (pencere içi mesaj)
ACCOUNT_QUOTA_WINDOW_SECONDS=1200   # kota penceresi (20 dk)
ACCOUNT_COOLDOWN_SECONDS=1200       # kilit algılanan hesabın dinlenme süresi
ACCOUNT_MAX_SWITCHES=2              # tek istekte en fazla kaç hesap değiştirilsin
ACCOUNT_BUDGET_GROWTH_STREAK=3      # bütçenin +1 artması için gereken temiz pencere
```

- **Seçim:** penceresi en boş hesap tercih edilir. Bütçe bir *engel* değil *tercih
  sırası*dır — tek hesaplı kurulumda istek asla reddedilmez.
- **Kilit:** `upstream limit reached` algılanınca o hesap `ACCOUNT_COOLDOWN_SECONDS`
  boyunca dinlendirilir ve istek **aynı istekte** başka hesapla yeniden denenir
  (yalnızca istemciye henüz tek bir parça gönderilmediyse; aksi halde iki yanıt
  birbirine karışırdı).
- **AIMD:** bir hesap pencere içinde N. mesajda kilitlendiyse gerçek sınır en fazla
  N-1'dir → öğrenilen tavan `N-1` olur (çarpan azaltma). Bütçe kadar mesaj kilit
  olmadan geçilince tavan +1 artar (toplama artırma), ama her mesajda değil —
  `ACCOUNT_BUDGET_GROWTH_STREAK` kadar temiz pencere gerekir.
- **Captcha:** `RECAPTCHA_PROVIDER=static` iken her hesap **kendi**
  `RECAPTCHA_STATIC_TOKEN` değerini kullanır; token tarayıcı oturumuna bağlı olduğu için
  hesaplar arası paylaşılmaz. Token ~2 dk ömürlüdür, yani bu kipte her hesabın cURL'ünü
  düzenli tazelemeniz gerekir. `browser`/`external` seçildiğinde hesaplardaki eski statik
  değerler sağlayıcıyı ezmez; sağlayıcıdan alınan taze token kullanılır. `noop` ve `static`
  kipinde reddedilen token aynı değerle tekrar denenmez.
- **Sohbet:** upstream sohbeti hesaba bağlıdır; hesap değişince başka hesabın
  sohbetine yazılmaz.

Durum ve öğrenilen sınırlar:

```bash
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/v1/admin/accounts
curl -X POST -H "Authorization: Bearer $API_KEY" \
     http://localhost:8000/v1/admin/accounts/2/reset   # dinlenmeyi elle sıfırla
```

## reCAPTCHA sağlayıcıları

```bash
RECAPTCHA_PROVIDER=static     # KALICI. RECAPTCHA_STATIC_TOKEN zorunlu (ölçüm: 2+ saat geçerli)
RECAPTCHA_PROVIDER=noop       # upstream doğrulamıyorsa / testler
RECAPTCHA_PROVIDER=browser    # ⚠️ Bu hedefte ÇALIŞMIYOR: 403 recaptcha validation failed
RECAPTCHA_PROVIDER=external   # Tek istekte doğrudan token döndüren özel HTTP çözücü
```

> **`browser` neden çalışmıyor:** sunucudaki Chromium'un ürettiği token düşük skorlu
> sayılıyor. Aynı çerez, gövde, site key ve action ile gerçek tarayıcıdan alınmış
> statik token geçiyor. Ölçüm ve ayrıntı: [`docs/LESSONS_2026_11_14.md`](docs/LESSONS_2026_11_14.md).

Yerel Python kurulumunda `browser` için: `pip install ".[browser]" && playwright install chromium`.
Gözlenen hedef **reCAPTCHA Enterprise** kullandığından örnek yapılandırmada
`RECAPTCHA_VERSION=enterprise` ve `RECAPTCHA_ACTION=chat_submit` bulunur. Browser
sağlayıcı hedef origin'de izole bir bootstrap belgesine `enterprise.js` yükleyip
`grecaptcha.enterprise.execute()` çağırır. Belge Playwright route ile aynı origin'de
üretilir; canlı hedef sayfasının Cloudflare/CSP veya diğer script'leri bootstrap'a
karışmaz. `/recaptcha/enterprise/reload` protobuf isteğini veya Google cookie'lerini
yeniden oynatmaz. Site key public'tir, üretilen CAPTCHA token'ı ise gizli ve kısa ömürlüdür.
Docker imajı Playwright ve Chromium'u önceden içerir. `external` sağlayıcı tek HTTP
cevabında doğrudan token döndüren bir uç bekler; standart `createTask`/`getTaskResult`
akışları ayrıca adaptör gerektirir.
Upstream token'ı reddederse (403) dinamik sağlayıcıda cache otomatik geçersizleşir ve istek
**bir kez** yeniden denenir.

### Render / Docker

- Depodaki `render.yaml` tek instance, ücretsiz `free` Docker servisi tanımlar;
  ücretli disk veya paid plan gerektirmez.
- `/healthz` yalnızca process liveness, `/readyz` statik dağıtım readiness kontrolüdür.
  Render blueprint'i `/readyz` kullanır; canlı upstream healthcheck içinde denenmez.
- Başlatma komutu Render'ın `PORT` değişkenini, yoksa `8000` portunu kullanır.
- Bellek içi hesap/sohbet durumu nedeniyle **tek instance ve tek worker** kullanılır.
- Free planda kalıcı disk yoktur. Dönen refresh token/çerezlerin restart/deploy
  sonrası korunması için `RENDER_API_KEY` secret'ını girin. `RENDER_SERVICE_ID`
  Render tarafından otomatik sağlanır; token yenileme sonrası env senkronizasyonu
  deploy tetiklemez. API key yoksa sistem çalışır ancak dönen kimlikler restart'ta
  kaybolabilir ve `/cookie` ile tazeleme gerekebilir.
- Free servis hareketsizlikte uyuyabilir; gelen HTTP isteğiyle uyanır. İlk istek
  gecikebilir ve gömülü Telegram botu uyku sırasında mesajları işleyemez. Kod,
  açılışta token'ı hemen denetleyerek uyku sonrası toparlanmayı hızlandırır.
- Güçlü bir `API_KEYS` değeri ve tam sohbet cURL'ünü `UPSTREAM_CURL` secret'ına girin;
  domain, model ID ve upstream kimliği buradan çıkar. Kullanılan CAPTCHA kipinin ayarları
  ayrı environment alanlarında kalır.
- Blueprint `RECAPTCHA_PROVIDER=static` gelir (kalıcı). `RECAPTCHA_SITE_KEY`, Enterprise
  modu ve `chat_submit` action'u hazır gelir. `static` kipinde `RECAPTCHA_STATIC_TOKEN`
  gerekir; cURL yüklemesi onu otomatik doldurur.
- `browser` seçilirse Chromium imajda hazır olsa da **token hedefte reddedilir**.

Hareketsizlikte upstream oturumunun eskiyip eskimediğini sınamak için isteğe bağlı
servis içi keepalive kullanılabilir. Varsayılan `0` kapalıdır; `10800` üç saatte bir
kısa non-stream completion gönderir:

```text
UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=10800
UPSTREAM_KEEPALIVE_MODEL=max
```

Bu yaklaşık 8 gerçek istek/gün ve kota/CAPTCHA kullanımıdır. Aralığa
`UPSTREAM_KEEPALIVE_JITTER_SECONDS` (vars. ±300 sn) kadar rastgele sapma eklenir ve
her probe farklı, kısa bir istem gönderir; böylece tam saatte atılan, birbirinin
aynısı isteklerden oluşan robotik desen oluşmaz. Günde bir kez
(`UPSTREAM_KEEPALIVE_STREAM_SMOKE_INTERVAL_SECONDS`, vars. 86400) aynı kontrol
`stream: true` ile yapılır: non-stream probe akış yolundaki bir kırılmayı göremez.
Render Free uyuduğunda task da durur; servis içi loopback istekleri Free uyku
süresini sıfırlamak için güvenilir bir always-on çözümü değildir. Sonuçlar timestamp'li
`upstream_keepalive_completed`, `upstream_keepalive_failed` ve
`upstream_keepalive_stream_smoke_*` loglarından izlenir.

Oturum düştüğünde telefonda DevTools olmadan kurtarma: Telegram **`/yenile`** →
servisteki Chromium hedef sayfayı açar, gerçek `create-evaluation` isteğinden
cookie + `dpl_...` sürüm damgası + `cx` + referer yolunu yakalar ve çalışan
ayarlara uygular (`POST /v1/admin/upstream/capture`). Oturum tamamen düşmüşse
araç bunu açıkça söyler; o zaman elle cURL gerekir.

Mobilde çerez/cURL üretmenin mümkün olup olmadığı ve adımları:
**[`docs/MOBILE_CURL.md`](docs/MOBILE_CURL.md)**.

Neden sürekli çerez taşındığı, hangi değerin ne zaman tazelendiği ve kalan yol
haritası tek sayfada: **[`docs/BIG_PICTURE.md`](docs/BIG_PICTURE.md)**.

> **⚠️ Bu kurulumda otomatik yenileme KAPALI** (`UPSTREAM_REFRESH_ENABLED=false`,
> kalıcı). Yenileme ucu Supabase `apikey` istiyor; o anahtar elde edilemiyor.
> Ayrıca ölçümde token yaşının pratikte bağlayıcı olmadığı görüldü (aynı çerezle
> 15+ saat). Aşağıdaki akış kod olarak durur, kullanılmaz.

Erişim token'ı yenilendiğinde wrapper bunu **kendisi** yapar: oturum
çerezindeki `refresh_token` ile `grant_type=refresh_token` isteği atılır (token'ın
bitmesine 10 dk kalınca, 60 sn'de bir denetlenir), yeni token hem bellekteki
ayarlara hem oturum çerezine yazılır. Elle tetik: Telegram **`/token`** veya
`POST /v1/admin/upstream/refresh`. Ayrıntı ve ayarlar:
[`docs/COOKIE_LIFECYCLE.md`](docs/COOKIE_LIFECYCLE.md) §5.

Elle uygulamak için: Telegram `/cookie <ad=değer; ...>` (Cookie-Editor'ün
*Header String* **veya** JSON çıktısı) ya da `/curl <cURL>`; API karşılığı
`POST /v1/admin/upstream/cookie {"cookie": "..."}` ·
`POST /v1/admin/upstream/curl {"curl": "..."}`. Değişiklik çalışan servise hemen uygulanır; yeni istekler taze client/cookie
jar'ına geçer, açık akışlar eski client üzerinde güvenle tamamlanır ve oturum
önbelleği temizlenir. Kimlik durumu `UPSTREAM_STATE_FILE` altına, yapılandırıldıysa
Render env'e de yazılır. En net dağıtım geçmişi için Render'daki `UPSTREAM_CURL`
secret'ını da güncellemek önerilir.

`/cookie` artık **önce inceler** (`POST /v1/admin/upstream/cookie?dry_run=true`):
gelen çerez adları, `session_cookie_found`, `access_token_found/expired` ve
`safe_to_apply` döner; hiçbir değer yanıta yazılmaz ve canlı ayar değişmez.
Oturum çerezi içermeyen bir set bot tarafından **uygulanmaz** (çalışan oturumu
ezmesin); bilinçli olarak zorlamak için `/cookie! <çerez>`.

Telegram `/ping` yalnızca `/healthz` demez; **son upstream kontrolünün** ne zaman
ve hangi sonuçla çalıştığını da yazar (`✓ ... başarılı (HTTP 200)` /
`⚠ ... BAŞARISIZ (HTTP 409, upstream_refresh_required)`). Böylece `/healthz` 200
dönerken upstream oturumu ölmüş olsa bile bu tek komutla görülür.

`refresh_required`/`forced_update` (HTTP 409) artık çoğunlukla **kendiliğinden**
geçer: wrapper sürüm damgasını (`x-deployment-id`, `x-arena-web-deployment`)
upstream'in kendi sayfasından okuyup tazeler, 409 alırsa isteği bir kez taze
damgayla, olmadı eskimiş başlıkları **hiç göndermeden** tekrar eder. Ayrıntılar ve kalan elle müdahale senaryosu:
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

Dağıtım sırası, istek yaşam döngüsü, readiness hata kodları ve belirti→çözüm tablosu:
[`docs/RENDER.md`](docs/RENDER.md).

## Telegram ile ara sıra test

Ek webhook veya üçüncü parti bot paketi gerekmez. `app.telegram.bot`, Telegram long
polling ile mesajı wrapper'ın **non-streaming** `/v1/chat/completions` ucuna gönderir.
Bot token'ı ve wrapper anahtarı yalnızca environment değişkenlerinden okunur.

### Aynı Web Service içinde (ayrı BG worker'sız)

Web Service environment'ına şu iki alanı ekleyin:

```text
TELEGRAM_BOT_TOKEN=<BotFather tokenı>
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Token varsa bot FastAPI lifespan ile birlikte açılır ve aynı process'te long polling
yapar. `WRAPPER_BASE_URL` kendi `PORT` değerinden, `WRAPPER_API_KEY` mevcut
`API_KEYS` ilk değerinden otomatik alınır; bunları Web Service'e ayrıca eklemeyin.
`UPSTREAM_CURL`, CAPTCHA ayarları ve `API_KEYS` aynı serviste kalır. Deploy sonrası
Web Service loglarında `telegram_bot_started` görünmelidir. Tek instance/tek worker
kullanın; birden fazla bot process'i Telegram 409 çakışması üretir.

### Yerel veya ayrı process

```powershell
$env:TELEGRAM_BOT_TOKEN = "<BotFather tokenı>"
$env:WRAPPER_BASE_URL = "https://SERVICE.onrender.com"  # sonda /v1 yok
$env:WRAPPER_API_KEY = "<API_KEYS içindeki tek anahtar>"
$env:TELEGRAM_ALLOWED_CHAT_IDS = ""                 # ilk kurulum: wrapper kapalı
python -m app.telegram.bot
```

Telegram'da `/start` yazın; bot chat ID'nizi gösterir. Bu ID'yi
`TELEGRAM_ALLOWED_CHAT_IDS=123456789` olarak ayarlayıp botu yeniden başlatın. Allowlist
boşken hiçbir mesaj wrapper'a iletilmez; allowlist dışındaki sohbetler de yok sayılır.
Sonra normal bir metin gönderin veya `/ping` ile wrapper health kontrolünü deneyin.
`/web <soru>` aynı soruyu **web arama modunda** sorar (`model=web`, `modality=search`);
düz metin sohbet modunda kalır. Model adı `TELEGRAM_WEB_MODEL` ile değiştirilir.
`/help` komutları listeler. Konuşma geçmişi tutulmaz; her mesaj bağımsızdır ve yanıtlar
Telegram sınırına göre parçalanır.

*Deneysel (ana akışın dışında):* `/gorsel <istem>` görsel üretmeyi dener ve gelirse
`sendPhoto` ile **fotoğraf** olarak gönderir (`TELEGRAM_IMAGE_MODEL`). Upstream bu modu
`429 {"error":"prompt failed"}` ile reddedebiliyor — bkz. "İkincil mod: görsel".

Bot normal (non-streaming) yolu test eder; SSE/stream regresyonu için ayrıca
`scripts/test_client.py --model max` çalıştırın. 429 yanıtı upstream rate limit olarak
görünür; restart veya cookie yenileme çözümü değildir. Free Web Service uyku moduna
geçerse gömülü bot da durur; Telegram mesajı uyuyan servisi uyandırmaz. Kesintisiz bot
için isteğe bağlı ayrı Render **Background Worker** kullanılabilir: Runtime `Docker`,
Dockerfile Path `./Dockerfile.telegram`, Build/Start/Docker Command boş olmalıdır.
Ayrı worker'da dört Telegram/wrapper env değeri ve Web Service kök URL'si gerekir.
Gerçek Telegram ve Render testleri bu ortamda token/secret olmadan çalıştırılmaz.

---

## Başka projelere açmak (proje anahtarları)

Servis OpenAI-uyumlu olduğu için bir tüketici yalnız `base_url` + `api_key`
değiştirir. Her projeye **ayrı anahtar** ver; böylece kullanımı proje bazında
görürsün ve bir projeyi diğerlerini etkilemeden iptal edersin.

```powershell
$B = "https://apiwrapper" + ".onrender.com"
$A = "ADMIN_ANAHTARIN"

# 1. Anahtar üret — değer yalnız bu yanıtta görünür, kaydet
Invoke-RestMethod -Method Post -Uri "$B/v1/admin/keys" `
  -Headers @{ Authorization = "Bearer $A" } -ContentType "application/json" `
  -Body (@{ label = "mobil uygulama" } | ConvertTo-Json)

# 2. Kullanımı oku (proje başına istek / token / hata)
Invoke-RestMethod -Uri "$B/v1/admin/keys" -Headers @{ Authorization = "Bearer $A" }

# 3. İptal et (anında etkili, son anahtar iptal edilemez)
Invoke-RestMethod -Method Delete -Uri "$B/v1/admin/keys/cli_abc123def456" `
  -Headers @{ Authorization = "Bearer $A" }
```

Kurallar:

- **Önce `ADMIN_API_KEYS` ayır.** Boşsa her `API_KEYS` anahtarı admin sayılır
  (geriye dönük uyumluluk) ve tüketiciye verdiğin anahtar `/v1/admin/*`
  uçlarına da erişir. `GET /v1/admin/keys` bunu gizlemez: ayrım yoksa
  `admin_separation: false` ve her satırda `is_admin: true` döner, her üretimde
  de log'a `project_key_minted_without_admin_separation` düşer.
- Anahtar **değeri loglanmaz ve listelenmez**; her yerde ya maskeli (`sk-***GI`)
  ya parmak izi (`cli_<sha256[:12]>`) olarak görünür. Etiketler `API_KEY_LABELS`
  alanında taşınır ve durum dosyası + Render env'i ile kalıcı olur.
- Bu katman **yalnız muhasebe**dir: model izni, günlük bütçe ve kuyruk yok.
  Koruma anahtar başına hız sınırıdır (`RATE_LIMIT_RPM`/`RATE_LIMIT_BURST`,
  varsayılan 120/30).
- Sayaçlar bellek içidir, restart'ta sıfırlanır; kalıcı rapor için `/metrics`
  (`apiwrapper_project_*`) dışarıda toplanır.
- Tüketiciye vereceğin rehber: [`docs/CONSUMERS.md`](docs/CONSUMERS.md).

## İşletim

```bash
make test     # parser, adapter, çekirdek ve uçtan uca API regresyonları
python scripts/test_client.py  # canlı servise karşı kontroller
make lint     # ruff
make docker   # docker compose up --build
```

- `GET /health` → devre kesici durumu, model sayısı, oturum sayısı
- `GET /healthz` → basit liveness; `GET /readyz` → statik dağıtım readiness
- Streaming başarı/hatası `stream_completed`/`stream_failed` JSON loglarında ayrılır;
  HTTP 200 ile başlayan SSE içindeki upstream hatası `test_client.py` çıktısında kod ve
  mesajıyla gösterilir. Ayrıntı: [`docs/RENDER.md`](docs/RENDER.md#8-json-logları-ve-sse-hata-ayıklama)
- `GET /metrics` → Prometheus (`apiwrapper_time_to_first_token_seconds` dahil)
- `POST /v1/admin/breaker/reset`, `/v1/admin/recaptcha/invalidate`, `/v1/admin/sessions/clear`
- `GET /v1/admin/config` → hassas olmayan etkin yapılandırma
- `GET /v1/admin/auth` → maskeli token teşhisi; diğer hesaplar için `?slot=2`, `?slot=3`, `?slot=4`
- `POST /v1/admin/keys {"label":"proje adı"}` → tüketici anahtarı üretir (redeploy yok);
  `GET /v1/admin/keys` → proje başına istek/token/hata; `DELETE /v1/admin/keys/{key_id}` → iptal.
  Ayrıntı: [`docs/CONSUMERS.md`](docs/CONSUMERS.md)

### Ayar politikaları

`UNSUPPORTED_PARAMS` upstream'in desteklemediği (`temperature`, `max_tokens`, `tools` …)
parametrelerin davranışını belirler:

| Değer | Davranış |
|---|---|
| `ignore` (varsayılan) | Sessizce yok sayılır |
| `hint` | Prompt'a doğal dil ipucu olarak eklenir |
| `error` | 400 ile reddedilir |

---

## Sık yapılan hatalar (bunları tekrar yapma)

14-15 Kasım 2026 ölçümlerinden çıkan dersler. Ayrıntı:
[`docs/LESSONS_2026_11_14.md`](docs/LESSONS_2026_11_14.md).

| # | Hata | Doğrusu |
|---|---|---|
| 1 | Sunucudan reCAPTCHA token'ı üretmek (`RECAPTCHA_PROVIDER=browser`) | **Reddediliyor.** `static` kullan. Aynı çerez/gövde ile `browser` → `403`, `static` → ✅ |
| 2 | Çerez "3 saatte ölüyor" sanıp sürekli cURL almak | Öldüren şey **tarayıcıya giriş yapmak**. Dokunmazsan **15+ saat** dayanır |
| 3 | `401 User not found` görünce hemen yeni cURL aramak | Önce **tarayıcıya girmeden** bekle; kod çerezi silmez, genelde kendi döner |
| 4 | cURL almak için tarayıcıda giriş yapıp **çıkış yapmamak** | Al, sonra **çıkış yap** (ya da ayrı hesap kullan). Yoksa sunucudaki çerez ölür |
| 5 | 2-14 Kasım arası değişiklikleri suçlamak | 1 Kasım sürümü **de** aynı `recaptcha validation failed` hatasını veriyor. Sorun kodda değil |
| 6 | Docker build'inde `playwright install --with-deps` | `libatomic.so.1` eksikliği → **`exit 127`**. Bugünkü Dockerfile kullanılır |
| 7 | "Token 20 dakikada ölür" bilgisine güvenmek | **Eski/yanlış bilgi.** Ölçüm: reCAPTCHA token 2+ saat, çerez 15+ saat |

---

## Kayıt: web arama modu nasıl kodlanmıştı (bu sürümde **yok**)

> **Durum:** PR #49 (`e498149`, 15 Kasım 15:21 UTC) ile eklendi, **canlıda çalıştı**,
> 16/17 Kasım geri dönüşünde kaldırıldı. Bu bölüm bilerek silinmedi: hem "böyle
> kodladık, çalıştı" kaydı olsun, hem ileri dönüşte buradan kurulsun diye.
> Ayrıntılı sürüm: [`docs/ROLLBACK_2026_11_16.md §7.1`](docs/ROLLBACK_2026_11_16.md).

### Dayandığı ölçüm (15 Kasım)

chat, web arama ve görsel istekleri **aynı uca** (`create-evaluation`), **aynı
çerez**, **aynı `mode`** (`direct-battle`) ve **aynı `modelAId`** ile gidiyordu.
Aradaki tek fark gövdedeki **`modality`** ve başlıktaki **`referer`**:

```
chat   → modality: chat    · referer: cURL'den gelen (UPSTREAM_REFERER_PATH, ör. /c/{chat_id})
web    → modality: search  · referer: /search/direct
görsel → modality: image   · referer: /image/direct
```

Bu yüzden **üç ayrı cURL gerekmiyordu**; tek `UPSTREAM_CURL` üç modu da taşıyordu.
İstemci modu OpenAI şemasını bozmadan **`model` alanıyla** seçiyordu: `model=web`.

### Kod

**`config/models.yaml`** — `modelAId` belge düzeyinde bir kez yazılır, kayıtlar
miras alır (tek satır güncellenince üç mod birden tazelenir):

```yaml
default: max
upstream_id: 019b24bb-5caf-71c3-b854-37d0c7086f21

models:
  - id: max
    owned_by: arena
    modality: chat
    # referer_path bilerek YOK — bkz. aşağıdaki "pinleme" kuralı
    aliases: [max-auto, chat]

  - id: web
    owned_by: arena
    modality: search
    referer_path: /search/direct
    description: Web arama modu — gövdede modality=search ile çağrılır.
    aliases: [search, web-search, ara]
```

**`app/services/model_registry.py`** — `ModelEntry`'ye üç alan:

```python
modality: str = DEFAULT_MODALITY   # gövdedeki `modality`
referer_path: str = ""             # boşsa UPSTREAM_REFERER_PATH kullanılır
listed: bool = True                # False → vitrinden düşer ama çalışmaya devam eder
```

**`app/upstream/headers.py`** — referer mod başına:

```python
"referer": settings.referer_url(chat_id, referer_path)
```

**`app/adapters/openai_to_upstream.py`** — gövdeye yazım:

```python
modality=resolved_modality,
referer_path=(referer_path or "").strip(),
```

**`app/upstream/stream_parser.py`** — kaynaklar:

```python
_SOURCE_LABEL_KEYS = ("title", "organization", "name", "source", "site", "publisher")
_SOURCE_URL_KEYS   = ("url", "uri", "link", "href")

extract_citations(data) -> list[Citation]   # görsel ve heartbeat parçaları elenir
render_sources(citations, heading="Kaynaklar") -> str
```

Blok **yalnız `modality=search`** yanıtının sonuna ekleniyordu
(`Kaynaklar:\n- Google\n- YouTube\n`). Sohbet çıktısı **bayt-özdeş** kalıyor, ama
kaynaklar her modda **sayılıp** `sources_seen` olarak loglanıyordu. Ölçülen gerçek:
hedef yalnız kaynak **adı** gönderiyordu (alan `organization`); `url` gelirse biçim
kendiliğinden markdown bağlantısına zenginleşecekti. OpenAI şeması hiç değişmedi.

### Bu işten çıkan iki kural

1. **Kanıtlanmış değeri pinleme, cURL'den gelsin.** Sohbet kaydına
   `referer_path: /text/direct` yazmak, kayıt düzeyinde env'i ezdiği için
   **7/24 çalışan sohbeti 403'e düşürdü** (PR #50 bunu geri aldı). Arama ve
   görsel için elimizde çalışan bir temel yoktu; o yollar doğrudan ölçümden geldi.
2. **Hedef bir modu reddediyorsa modu silme, vitrinden indir.** Görsel mod
   `429 {"error":"prompt failed"}` almaya başlayınca `listed: false` yapıldı;
   `model=image` açıkça istendiğinde ve Telegram `/gorsel` ile çalışmaya devam etti.

### Açık kalan

`a2` kanalında `organization:str(6)|str(7)|str(9)` biçiminde 3-5 parça, `ag`
kanalında `str(3..10)` biçiminde **22-33 parça** ölçüldü; `ag` **sınıflandırılmadı**.
Sohbet modunda bile `sources_seen=2` görüldü — yani kaynak üretimi moddan
bağımsız çalışıyordu, yalnız iletimi moda bağlıydı.

---

## Yasal Uyarı

Bu araç yalnızca eğitim ve birlikte çalışabilirlik amaçlıdır. Hedef servisin
kullanım koşullarına (ToS), oran sınırlarına ve yürürlükteki mevzuata uyum
tamamen kullanıcının sorumluluğundadır.

## Teslimat Raporu

Tam teknik teslimat özeti (klasör ağacı, kritik dosya kodları, konfigürasyon,
çalıştırma/test talimatları, edge-case ve risk analizi):
**[`docs/DELIVERY_REPORT.md`](docs/DELIVERY_REPORT.md)**
