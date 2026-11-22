# Dışarıdan Arka Arkaya İstek Atarken Hız Sınırına (`429`) Takılmama Formülü

Bu rehber, dış yazılımlardan, betiklerden veya otopilot akışlarından `ApiWrapper`
üzerinden hedef siteye arka arkaya seri istek gönderirken **geçici hız sınırına
(`429`), bot ivme kilidine (`prompt failed`) ve günlük kredi tükenmesine**
takılmamak için gereken kuralları ve ölçülmüş sınırları kalıcı olarak kayıt altına alır.

---

## 1. Hedef Sitenin Gerçek Sınırları (Özet Tablo)

| Sınır Katmanı | Gerçek Değer / Tavan | Aşıldığında Dönen Yanıt |
|---|---|---|
| **Tek mesaj karakter tavanı (`Max`)** | `120.000` karakter (~30k–35k token) | `400 Message is too long` |
| **Tek sohbet oturumu token tavanı** | Oturum bazlı toplam bütçe | `SESSION_TOKEN_LIMIT` (`"This session has reached its token usage limit..."`) |
| **Tek istek context penceresi** | Model context penceresi | `LONG_PROMPT` (`"Conversation is too long. Please start a new chat."`) |
| **Günlük ücretsiz kredi havuzu** | `2.500` kredi (`1.000 kredi = $1.00`, yani **`$2.50 / gün / hesap`**) | `429 {"error": "daily spend limit reached"}` + `RateLimit-Reset` |
| **Mod bazlı hız sınırı (`modality`)** | Kısa pencerede mod başına istek sıklığı | `429 {"error": "rate limit reached"}` + `Ratelimit-Modality` + `RateLimit-Reset` |
| **Model bazlı hız sınırı (`model`)** | Kısa pencerede model başına istek sıklığı | `429 {"error": "rate limit reached", "modelId": "..."}` + `RateLimit-Reset` |
| **Bot / ivme sınırı (`prompt_rate_limit`)** | Boşluksuz / insan dışı hızda ardışık gönderim | `429 {"error": "prompt failed"}` (tarayıcıda görsel reCAPTCHA v2 tetikler) |
| **Yeni hesap ivme kilidi** | Yeni açılmış Google hesabında hızlı yeni sohbet | `429 {"error": "verification required"}` veya `"You have started too many new chats today..."` |
| **Aynı sohbette çakışma kilidi** | Önceki cevap bitmeden aynı sohbete yeni mesaj | `409 {"error": "A reply of the last turn is still running"}` |
| **Agent Mode günlük mesaj tavanı** | `100` mesaj / 24 saat (otomatik bağlam sıkıştırma `%80` / `%90`) | `429` Agent günlük sınır mesajı |

### Neden `"prompt failed"` (`429`) Tetiklenir?
Tarayıcı her mesajda sunucuya üç sinyal gönderir:
1. Taze `recaptchaV3Token` (~2 dk ömürlü),
2. `cx` (`CLIENT_INTERACTION_FIELD`, v2 — 24 karakterlik etkileşim özeti: tuş basım süresi, fare hareketi, yazılan/yapıştırılan karakter sayısı, sekme kalp atışı ve dakika damgası),
3. İki mesaj arasındaki doğal insan bekleme aralığı.

cURL / API kullanımında statik `recaptchaV3Token` ~2 dakika sonra bayatladığı için
hedef site **hesabın geçmiş güven puanına, eşzamanlı istek olmamasına ve iki istek
arasındaki süreye** bakar. Arka arkaya `0–2 sn` arayla istek yağdırılırsa
`"prompt_rate_limit"` devreye girip `429 {"error": "prompt failed"}` döndürür.

---

## 2. Hız Sınırına Takılmama Formülü (5 Altın Kural)

### Kural 1 — İstekleri Doğrudan Hedefe Değil, Daima `ApiWrapper` Sırasından Geçir
`ApiWrapper` üzerinde hesap bazlı FIFO sırası ve rastgele bekleme **açık kalmalıdır**
(varsayılan olarak açıktır):

```env
REQUEST_QUEUE_ENABLED=true
REQUEST_GAP_MIN_SECONDS=15
REQUEST_GAP_MAX_SECONDS=30
```

* Dışarıdan aynı saniyede **10 istek arka arkaya** gönderilse bile `ApiWrapper`
  bunları tek sıraya alır:
  `1. istek başlar ve tamamen biter → 15–30 sn rastgele insan boşluğu beklenir → 2. istek gönderilir.`
* Böylece ne `409` çakışması ne de `0–2 sn` bot ivmesi oluşur.

