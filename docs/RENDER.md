# Render aktif çalışma runbook'u

Bu belge sistemi dağıtımdan cevaba kadar sırayla yürütür. Amaç yalnızca container'ı
açmak değil, **hangi aşamanın çalıştığını ve hatanın nerede başladığını** ayırt
edebilmektir.

> **Hangi dal?** `render.yaml` dal seçmez; Render servis ayarlarındaki production
> dal kullanılır. Canlı servisin izlediği dalı Render panelinden doğrulayın.
> Geçmiş merge kayıtları [`MERGE_TARGET.md`](MERGE_TARGET.md) içindedir.

> ## ⚠️ SÜRÜM NOTU (19 Kasım 2026) — önce bunu oku
>
> Bu runbook `77d4011` sürümü için yazıldı. Canlıdaki kod bir **geri dönüş**
> sürümüdür ([`ROLLBACK_2026_11_16.md`](ROLLBACK_2026_11_16.md)); aşağıdakilerin
> bir kısmı bu sürümde yok.
>
> **Bu sürümde OLMAYANLAR** (metinde geçtikleri yerde ayrıca işaretlendi):
> `RENDER_API_KEY` ile kalıcılık (`render_env_synced` logu), otomatik token
> yenileme, Telegram `/cookie` `/yenile` `/token` `/apikey`, `UPSTREAM_CX_SEND`.
> **§9'un tamamı** bu sürümde geçersizdir.
>
> **Bu sürümde geçerli olan tek kalıcılık kuralı:** dönen çerez yalnız bellekte
> yaşar; her restart env'deki `UPSTREAM_CURL`'e döner, o cURL o anda bayattır ⇒
> **restart = oturum ölümü**. cURL'ü tazelemek = Render → Environment →
> `UPSTREAM_CURL` → yeniden deploy (`ROLLBACK §6`, `§12.1`).
>
> **Canlı servisin gerçek durumu (operatör, 19 Kasım):**
>
> | | Belgede / dosyada | Canlıda |
> |---|---|---|
> | Plan | Bu belge ve `render.yaml` "free" diyor (`render.yaml` geri dönüşten 19 Kasım'a dek `starter` yazıyordu; operatör kararıyla `free` yapıldı) | **Free** |
> | Uyku | "hareketsizlikte uyur" | Dışarıdan düzenli istek atan bir zamanlayıcı (**cron-job.org**) uyanık tutuyor; 2 haftadan uzun süredir uyuduğu görülmedi. Loglardaki 5 dakikada bir `GET /` büyük olasılıkla budur (`ROLLBACK §12.8`, 6. soru) |
> | `RECAPTCHA_PROVIDER` | `render.yaml` geri dönüşten beri `browser` yazıyordu; 19 Kasım'da `static` yapıldı | **`static`** |
>
> `render.yaml` yalnız bir şablondur (Blueprint): canlı değerler Render
> panosundadır. Şablon 19 Kasım'dan beri canlıyla aynı planı (`free`) yazar;
> ondan **yeni** servis kurulursa o da Free açılır. `UPSTREAM_MODE` şablonda
> tanımlı değildir ve gerekmez: `UPSTREAM_CURL` doluyken `mode` ve akış yolu
> cURL'den okunur, env değerleri okunmaz (`ROLLBACK §6`, 19 Kasım düzeltmesi).
>
> **Free planın, Render'ın kendi belgesinde yazan ve bu servisi ilgilendiren
> sınırları** ([render.com/docs/free](https://render.com/docs/free); 19 Kasım'da
> okundu, bu kurulumda **gözlenmedi**). Hepsi aynı yere çıkar: süreç yeniden
> başlarsa oturum ölür.
>
> | Render'ın yazdığı | Bu servis için anlamı |
> |---|---|
> | 15 dakika istek almayan Free servis **uyur**; sonraki istekte ~1 dakikada açılır | Uyanış yeni süreçtir. Uyanık tutan şey dışarıdaki zamanlayıcıdır (cron-job.org); durursa oturum gider |
> | *"Render might restart a Free web service at any time."* | Elimizde olmayan bir restart kaynağı. Bir oturum, taze cURL'e rağmen kısa sürede `401`'e düşerse Render → **Events** sayfasında o saatte bir restart var mı diye bak |
> | Çalışma alanı başına ayda **750 Free saat**; biterse Free servisler ay sonuna kadar **askıya alınır** | 7/24 çalışan tek servis en çok 744 saat kullanır: sığar. Aynı çalışma alanında sürekli çalışan **ikinci** bir Free web servisi bu payı ay bitmeden tüketir |
> | Free serviste kabuk (shell) erişimi ve kalıcı disk yoktur | Çalışan imajın içine girip bakılamaz; imajın içeriği yalnız **derleme logundan** okunur (aşağıda) |
>
> **İmaj sabit sürümlerle derlenir (19 Kasım; PR #56, 20 Kasım'da birleştirildi).**
> Taban imaj özetle (`python:3.12-slim@sha256:…`), Python paketleri
> `requirements.lock`'taki tam sürümlerle kurulur; derlemenin son adımı imajın
> içini denetler. Derleme logunda şu iki satır görünmelidir:
>
> ```
> GÖNDERME YIĞINI python=3.12.16 openssl=3.5.7 httpx=0.28.1 httpcore=1.0.9 h2=4.4.1 hpack=4.2.0 hyperframe=6.1.0 h11=0.16.0 anyio=4.16.0 certifi=2026.10.23 idna=3.20
> kilit uyumlu: 36 kurulu paket, 36 sabitlenmiş sürüm
> ```
>
> Derleme `KİLİT UYUŞMUYOR` ile durursa yeni sürüm yayına **girmez**; Render'ın
> belgesine göre servis son başarılı sürümle çalışmaya devam eder. Ne yapılacağı:
> [`LESSONS_2026_11_14.md §20.6`](LESSONS_2026_11_14.md).
>
> **Canlıya alma adımları** (PR #56; önce tek hesap, 12 saat sonra ikinci):
> [`ROLLBACK_2026_11_16.md §13-§14`](ROLLBACK_2026_11_16.md).

## 1. Dağıtım nasıl ilerler?

1. Render, `render.yaml` içindeki tek Docker Web Service'i okur.
2. `Dockerfile` Python paketlerini, token sayacını, Playwright ve Chromium'u kurar.
   *(19 Kasım, PR #56: taban imaj özetle, paketler `requirements.lock` ile sabit;
   son adım imajın içini denetler — baştaki not.)*
3. Render bir `PORT` verir. Container komutu bu portu kullanır; elle `PORT` yazmayın.
4. Uvicorn **tek worker** ile açılır.
5. FastAPI lifespan sırasıyla upstream client'ını, CAPTCHA sağlayıcısını, oturum
   yöneticisini, model kaydını ve hesap havuzunu kurar. `UPSTREAM_CURL` doluysa önce
   cURL ayrıştırılır; cURL'deki `modelAId` varsayılan modelin upstream kimliğini ezer.
6. `/healthz` process'in cevap verdiğini, `/readyz` temel dağıtım ayarlarının tamam
   olduğunu bildirir.
7. Render blueprint'i healthcheck olarak `/readyz` kullanır. Eksik ayarlar varsa
   cevap gövdesinde yalnızca güvenli hata kodları görünür; secret değerler görünmez.

`plan: free` ücretli kaynak gerektirmez. `numInstances: 1` bilinçlidir: hesap
kotaları, cookie jar'lar, öğrenilen limitler ve sohbetler bellek içindedir.
Free planda kalıcı disk yoktur; dönen refresh token ve çerezler `RENDER_API_KEY`
ile Render environment değişkenlerine senkronize edilir. `RENDER_SERVICE_ID`
Render tarafından otomatik sağlanır. Sohbet geçmişi ve öğrenilen kota sayaçları
restart'ta sıfırlanır; bunlar upstream istek mantığını bozmaz.

> ⚠️ **Bu sürümde (19 Kasım notu):** `RENDER_API_KEY` senkronu **yok**; dönen
> çerezler restart'ta **kaybolur** ve bu, upstream isteklerini bozar (oturum
> ölür). `render.yaml` 19 Kasım'dan beri canlıdaki gibi `plan: free` yazar.

## 2. Bir tamamlama isteği nasıl yürür?

> **19 Kasım notu.** Aşağıdaki sıra canlı (`03aca2c`) kod için doğrudur. PR #56
> ile (20 Kasım'da birleştirildi) 4. adımdan önce istek bir hesabın **sırasına** girer
> (hesap başına tek tek, aralarında 5–10 sn); hesap seçimi "o an en az işi olan
> hesap"tır ve 10. adımdaki devir `401` ile kota reddini kapsar:
> [`ASK_API.md §4`](ASK_API.md), [`TWO_ACCOUNTS.md §3-§5`](TWO_ACCOUNTS.md).
> 7. adımda bu hedef için geçerli kip yalnız `static`'tir.

1. **API doğrulama:** `Authorization: Bearer` veya `x-api-key` kontrol edilir.
2. **Rate-limit/body limit:** İstemci kotası ve istek boyutu uygulanır.
3. **Model çözümü:** OpenAI model adı `config/models.yaml` içindeki gerçek
   `upstream_id` değerine çevrilir.
4. **Hesap seçimi:** Cooldown'da olmayan, pencerede en az mesaj kullanmış hesap seçilir.
5. **Sohbet:** `conversation_id`/istemci kimliği ve hesap etiketiyle upstream sohbeti
   bulunur veya yeni `chat_id` üretilir.
6. **Warm-up:** Gerekirse seçilen hesabın domain'i, kimliği ve **hesaba özel cookie
   jar'ı** ile sohbet sayfası açılır. Warm-up hatası ana isteği tek başına durdurmaz.
7. **CAPTCHA:** `browser`, `external`, `static` veya `noop` kipinden token alınır.
   Browser token'ı hedef origin'de üretilir; `about:blank` üzerinde deneme yapılmaz.
   Gözlenen hedef için `enterprise.js` ve `grecaptcha.enterprise.execute()` kullanılır;
   `/enterprise/reload` isteği veya Google cookie'leri kopyalanmaz.
8. **Upstream POST:** Aynı hesabın client'ı, cookie jar'ı, Authorization ve model
   kimliği kullanılır. Retry/backoff toplam deadline içindedir; 301/302 POST'u GET'e
   çevirecek şekilde otomatik takip edilmez.
9. **Hata sınıflandırma:** kota, kimlik, CAPTCHA, yönlendirme, ağ ve timeout ayrılır.
10. **Güvenli devir:** Kota hatasında, istemciye henüz içerik gitmediyse başka hesap
    denenebilir. İçerik başladıktan sonra iki cevap birbirine karıştırılmaz.
11. **Cevap:** AI SDK akışı parse edilerek OpenAI SSE veya non-streaming JSON üretilir.
12. **Kapanış:** Uygulama kapanırken CAPTCHA kaynakları, tüm hesap client'ları ve
    oturum cache'i temizlenir.

## 3. Render'a alma

### Ön koşullar

- `config/models.yaml` içinde varsayılan modelin `upstream_id` değeri gerçek olmalı;
  `REPLACE_WITH_...` bırakılırsa `/readyz` servisi hazır kabul etmez.
- Blueprint `free` plan kullanır. Servis hareketsizlikte uyuyabilir; bu durum hata
  değildir, gelen HTTP isteğiyle uyanır ancak ilk cevap gecikebilir.
  *(19 Kasım: canlı servis Free ve cron-job.org ile uyanık tutuluyor;
  `render.yaml` de `free` yazıyor. **Uyku, bu sürümde masum değildir:**
  Render'ın belgelediği davranışa göre uyuyan servis gelen istekte yeni süreçle
  açılır; bellekteki oturum gider — restart ile aynı sonuç. Bu kurulumda
  **gözlenmedi**; zamanlayıcı durursa görülmesi beklenir.)*
- Güçlü, varsayılandan farklı bir `API_KEYS` değeri seçilmeli.
- Free planda refresh token kalıcılığı için Render hesabından bir API key üretilip
  `RENDER_API_KEY` secret'ına yazılmalı. `RENDER_SERVICE_ID` otomatik olduğundan
  elle servis ID'si eklenmez. Bu anahtar yoksa servis yine çalışır fakat restart
  sonrası `/cookie` ile kimlik tazeleme gerekebilir.
  *(⚠️ Bu sürümde yok: `RENDER_API_KEY` okunmaz, `/cookie` yoktur.)*
- `UPSTREAM_CURL` için Max'teki tamamlanmış `create-evaluation` POST isteğinin tamamı
  kullanılmalı; telemetri, RUM, analytics veya yarım kopyalanmış istek kullanılmamalı.
  cURL'deki URL'nin şeması olabilir; wrapper hedef domain'i kendisi çıkarır.
- Gözlenen CAPTCHA için `RECAPTCHA_VERSION=enterprise` ve
  `RECAPTCHA_ACTION=chat_submit` kullanılır. Public site key blueprint'tedir;
  CAPTCHA token'ı, Google cookie'si veya protobuf `/reload` gövdesi girilmez.

### Blueprint ile

1. Render'da **New → Blueprint** seçin ve bu depoyu bağlayın.
2. `API_KEYS` için güçlü, varsayılandan farklı bir değer girin.
3. Render hesabınızın API key'ini `RENDER_API_KEY` secret'ına girin. Bu değer
   token/çerez rotasyonunu Free planda restart dayanıklı yapar; repo'ya veya
   Telegram'a yazılmamalıdır. Servis ID'sini kopyalamanız gerekmez.
   *(⚠️ Bu sürümde bu adım **yok**: blueprint'te `RENDER_API_KEY` tanımlı değil,
   kod da okumuyor. Yerine yapılacak bir şey yoktur; 4. adımdaki cURL yeter.)*
4. `UPSTREAM_CURL` secret'ına Max'teki tamamlanmış `create-evaluation` isteğinin normal
   cURL'ünü yapıştırın. Bunu sohbete, README'ye, `render.yaml` dosyasına veya loglara
   kopyalamayın. Eski ayrı `TARGET_DOMAIN`/`UPSTREAM_COOKIE`/token alanları bırakılırsa
   bile dolu `UPSTREAM_CURL` önceliklidir; tek hesap için eski `_2`/`_3`/`_4` değerlerini
   silmek en temiz seçenektir.
5. Blueprint `RECAPTCHA_PROVIDER=static` getirir (**kalıcı**). Sunucudaki Chromium'un
   ürettiği token hedefte `403 recaptcha validation failed` alıyor; gerçek tarayıcıdan
   alınmış statik token çalışıyor. Public site key, Enterprise sürümü ve action hazırdır.
   cURL yüklemesi `RECAPTCHA_STATIC_TOKEN` alanını otomatik doldurur.
6. Deploy loglarında Chromium kurulumu ve application startup aşamalarını izleyin.
   `upstream_curl_loaded` yalnız uygulanan alan adlarını ve model bulunup bulunmadığını
   gösterir; ham cURL veya secret taşımaz. İlk başarılı yenilemeden sonra
   `render_env_synced` olayı Free kalıcılığının çalıştığını gösterir.
   *(⚠️ `render_env_synced` bu sürümde hiç basılmaz.)*
7. Aşağıdaki kontrolleri yapın.

Blueprint secret değerleri depoda tutmaz; `sync: false` alanları Render tarafından
istenir. cURL'den domain, akış yolu, mode, referer, kullanıcı ajanı, ek header'lar,
cookie/authorization ve `modelAId` çıkarılır. `UPSTREAM_CURL` değiştirilirse mevcut
process dosyayı/env'i sıcak okumaz; Render'da yeni deploy veya restart gerekir. Yanlış
telemetri cURL'ü, URL'si çıkarılamayan cURL ve cookie/authorization içermeyen cURL
başlangıçta reddedilir; hata metni ham secret'ı içermez.

### İlk kontroller

```bash
curl -fsS https://SERVICE.onrender.com/healthz
curl -fsS https://SERVICE.onrender.com/readyz
curl -H "Authorization: Bearer $API_KEY" \
     https://SERVICE.onrender.com/v1/models
```

Canlı upstream ile tam doğrulama:

```bash
BASE_URL=https://SERVICE.onrender.com/v1 \
API_KEY="$API_KEY" \
python scripts/test_client.py --model MODEL_ADI
```

Bu script modelleri, maskeli kimlik teşhisini, non-streaming cevabı, SSE akışını,
usage/finish chunk'larını ve temel hata yollarını kontrol eder.

## 4. `/readyz` hata kodları

| Kod | Anlamı |
|---|---|
| `target_domain_not_configured` | Varsayılan localhost veya örnek domain kalmış |
| `authentication_disabled` | Public serviste API doğrulama kapalı |
| `api_keys_missing` | Kullanılabilir API anahtarı yok |
| `default_development_api_key` | `sk-local-dev-key` hâlâ kabul ediliyor |
| `default_model_missing` | Varsayılan model çözülemiyor |
| `default_upstream_model_placeholder` | Gerçek upstream model ID girilmemiş |
| `recaptcha_site_key_missing` | `browser` için site key eksik |
| `recaptcha_external_url_missing` | `external` için çözücü URL'si eksik |
| `recaptcha_static_token_missing` | `static` için global/hesap token'ı yok |

`/readyz` canlı upstream'e istek atmaz. Cookie/token süresi veya upstream kesintisi
bu kontrolü değiştirmez; bu durumlar için aşağıdaki istek hatalarına ve loglara bakılır.
Bu ayrım, geçici upstream arızalarının Render restart döngüsü oluşturmasını engeller.

## 5. Belirti → olası aşama → kontrol

| Belirti | Olası aşama | İlk kontrol |
|---|---|---|
| Render hiç active olmuyor | Build veya `/readyz` | Deploy logu ve readiness `issues` |
| `/healthz` 200, completion 404 | Model veya stream path | Model adı; `UPSTREAM_CURL` içindeki gerçek akış yolu |
| Completion 401/502 `upstream_unauthorized` | Upstream kimliği | `/v1/admin/auth?slot=N`, cookie/token |
| Completion 403/503 CAPTCHA | CAPTCHA kipi/site key/origin | Provider, site key, browser logları |
| Upstream 404 | Hedef yol değişmiş | Gerçek istek yolu; yönlendirme logu |
| 429 ve hesap cooldown | Upstream hesap kotası | `/v1/admin/accounts`; zorlamayın |
| SSE HTTP 200 ama metin/usage yok | Akış içinde upstream hatası | SSE `error.code` ve aynı `request_id` altındaki `stream_failed` |
| 502 ağ/TLS/proxy | Upstream bağlantısı | Domain, TLS, outbound erişim |
| 504 | Idle veya toplam deadline | Upstream yanıt süresi ve timeout ayarları |
| Restart sonrası sohbet/kota sıfırlandı | Bellek içi çalışma durumu | Free planda beklenen davranış |
| Restart sonrası refresh token eskiye döndü | Render env sync | `RENDER_API_KEY` ve `render_env_synced`/`render_env_sync_failed` loglarını doğrulayın *(⚠️ bu sürümde env sync yok: restart'ta eskiye dönmek **beklenen** davranıştır; çare taze `UPSTREAM_CURL`)* |
| İstek uzun süre bekliyor, sonra cevap geliyor | Free sleep/cold start | Servis uyanıyor; açılış ve token denetimi loglarını izleyin |
| İkinci hesap birincinin cookie'sini kullanıyor | Eski davranış/regresyon | Hesap client/cookie testlerini çalıştırın |

Yönetim uçları API anahtarı ister. Token değerleri değil, maskeli teşhis kullanın:

```bash
curl -H "Authorization: Bearer $API_KEY" \
     https://SERVICE.onrender.com/v1/admin/auth?slot=2
```

## 6. Telegram ile ara sıra istek testi

Bot ayrı webhook altyapısı istemez; Telegram `getUpdates` long polling kullanır ve
mesajı wrapper'ın non-streaming `/v1/chat/completions` ucuna gönderir.

### Aynı Web Service içinde (ayrı worker yok)

Web Service environment'ına yalnız şu iki alanı ekleyin:

```text
TELEGRAM_BOT_TOKEN=<BotFather tokenı>
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Token varsa FastAPI lifespan botu aynı process'te başlatır. `WRAPPER_BASE_URL` kendi
`PORT` değerinden, `WRAPPER_API_KEY` mevcut `API_KEYS` ilk değerinden türetilir; bu iki
alanı Web Service'e ayrıca eklemeyin. Deploy sonrası aynı Web Service loglarında
`telegram_bot_started` arayın. Tek instance/tek worker sınırı korunmalıdır; aksi halde
aynı tokenla polling yapan process'ler 409 üretir. Free Web Service uyursa bot da durur;
Telegram mesajı uyuyan servisi uyandırmaz.

### Yerel veya ayrı process

```powershell
$env:TELEGRAM_BOT_TOKEN = "<BotFather tokenı>"
$env:WRAPPER_BASE_URL = "https://SERVICE.onrender.com"  # kök URL, /v1 ekleme
$env:WRAPPER_API_KEY = "<API_KEYS içindeki tek anahtar>"
$env:TELEGRAM_ALLOWED_CHAT_IDS = ""
python -m app.telegram.bot
```

1. Telegram'da bota `/start` gönderin. Allowlist boşken bot yalnızca chat ID'yi gösterir;
   wrapper'a istek atmaz.
2. Gelen ID'yi `TELEGRAM_ALLOWED_CHAT_IDS` değerine yazıp botu yeniden başlatın.
3. `/ping` wrapper `/healthz` kontrolünü, normal metin gerçek non-streaming tamamlamayı
   test eder. `/help` kullanım bilgisini gösterir.

Ayrı bot ancak Free kapsamı dışında bir **Background Worker** kullanılacaksa gerekir;
bu Blueprint worker oluşturmaz ve Free kullanım için gömülü bot yeterlidir. Ayrı worker
kullanılacaksa aynı repo/branch ile şu ayarlar seçilir:

| Alan | Değer |
|---|---|
| Runtime | `Docker` |
| Dockerfile Path | `./Dockerfile.telegram` |
| Build Command | Boş |
| Start/Docker Command | Boş; dosyanın `CMD` satırı botu başlatır |

Web Service `./Dockerfile` kullanmaya devam etmelidir; onun komutunu botla değiştirmeyin.
Worker'a yalnız yukarıdaki dört env alanını ekleyin; `UPSTREAM_CURL` web servisinde kalır.
`WRAPPER_BASE_URL`, kök ApiWrapper JSON'unu veren Web Service adresidir; `/v1` veya
`/docs` eklemeyin. Render worker'da `127.0.0.1` kullanmayın. Bot token'ı ve wrapper API
key'i hiçbir zaman Telegram mesajına veya depoya yazılmaz. Bot tek turluktur, konuşma
geçmişi tutmaz; stream testi için `test_client.py` kullanılır. Gerçek Telegram/Render
bağlantısı secret'lar olmadan bu geliştirme ortamında canlı doğrulanmamıştır.

## 7. Free planda çalışma sınırları

> ⚠️ **Bu sürümde (19 Kasım notu) bu listeden geçerli OLMAYAN maddeler:**
>
> * *"Refresh HTTP akışı uygulanmıştır…"* — bu sürümde token yenileyen bir arka
>   plan işi **yok**; erişim token'ını hedef, biz istek attıkça `Set-Cookie` ile
>   yeniler (`ROLLBACK §12.4`).
> * *"`static` CAPTCHA token'ı kısa ömürlüdür; unattended kullanım için `browser`
>   veya `external` gerekir"* — **ölçümle çürütüldü ve tersi doğru:** aynı statik
>   token ~28 saat kabul edildi, `browser` ise bu hedefte 403 alıyor
>   (`LESSONS §3`, `§15.3`).
> * *"…ardından varsa yerel durum dosyası uzlaştırılır. Free planda asıl kalıcı
>   katman Render env senkronizasyonudur"* — ikisi de **yok**.
> * *"Token yenilemesi eski hesap client'larını…"* — token yenilemesi yok.
> * *"`UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=10800`…"* — örnek değer ölçülmedi;
>   canlıda denenenler `600` ve `3600`. PR #56'da aralık, hesabın son başarılı
>   gerçek isteğinden sayılır (`ASK_API.md §6`).
> * *"`UPSTREAM_CX_SEND=true`"* — ayar yok.
> * Sürüm damgası maddesindeki ayar adı düzeltildi: `DEPLOYMENT_ID_TTL_SECONDS`
>   (başında `UPSTREAM_` yok).

- Free Web Service hareketsizlikte uyur; harici bir HTTP isteği gelince uyanır.
  Cold start sırasında gecikme normaldir. İçeriden çalışan keepalive veya Render
  healthcheck'i always-on garantisi değildir. Telegram long polling de uyku
  sırasında çalışmaz; Telegram mesajları servis uyandıktan sonra işlenebilir.
- Render'ın aktif kalması, upstream oturumunun sonsuza kadar geçerli olması demek değildir.
- Refresh HTTP akışı uygulanmıştır: refresh token varsa erişim token'ı açılışta
  hemen ve sonrasında 60 sn'de bir denetlenir, bitmesine 10 dk kala Supabase/GoTrue
  `grant_type=refresh_token` isteğiyle yenilenir. İstek anında süresi dolmuş token
  ve 401 için de kurtarma denenir. Refresh token yoksa/iptal edildiyse giriş yapmış
  tarayıcıdan yeni çerez gerekir; wrapper parola/OTP ile giriş yapamaz.
- `static` CAPTCHA token'ı kısa ömürlüdür; unattended kullanım için `browser` veya
  uygun `external` sağlayıcı gerekir.
- Browser sağlayıcı CAPTCHA bootstrap belgesini Playwright route ile hedef origin'de
  üretir; canlı hedef HTML'ine bağımlı değildir. Google reCAPTCHA betiğine outbound
  erişim gerekir; upstream completion isteği için hedef servise erişim ayrıca gerekir.
  Chromium RAM/CPU kullanır; kaynak sınırını Render loglarından izleyin.
- Tek instance/tek worker sınırını kaldırmak, bellek içi hesap ve sohbet durumunu
  instance'lar arasında senkronize etmez.
- Her deploy/restart'ta öğrenilen kota ve sohbet durumu sıfırlanır. Cookie/token
  tarafında önce environment/cURL tohumu okunur, ardından varsa yerel durum dosyası
  uzlaştırılır. Free planda asıl kalıcı katman Render env senkronizasyonudur
  (`RENDER_API_KEY` + otomatik `RENDER_SERVICE_ID`); `/app/.cache` kalıcı değildir.
- Token yenilemesi eski hesap client'larını yeni isteklere hemen kapatır; açık
  upstream akışları ise eski client üzerinde tamamlanır. Böylece saatlik yenileme
  devam eden SSE cevabını kesmez.
- `UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=10800` süreç ayaktayken üç saatte bir kısa
  non-stream completion gönderir; varsayılan `0` kapalıdır. Bu kota kullanan bir
  hareketsizlik deneyidir, auth refresh uygulaması değildir. Aralık
  `UPSTREAM_KEEPALIVE_JITTER_SECONDS` (± vars. 300 sn) ile sapmalanır, her probe
  farklı bir kısa istem gönderir ve günde bir kez aynı kontrol `stream: true` ile
  yapılır. Free sleep task'ı da durdurur. `upstream_keepalive_started`,
  `upstream_keepalive_completed`, `upstream_keepalive_failed` ve
  `upstream_keepalive_stream_smoke_*` timestamp'li logları secret/prompt/cevap
  taşımaz.
- Sürüm damgası (`x-deployment-id`, `x-arena-web-deployment`) `UPSTREAM_CURL`'den
  donmuş taşınmaz: `DEPLOYMENT_ID_TTL_SECONDS` (vars. 1800) aralığıyla
  upstream'in ana sayfasından okunur ve 409 alınırsa istek bir kez taze damgayla
  tekrar edilir. Sayfa okunamazsa (bot koruması/ağ) donmuş değer kullanılır; yani
  kanarya isteği bozmaz. Durum `GET /health` → `upstream.deployment_stamp`
  alanında **değer olmadan** görünür.
- `cx` iki farklı sohbette aynı değerde gözlendi (oturum/uygulama bazlı, mesaj bazlı
  değil) ve wrapper bu alan olmadan başarılı istek atıyor. Bu yüzden zorunlu kabul
  edilmez; bir 4xx gövdesi `cx` alanına atıfta bulunursa logda `upstream_cx_flag`
  ve `apiwrapper_upstream_cx_flags_total` metriği görünür.
- `cx` artık cURL gövdesinden **yakalanır** (`UPSTREAM_CX`) ama varsayılan olarak
  **gönderilmez**. Gönderimi `UPSTREAM_CX_SEND=true` ile açılır: 4xx gövdesi bu
  alana atıfta bulunmaya başlarsa tek değişiklik bu bayraktır.

## 8. JSON logları ve SSE hata ayıklama

Render loglarında her isteğin `request_id` değeri ortaktır. Tamamlama yaşam döngüsü
artık yalnızca HTTP erişim loguna bağlı değildir:

| Event | Ne zaman üretilir? | Önemli alanlar |
|---|---|---|
| `completion_completed` | Non-streaming cevap hazır | `completion_id`, `model`, `content_chars`, `usage_total_tokens`, `duration_ms` |
| `stream_completed` | Upstream akışı ve OpenAI SSE tamamlandı | `finish_reason`, `content_chars`, `content_chunks`, `first_token_ms`, `usage_total_tokens`, `duration_ms` |
| `stream_failed` | Akış içinde auth, CAPTCHA, kota, ağ veya parser hatası | `error_code`, `error_type`, `status`, `content_chars`, `first_token_ms`, `duration_ms` |
| `client_disconnected` | İstemci akışı erken kapattı | `completion_id`, `model`, `content_chars`, `duration_ms` |

Yeni tamamlama logları prompt'u veya cevap metnini ayrıca yazmaz; içerik yerine
`content_chars` gibi sayaçlar kullanır. Cookie, CAPTCHA token'ı ve API anahtarı alanları
taşınmaz. `stream_failed.error` alanı wrapper'ın mevcut hata mesajını içerdiğinden, logları
paylaşırken yine secret değerleri maskeleme kuralı uygulanmalıdır.

**Önemli SSE ayrımı:** Streaming response başlıkları upstream cevaptan önce HTTP 200
ile açılır. Sonradan oluşan hata aynı gövdede `data: {"error": ...}` olarak taşınır ve
`[DONE]` ile kapanır. Dolayısıyla erişim logundaki `status=200`, tek başına akışın
başarılı olduğunu kanıtlamaz. `stream_completed` yerine `stream_failed` olup
olmadığına bakın. `request.duration_ms` streaming'de yalnızca başlıklara kadar olan
süreyi gösterebilir; tam akış süresi için `stream_completed.duration_ms` kullanılır.

`scripts/test_client.py` bu hata chunk'ını artık “0 karakter / usage yok” diye gizlemek
yerine gerçek `code` ve `message` ile gösterir. Script non-stream ve stream için art
arda iki canlı istek atar; ikinci istek `upstream_rate_limited` dönerse önce upstream
hız sınırının geçmesini bekleyin. Bu durum cookie yenilemek veya Render'ı restart etmek
için kanıt değildir.

Örnek güvenli hata alanları:

```json
{
  "event": "stream_failed",
  "error_code": "upstream_rate_limited",
  "error_type": "rate_limit_error",
  "status": 429,
  "content_chars": 0,
  "request_id": "req_..."
}
```

## 9. Token yenileme `apikey` hatası ve `/apikey` kurtarması

> ⚠️ **Bu bölümün tamamı bu sürümde geçersizdir (19 Kasım notu).** Token
> yenileme kodu, `/apikey`, `/cookie` ve `/yenile` geri dönüşle kalktı; buradaki
> Telegram mesajları da gelmez. Kayıt olarak duruyor.

Belirti zinciri (Telegram):

1. `⚠ Sitenin ana sayfasından anon anahtar otomatik keşfedilemedi`
2. `⚠ erişim token'ının bitmesine ≈N dk kaldı ve otomatik yenilenemedi
   (Upstream apikey istiyor)`
3. Bir süre sonra `🔴 upstream_unauthorized (HTTP 401)`

Kök neden: Supabase/GoTrue yenileme ucu `apikey` (anon anahtar) istiyor; anahtar
yoksa refresh token geçerli olsa bile yenileme reddediliyor ve erişim token'ı
bitince oturum 401'e düşüyor.

**Artık `/apikey` tek başına kurtarır** — anahtarı kaydettikten sonra yenilemeyi
aynı istekte dener:

```
/apikey                # siteden keşfet (başarısızsa NEDENİNİ söyler)
/apikey <anon-anahtar> # elle ver; cevap "Token yenilendi (~60 dk)" olmalı
```

- Keşif başarısızsa cevap ana sayfanın HTTP kodunu ve kaç JS parçasının
  tarandığını söyler (bot koruması `403` ise otomatik keşif zaten çalışmaz;
  anahtarı elle verin).
- `Token hâlâ yenilenemedi` derse anahtar yanlıştır; oturum 401'e düşmeden
  `/cookie` veya `/yenile` ile taze çerez gönderin.
- Anahtar local state'e ve (yapılandırıldıysa) Render env'ine yazılır; restart'ta
  korunur.

Uyarı mesajındaki `≈N dk` değeri her kontrolde yeniden hesaplanır; iki bildirim
arasında farklı görünmesi normaldir. Kesin zaman için mesajdaki
`bitiş ≈ YYYY-MM-DD HH:MM UTC` alanına bakın.
