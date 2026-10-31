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
   yöneticisini, model kaydını ve hesap havuzunu kurar.
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
- `TARGET_DOMAIN` şemasız yazılmalı: `upstream.example.com`, `https://` olmadan.
- Gözlenen CAPTCHA için `RECAPTCHA_VERSION=enterprise` ve
  `RECAPTCHA_ACTION=chat_submit` kullanılır. Public site key blueprint'tedir;
  CAPTCHA token'ı, Google cookie'si veya protobuf `/reload` gövdesi girilmez.

### Blueprint ile

1. Render'da **New → Blueprint** seçin ve bu depoyu bağlayın.
2. `render.yaml` algılandığında istenen environment değerlerini girin.
3. Blueprint sonrası ikinci+ hesapları veya hedefe özel diğer ayarları Render
   Environment ekranından ekleyin.
4. Deploy loglarında Chromium kurulumu ve application startup aşamalarını izleyin.
5. Aşağıdaki kontrolleri yapın.

Blueprint secret değerleri depoda tutmaz; `sync: false` alanları Render tarafından
istenir. `UPSTREAM_ACCESS_TOKEN` doğrudan token kullanılmayacaksa boş bırakılabilir.
`UPSTREAM_AUTH_FROM_COOKIE` varsayılan olarak `false` kalır; hedefin gerçekten
Authorization başlığı istediği biliniyorsa ayrıca `true` yapılır.

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
| `/healthz` 200, completion 404 | Model veya stream path | Model adı ve `UPSTREAM_STREAM_PATH` |
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

## 6. 7/24 çalışma sınırları

- Render'ın aktif kalması, upstream oturumunun sonsuza kadar geçerli olması demek değildir.
- Gerçek refresh HTTP sözleşmesi henüz uygulanmamıştır. `refresh_token` ayrıştırma ve
  hesap bazlı cookie altyapısı hazırdır; tahmini refresh endpoint'i kullanılmamalıdır.
- `static` CAPTCHA token'ı kısa ömürlüdür; unattended kullanım için `browser` veya
  uygun `external` sağlayıcı gerekir.
- Browser sağlayıcı için hedef origin'e ve Google reCAPTCHA betiğine outbound erişim
  gerekir. Chromium RAM/CPU kullanır; kaynak sınırını Render loglarından izleyin.
- Tek instance/tek worker sınırını kaldırmak, bellek içi hesap ve sohbet durumunu
  instance'lar arasında senkronize etmez.
- Her deploy/restart cookie jar'ı environment'taki başlangıç değerlerinden kurar ve
  öğrenilen kota/sohbet durumunu sıfırlar.
- Max için `modelAId` art arda isteklerde sabit, `cx` ise değişkendir. `cx` sabit
  yazılmaz; ilk canlı test bu alan olmadan yapılır. Upstream zorunlu tutarsa istemci
  JS'sindeki gerçek üretim yöntemi ayrıca eklenmelidir.

## 7. JSON logları ve SSE hata ayıklama

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