### Kural 2 — Yoğun Trafikte İki Hesaplı Fermuar Dağıtımını (`LaneSet`) Kullan
`hesap-1` (`UPSTREAM_CURL`) ve `hesap-2` (`UPSTREAM_CURL_2` / `/upstream_curl2`)
birlikte açık olduğunda (*Agent Mode çalışmıyorken*):

* `ApiWrapper` gelen seri istekleri **`hesap-1 → hesap-2 → hesap-1 → hesap-2`**
  sırasıyla fermuar gibi dağıtır (`ACCOUNT_FAILOVER=true`).
* Dışarıdan her 15 saniyede bir istek gelse bile her bir Google hesabı **30–50 saniyede
  bir** istek görür ve günlük ücretsiz kredi bütçesi **`2 × 2.500 = 5.000` krediye (`$5.00`)** çıkar.
* **Dikkat:** `hesap-2` üzerinde Agent Mode çalıştırılacağı zaman `/upstream_curl2 kapat`
  ile `hesap-2`'nin `Max` bağlantısı tamamen kapatılmalıdır (aynı Google hesabında
  `Max` ve Agent Mode aynı anda çalışmamalıdır).

### Kural 3 — Dış İstemcide Anlık Otomatik Retry'ı Kapat (`max_retries=0`)
OpenAI SDK ve pek çok HTTP istemcisi varsayılan olarak hata/timeout anında `0.5 sn`
sonra kendi başına 2–3 kez tekrar istek atar. Bu davranış kuyruğu şişirir ve hız
sınırını uzatır:

* Dış istemcide **`max_retries=0`** ayarla; beklemeyi ve hesap devrini yalnızca
  `ApiWrapper` yönetsin.

### Kural 4 — Dış İstemci Timeout'unu Uzun Tut (`300–600 sn`) veya `/v1/ask` / `stream=True` Kullan
Arka arkaya 5 istek gönderdiğinde 5. istek sırasının gelmesi için öndeki 4 isteğin
bitmesini + aradaki `15–30 sn` boşlukları bekler:

* **Seçenek A (`/v1/ask` — otomasyonlar için en güvenli yol):** İlk 30 sn'de ve
  sonra her 15 sn'de bir `{"status":"wait"}` nabzı gönderdiği için Cloudflare,
  Render veya istemci bağlantıyı asla kesmez.
* **Seçenek B (`/v1/chat/completions`):** `stream=True` kullan veya istemci
  `timeout` değerini en az **`300–600` saniye** ver. Kısa timeout verip kopan
  akışsız isteği tekrar göndermek süreyi kısaltmaz, aksine iki kat kredi harcar.

### Kural 5 — Durumsuz (`create-evaluation`) + Kompakt İstem Kullan, Oturmuş Google Hesabı Seç
* Her istekte tüm geçmişi ham olarak biriktirip göndermek hem `SESSION_TOKEN_LIMIT`
  duvarına çarpar hem de günlük `2.500` krediyi birkaç turda bitirir. Uzun işlerde
  tüm konuşmayı yığmak yerine **yapısal özet hafıza + yalnızca son 1–2 turun özeti**
  (~4.000–7.000 karakter) gönderilmelidir.
* Sıfır açılmış Google hesapları yerine geçmişi olan, tarayıcıda birkaç normal
  sohbet yapılmış hesaplar kullanılmalıdır (`new_account_velocity` korumasına
  takılmamak için).

---

## 3. Hazır Dış İstemci Şablonları

### A. Python (`openai` SDK — Seri İsteklere Dayanıklı Kurulum)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<servis-adin>.onrender.com/v1",
    api_key="sk-...",
    timeout=600.0,   # Kuyrukta öndeki istekleri beklerken kopmaması için uzun tut
    max_retries=0,   # Anlık otomatik tekrar denemeyi kapat; sırayı ApiWrapper yönetir
)

yanit = client.chat.completions.create(
    model="max",
    messages=[{"role": "user", "content": "İstem metni..."}],
    stream=True,     # Proxy zaman aşımını önlemek için akışlı okuma önerilir
)
for parca in yanit:
    icerik = parca.choices[0].delta.content if parca.choices else None
    if icerik:
        print(icerik, end="", flush=True)
```

### B. Otomasyon / Arka Plan Sistemleri İçin `/v1/ask` (Önerilen)

`/v1/ask` ucu kuyrukta beklerken de işlem sürerken de düzenli canlılık sinyali
(`wait`) ürettiği için arka arkaya gönderilen uzun görevlerde bağlantı kopması
yaşanmaz. Hazır istemci örneği için bkz. [`ASK_API.md`](ASK_API.md) ve
`scripts/ask_client.py`.
