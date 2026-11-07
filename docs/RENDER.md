# Render aktif çalışma runbook'u

Bu belge sistemi dağıtımdan cevaba kadar sırayla yürütür. Amaç yalnızca container'ı
açmak değil, **hangi aşamanın çalıştığını ve hatanın nerede başladığını** ayırt
edebilmektir.

> **Hangi dal?** `render.yaml` dal seçmez; Render servis ayarlarındaki production
> dal kullanılır. Canlı servisin izlediği dalı Render panelinden doğrulayın.
> Geçmiş merge kayıtları [`MERGE_TARGET.md`](MERGE_TARGET.md) içindedir.

## 1. Dağıtım nasıl ilerler?

1. Render, `render.yaml` içindeki tek Docker Web Service'i okur.
2. `Dockerfile` Python paketlerini, token sayacını, Playwright ve Chromium'u kurar.
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

## 2. Bir tamamlama isteği nasıl yürür?

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
- Güçlü, varsayılandan farklı bir `API_KEYS` değeri seçilmeli.
- Free planda refresh token kalıcılığı için Render hesabından bir API key üretilip
  `RENDER_API_KEY` secret'ına yazılmalı. `RENDER_SERVICE_ID` otomatik olduğundan
  elle servis ID'si eklenmez. Bu anahtar yoksa servis yine çalışır fakat restart
  sonrası `/cookie` ile kimlik tazeleme gerekebilir.
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
4. `UPSTREAM_CURL` secret'ına Max'teki tamamlanmış `create-evaluation` isteğinin normal
   cURL'ünü yapıştırın. Bunu sohbete, README'ye, `render.yaml` dosyasına veya loglara
   kopyalamayın. Eski ayrı `TARGET_DOMAIN`/`UPSTREAM_COOKIE`/token alanları bırakılırsa
   bile dolu `UPSTREAM_CURL` önceliklidir; tek hesap için eski `_2`/`_3`/`_4` değerlerini
   silmek en temiz seçenektir.
5. `RECAPTCHA_PROVIDER=browser`, public site key, Enterprise sürümü ve action blueprint'te
   hazırdır. cURL'deki kısa ömürlü CAPTCHA token'ı bu seçimi static'e çevirmez.
6. Deploy loglarında Chromium kurulumu ve application startup aşamalarını izleyin.
   `upstream_curl_loaded` yalnız uygulanan alan adlarını ve model bulunup bulunmadığını
   gösterir; ham cURL veya secret taşımaz. İlk başarılı yenilemeden sonra
   `render_env_synced` olayı Free kalıcılığının çalıştığını gösterir.
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
| Restart sonrası refresh token eskiye döndü | Render env sync | `RENDER_API_KEY` ve `render_env_synced`/`render_env_sync_failed` loglarını doğrulayın |
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
  donmuş taşınmaz: `UPSTREAM_DEPLOYMENT_ID_TTL_SECONDS` (vars. 1800) aralığıyla
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

### Render 400 ve CAPTCHA teşhisi

`render_env_sync_failed` olayındaki `empty_value` ve
`empty_fallback_attempted` alanları yalnız değerin boş olup olmadığını ve alternatif
kaydetmenin denenip denenmediğini belirtir; Render yanıt gövdesi, anahtarlar ve
oturum bilgileri loglanmaz. Bir env alanına boş değer yazılması 400 döndürürse,
servis o alana ayrılmış `__APIWRAPPER_EMPTY_VALUE_V1__` işaretini yazmayı dener;
bu sürüm işareti yeniden açılışta boş değer olarak okur. Böylece eski bir değeri
canlandırabilecek kör `DELETE` işlemi yapılmaz. `render_env_sync_empty_fallback`
yalnız bu alternatif yazmanın kabul edildiğini; `render_env_synced` tüm alanların
yazıldığını gösterir. Alternatif de başarısızsa işçi yeniden dener ve sorun çözülmüş
sayılmaz. HTTP 400'ün nedeni yalnızca bu loglardan kesinleştirilemez.

**Geri alma:** Bu işaret bu sürüme ayrılmıştır; işareti tanımayan eski sürüme
dönmeden önce etkilenen servis env değerlerini güvenli biçimde gözden geçirin.
İşareti gerçek cookie, token veya diğer elle girilen değer olarak kullanmayın.
Startup'ta eski cURL ile env snapshot arasındaki seçim JWT expiry karşılaştırmasına
bağlıdır; işaret bu seçimi veya bir upstream isteğinin kabulünü garanti etmez.

Browser sağlayıcısında `/tani` ve `recaptcha_browser_token_source`, son tokenın
`live_click`, `live_execute` veya `bootstrap_execute` yolunu gösterir; tokenın
kendisi gösterilmez. Bu, üretim yolunun teşhisidir, doğrulama sonucu değildir.
Upstream 403 döndürürse site key/action/version/alan eşleşmesini ve hedefin
kendi yetkilendirme ve erişim politikasını kontrol edin; tek başına JWT expiry
veya token üretilmesi kabul kanıtı değildir.
