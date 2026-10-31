# Render aktif çalışma runbook'u

Bu belge sistemi dağıtımdan cevaba kadar sırayla yürütür. Amaç yalnızca container'ı
açmak değil, **hangi aşamanın çalıştığını ve hatanın nerede başladığını** ayırt
edebilmektir.

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

`plan: starter` sleep etmeyen ücretli plandır. `numInstances: 1` bilinçlidir: hesap
kotaları, cookie jar'lar, öğrenilen limitler ve sohbetler bellek içindedir.

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
- Render'da sleep etmeyen plan kullanılmalı. Blueprint bunu `starter` olarak tanımlar.
- Güçlü, varsayılandan farklı bir `API_KEYS` değeri seçilmeli.
- `UPSTREAM_CURL` için Max'teki tamamlanmış `create-evaluation` POST isteğinin tamamı
  kullanılmalı; telemetri, RUM, analytics veya yarım kopyalanmış istek kullanılmamalı.
  cURL'deki URL'nin şeması olabilir; wrapper hedef domain'i kendisi çıkarır.
- Gözlenen CAPTCHA için `RECAPTCHA_VERSION=enterprise` ve
  `RECAPTCHA_ACTION=chat_submit` kullanılır. Public site key blueprint'tedir;
  CAPTCHA token'ı, Google cookie'si veya protobuf `/reload` gövdesi girilmez.

### Blueprint ile

1. Render'da **New → Blueprint** seçin ve bu depoyu bağlayın.
2. `API_KEYS` için güçlü, varsayılandan farklı bir değer girin.
3. `UPSTREAM_CURL` secret'ına Max'teki tamamlanmış `create-evaluation` isteğinin normal
   cURL'ünü yapıştırın. Bunu sohbete, README'ye, `render.yaml` dosyasına veya loglara
   kopyalamayın. Eski ayrı `TARGET_DOMAIN`/`UPSTREAM_COOKIE`/token alanları bırakılırsa
   bile dolu `UPSTREAM_CURL` önceliklidir; tek hesap için eski `_2`/`_3`/`_4` değerlerini
   silmek en temiz seçenektir.
4. `RECAPTCHA_PROVIDER=browser`, public site key, Enterprise sürümü ve action blueprint'te
   hazırdır. cURL'deki kısa ömürlü CAPTCHA token'ı bu seçimi static'e çevirmez.
5. Deploy loglarında Chromium kurulumu ve application startup aşamalarını izleyin.
   `upstream_curl_loaded` yalnız uygulanan alan adlarını ve model bulunup bulunmadığını
   gösterir; ham cURL veya secret taşımaz.
6. Aşağıdaki kontrolleri yapın.

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
| Restart sonrası sohbet/kota sıfırlandı | Bellek içi durum | Beklenen davranış; kalıcı state yok |
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

İsteğe bağlı ayrı/always-on bot için Render'da aynı repo/branch ile **Background Worker**
oluşturun:

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

## 7. 7/24 çalışma sınırları

- Render'ın aktif kalması, upstream oturumunun sonsuza kadar geçerli olması demek değildir.
- Gerçek refresh HTTP sözleşmesi henüz uygulanmamıştır. `refresh_token` ayrıştırma ve
  hesap bazlı cookie altyapısı hazırdır; tahmini refresh endpoint'i kullanılmamalıdır.
- `static` CAPTCHA token'ı kısa ömürlüdür; unattended kullanım için `browser` veya
  uygun `external` sağlayıcı gerekir.
- Browser sağlayıcı CAPTCHA bootstrap belgesini Playwright route ile hedef origin'de
  üretir; canlı hedef HTML'ine bağımlı değildir. Google reCAPTCHA betiğine outbound
  erişim gerekir; upstream completion isteği için hedef servise erişim ayrıca gerekir.
  Chromium RAM/CPU kullanır; kaynak sınırını Render loglarından izleyin.
- Tek instance/tek worker sınırını kaldırmak, bellek içi hesap ve sohbet durumunu
  instance'lar arasında senkronize etmez.
- Her deploy/restart cookie jar'ı environment'taki başlangıç değerlerinden kurar ve
  öğrenilen kota/sohbet durumunu sıfırlar.
- Max için `modelAId` art arda isteklerde sabit, `cx` ise değişkendir. `cx` sabit
  yazılmaz; ilk canlı test bu alan olmadan yapılır. Upstream zorunlu tutarsa istemci
  JS'sindeki gerçek üretim yöntemi ayrıca eklenmelidir.

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
