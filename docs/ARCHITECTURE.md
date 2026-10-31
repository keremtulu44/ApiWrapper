# ApiWrapper — Mimari Plan (AŞAMA 1)

> Harici bir Web LLM servisinin (`https://<TARGET_DOMAIN>/nextjs-api/stream/post-to-evaluation/{chat_id}`)
> **OpenAI-Compatible** bir REST API'ye (`/v1/chat/completions`, `/v1/models`) dönüştürülmesi.
> Hedef: production-ready, modüler, asenkron, yüksek performanslı FastAPI servisi.

---

## 1. Sistem Genel Görünümü

```
┌──────────────┐   OpenAI SDK / curl / LangChain / OpenWebUI / Cursor
│   İstemci    │   POST /v1/chat/completions  (Bearer sk-local-...)
└──────┬───────┘
       │ (1) OpenAI şeması
┌──────▼─────────────────────────────────────────────────────────┐
│ FastAPI Uygulaması (ASGI / uvicorn+uvloop+httptools)           │
│                                                                 │
│  Middleware Zinciri (dıştan içe)                                │
│   ├─ RequestIDMiddleware      → X-Request-ID üret/propagate      │
│   ├─ AccessLogMiddleware      → structlog JSON, latency, tokens  │
│   ├─ CORSMiddleware           → yapılandırılabilir origin        │
│   ├─ GZip / Brotli            → non-stream yanıtlar için         │
│   ├─ RateLimitMiddleware      → token-bucket (per API key + IP)  │
│   └─ ErrorEnvelopeMiddleware  → tüm hatalar OpenAI error şeması  │
│                                                                 │
│  Router Katmanı  api/v1/{chat,models,health,admin}.py            │
│         │                                                        │
│  Servis Katmanı  services/completion_service.py                  │
│    ├─ Şema Adaptörü  adapters/openai_to_upstream.py              │
│    │     • messages[] → tek prompt (rol şablonlama, sistem msg)  │
│    │     • model adı → upstream modelAId eşlemesi                │
│    │     • multimodal içerik parçaları → attachments/metadata    │
│    ├─ Oturum Yöneticisi  services/session_manager.py             │
│    │     • chat_id / userMessageId / modelAMessageId üretimi     │
│    │     • chat_id rotasyonu, hesap bazlı warm-up oturumu       │
│    ├─ Kimlik Çözücü  upstream/auth.py                            │
│    │     • cookie → access_token ayıklama (JSON/base64/parçalı)  │
│    │     • Authorization: Bearer üretimi, JWT exp uyarısı        │
│    ├─ reCAPTCHA v3 Sağlayıcısı  services/recaptcha/*             │
│    └─ Upstream İstemcisi  upstream/client.py (httpx.AsyncClient) │
│         • HTTP/2, connection pool, retry+jitter, circuit breaker │
│         • proxy rotasyonu, TLS/JA3 uyumlu istemci (opsiyonel)    │
│         │                                                        │
│  Akış Ayrıştırıcı  upstream/stream_parser.py                     │
│    • Vercel AI SDK data-stream protokolü (0:"..", e:{}, d:{} ..) │
│    • byte→satır tamponlama, kısmi UTF-8 güvenli decode           │
│         │                                                        │
│  Yanıt Dönüştürücü  adapters/upstream_to_openai.py               │
│    • delta parçaları → chat.completion.chunk SSE                 │
│    • birikim → chat.completion (non-stream)                      │
└──────┬──────────────────────────────────────────────────────────┘
       │ (2) text/plain gövde, tarayıcı taklidi başlıklar
┌──────▼───────────────────────────────────────────────────────────┐
│  https://<TARGET_DOMAIN>/nextjs-api/stream/post-to-evaluation/…  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Veri Akışı (uçtan uca, adım adım)

**İstek yolu**
1. `POST /v1/chat/completions` → Pydantic v2 `ChatCompletionRequest` ile doğrulanır
   (bilinmeyen alanlar reddedilmez, `model_config = ConfigDict(extra="allow")` ile ileri uyumluluk).
2. `auth.py` bağımlılığı `Authorization: Bearer <key>` başlığını sabit-zamanlı
   (`hmac.compare_digest`) karşılaştırır. Anahtar yoksa 401 + OpenAI error zarfı.
3. Rate limit: `(api_key, model)` bazlı token-bucket; aşımda 429 + `Retry-After`.
4. `model` alanı `ModelRegistry` üzerinden upstream `modelAId`'e çevrilir; bilinmeyen
   model → 404 `model_not_found`.
5. `messages[]` prompt'a düzleştirilir:
   - `system` → en üstte `System:` bloğu (veya yapılandırılabilir şablon),
   - `user`/`assistant` → `User:` / `Assistant:` etiketleri,
   - `tool`/`function` rolleri → JSON blok olarak serialize,
   - içerik parçaları (`[{type:"text"},{type:"image_url"}]`) → metin + attachment listesi,
   - `max_tokens`, `temperature` gibi upstream'in desteklemediği alanlar
     `unsupported_params` politikası ile ya yok sayılır ya da prompt'a ipucu olarak eklenir
     (config: `ignore` | `hint` | `error`).
6. `SessionManager` bir `chat_id` (UUIDv4) ve mesaj id'leri üretir; gerekiyorsa
   seçilen hesabın domain/cookie'si ve hesaba özel HTTP cookie jar'ı ile warm-up yapar.
7. `RecaptchaProvider` token üretir (aşağıda 4. bölüm).
8. `UpstreamClient` `--data-raw` ile birebir aynı JSON gövdeyi
   `content-type: text/plain;charset=UTF-8` başlığıyla **stream** olarak POST eder.

**Yanıt yolu**
9. `stream_parser` gelen baytları satır satır tüketir, Vercel AI SDK
   data-stream kodlarını çözer:
   - `0:"metin"` → metin delta
   - `f:{"messageId":..}` → başlangıç meta
   - `8:` / `2:` → annotation/data
   - `9:`/`a:` → tool call / tool result
   - `e:{...}` , `d:{"finishReason":"stop","usage":{...}}` → bitiş + kullanım
   - `3:"hata"` → upstream hata → OpenAI `upstream_error`
   Bilinmeyen prefix'ler sessizce loglanır (ileri uyumluluk).
10. **Stream=true**: her delta anında `data: {chat.completion.chunk}\n\n` olarak yazılır;
    ilk chunk `role:"assistant"`, son chunk `finish_reason`, ardından `data: [DONE]`.
    `include_usage` istenirse son chunk'ta `usage` alanı.
11. **Stream=false**: parçalar bellekte birleştirilir, tek `chat.completion` döner.
12. Token sayımı: upstream usage vermezse `tiktoken` (cl100k_base) ile tahmin edilir,
    `usage.estimated=true` bayrağı `system_fingerprint` yanında raporlanır.
13. İstemci bağlantıyı koparırsa (`await request.is_disconnected()`) upstream isteği
    `CancelledError` ile iptal edilir — sızdıran bağlantı kalmaz.

---

## 3. Dosya Mimarisi

```
ApiWrapper/
├─ pyproject.toml                 # uv/pip, ruff, mypy, pytest konfigürasyonu
├─ .env.example                   # tüm ayarların örneği
├─ Dockerfile / docker-compose.yml
├─ Makefile                       # run, lint, test, docker
├─ README.md  docs/ARCHITECTURE.md
├─ app/
│  ├─ main.py                     # FastAPI factory, lifespan, router mount
│  ├─ core/
│  │  ├─ config.py                # pydantic-settings, tek kaynak doğruluk
│  │  ├─ logging.py               # structlog JSON + request_id contextvar
│  │  ├─ security.py              # API key doğrulama, sabit-zaman karşılaştırma
│  │  ├─ errors.py                # OpenAIError hiyerarşisi + handler'lar
│  │  ├─ rate_limit.py            # asyncio token-bucket
│  │  └─ metrics.py               # Prometheus counters/histograms
│  ├─ schemas/
│  │  ├─ openai.py                # Request/Response/Chunk/Usage/Model modelleri
│  │  └─ upstream.py              # UpstreamPayload, StreamEvent modelleri
│  ├─ adapters/
│  │  ├─ prompt_builder.py        # messages[] → prompt (şablon stratejileri)
│  │  ├─ openai_to_upstream.py
│  │  └─ upstream_to_openai.py
│  ├─ upstream/
│  │  ├─ client.py                # httpx.AsyncClient havuzu, retry, breaker
│  │  ├─ headers.py               # tarayıcı taklidi başlık üretimi (UA rotasyonu)
│  │  ├─ stream_parser.py         # AI-SDK data-stream çözücü
│  │  └─ exceptions.py
│  ├─ services/
│  │  ├─ completion_service.py    # orkestrasyon (stream + non-stream)
│  │  ├─ session_manager.py       # chat_id/cookie/warm-up, LRU+TTL cache
│  │  ├─ model_registry.py        # models.yaml yükleme, alias çözümü
│  │  └─ recaptcha/
│  │     ├─ base.py  static.py  external.py  playwright.py
│  ├─ api/v1/
│  │  ├─ chat.py  models.py  health.py  admin.py
│  └─ utils/  ids.py  tokens.py  backoff.py  sse.py
├─ config/models.yaml             # model adı ↔ modelAId eşlemesi
└─ tests/                         # pytest-asyncio, respx ile upstream mock
```

---

## 4. reCAPTCHA v3 / Enterprise Stratejisi (kritik nokta)

`recaptchaV3Token` upstream'in zorunlu alanı. Tek bir arayüz (`RecaptchaProvider.get_token()`)
arkasında **takılıp çıkarılabilir** dört sağlayıcı:

| Sağlayıcı | Açıklama | Kullanım |
|---|---|---|
| `noop` | Boş string gönderir | Upstream doğrulamıyorsa / test |
| `static` | `.env`'den elle alınan token | Hızlı başlangıç, kısa ömürlü (~2 dk) |
| `external` | Tek HTTP cevabında doğrudan token döndüren yapılandırılabilir çözücü API'si | Üretim |
| `browser` | Playwright ile hedef origin'de v3 `grecaptcha.execute()` veya Enterprise `grecaptcha.enterprise.execute()` çağırır, token'ı cache'ler (TTL 100 sn) | Bağımsız üretim |

Sağlayıcılar `TokenCache` (TTL + tek-uçuş `asyncio.Lock`) ile sarılır; eşzamanlı 100 istek
tek token üretimini paylaşır. Token reddi (upstream 403) → otomatik invalidate + 1 kez retry.

---

## 5. Güvenlik Önlemleri

- **Kimlik doğrulama:** `API_KEYS` çoklu anahtar listesi, sabit-zaman karşılaştırma; `/health` hariç tüm uçlar korumalı.
- **Gizli veri sızıntısı yok:** loglarda `Authorization`, `cookie`, `recaptchaV3Token` alanları `***` ile maskelenir (structlog processor).
- **Girdi sertleştirme:** `max_messages`, `max_prompt_chars`, `max_body_bytes` limitleri; aşımda 413/422.
- **SSRF koruması:** `TARGET_DOMAIN` yalnızca config'den gelir, istemci girdisiyle asla oluşturulmaz; şema `https` zorunlu.
- **Rate limit + eşzamanlılık tavanı:** global `asyncio.Semaphore` ile upstream'e paralel istek sınırı.
- **Timeout katmanları:** connect/read/write/pool ayrı ayrı; toplam istek için `asyncio.timeout`.
- **Circuit breaker:** ardışık N hatada 30 sn açık devre → hızlı 503, upstream'i yormaz.
- **Konteyner:** non-root kullanıcı, `--read-only` uyumlu, healthcheck.
- **Sırlar:** yalnız env/secret dosyası; `.env` git-ignore, `.env.example` şablon.
- **Yasal not:** README'de hedef servisin ToS'una uyumun kullanıcı sorumluluğunda olduğu belirtilir.

---

## 6. Dayanıklılık & Performans

- `uvloop` + `httptools`, tek paylaşılan `httpx.AsyncClient` (HTTP/2, keep-alive havuzu).
- Retry: yalnız idempotent hatalarda (429/5xx/ağ), üstel geri çekilme + full jitter, `Retry-After` saygısı.
- Kota istisnası: gövde `UPSTREAM_LIMIT_MARKERS` işaretlerinden birini taşıyorsa (örn. "upstream limit reached") istek **retry edilmez**; `429 upstream_quota_reached` + `Retry-After` döner. Kilitli hesapla tekrar denemek kilit süresini uzattığı için bu bilinçli bir tercihtir.
- Hesap havuzu: `UPSTREAM_COOKIE_2/3/4` yuvaları `app/services/account.py` içinde `Account`'a dönüşür; `AccountPool` kayan pencere sayar, kilitte hesabı dinlendirir ve bütçeyi AIMD ile öğrenir. Hesap geçişi yalnızca istemciye henüz olay gönderilmediyse yapılır (aksi halde iki yanıt karışır). Upstream sohbeti hesaba bağlıdır (`_session_key` sonuna hesap etiketi eklenir).
  - **Kota tespiti `CompletionService._events` içinde yapılır**, çağıran tarafta değil. Üç kaynak da orada yakalanır: HTTP hata gövdesi (client istisnayı akış bağlamının içinde fırlatır), `3:` hata olayı ve düz metin delta'sı (`QUOTA_TEXT_SCAN_CHARS > 0`). Sebep: `report_quota`/devir `except UpstreamQuotaExceeded` bloğunda yaşar; tespit dışarıda yapılırsa istisna o bloğun dışında fırlar ve hesap ne dinlenmeye alınır ne de bütçe öğrenilir.
  - "İstemciye içerik gitti mi" sayacı yalnızca **metin delta'sında** artar (`_CLIENT_VISIBLE_EVENTS = {TEXT}`). Akış yolunda istemciye iletilen tek şey `TEXT`'tir; `f:` (START), `g:`/`ag:` (REASONING), `DATA`, `TOOL_*` ve `FINISH` parse edilir ama tek bayt iletilmez. Bunlardan herhangi biri sayılırsa devir koşulu haksız yere kapanır: upstream her akışı `f:` ile açar, düşünme modelleri de cevap metninden önce `g:` delta'ları gönderir — ikisi de istemciye hiçbir şey ulaşmamışken `emitted > 0` yapardı.
  - `QuotaTextScanner` **deneme başına** yenilenir: tampon denemeler arasında korunursa birinci hesabın kota metni ikinci hesabın normal cevabıyla birleşir ve sağlam hesap da kilitlenir.
- Sohbet rotasyonu: `SESSION_REUSE=true` iken upstream `chat_id`'si API anahtarı parmak izine (`client_fingerprint`, SHA-256) bağlanır; `SESSION_ROTATE_AFTER_MESSAGES` / `SESSION_ROTATE_AFTER_SECONDS` eşiğinde yeni sohbete geçilir. OpenAI-uyumlu istemciler şema dışı `conversation_id` gönderemediği için bu gereklidir. **Dikkat:** istemci her istekte tüm geçmişi gönderir ve wrapper bunu tek prompt'a düzleştirir; sohbet yeniden kullanıldığında upstream kendi geçmişini de tutuyorsa bağlam iki kez hesaplanır. Rotasyon eşiği bu birikimi sınırlar.
- Streaming'de **hiç** tam yanıt biriktirilmez (non-stream hariç) → sabit bellek.
- Prometheus `/metrics`: istek sayacı, latency histogramı, TTFT (ilk token süresi), upstream hata sayacı.
- Graceful shutdown: lifespan içinde client/pool kapatma, aktif stream'lere drain süresi.

---

## 7. Edge-case Listesi (kodda karşılanacak)

boş `messages` · yalnız `system` mesajı · çok uzun prompt · `stream_options.include_usage` ·
`n>1` (desteklenmiyor → açık hata) · `stop` dizileri (yerel kesme) · istemci kopması ·
upstream yarım kesilen stream · geçersiz JSON satırı · UTF-8 çok baytlı karakterin chunk sınırında bölünmesi ·
upstream 403/429/5xx · recaptcha token süresi dolması · cookie süresi dolması · eşzamanlı token yenileme yarışı ·
model alias'ı · bilinmeyen model · yanlış API key · gövde limit aşımı · SSE `[DONE]` garantisi (finally bloğu).

---

## 8. Teslimat Kapsamı (AŞAMA 2)

Tam çalışan kod (placeholder yok) + `.env.example` + `config/models.yaml` + Dockerfile +
docker-compose + pytest testleri (respx ile upstream mock, stream parser birim testleri,
uçtan uca OpenAI SDK uyumluluk testi) + README (kurulum, örnek `curl`, OpenAI SDK örneği).

---

## Onay Bekleyen Kararlar

1. `TARGET_DOMAIN` ve model id'leri — gerçek değerler mi, yoksa tamamen config'den mi okunsun?
2. reCAPTCHA için hangi sağlayıcı varsayılan olsun?
3. Kalıcılık (Redis) gerekli mi, yoksa tek-süreç in-memory yeterli mi?
