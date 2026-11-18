# `/v1/ask` — otomatik sistemler için "bekle / bitti / problem" ucu

> **Durum (18 Kasım 2026):** kod yazıldı ve test edildi (PR #56), **canlıda değil.**
> Canlıya alma koşulu ve adımları: [`ROLLBACK_2026_11_16.md` §13](ROLLBACK_2026_11_16.md).
> Bu belgedeki süreler gerçek bir sunucu üzerinde ölçülerek doğrulandı; gerçek
> upstream'e karşı ilk doğrulama deploy'dan sonra yapılacak (§8).

`/v1/chat/completions` bir insan ya da OpenAI SDK'sı içindir: cevap gelene dek
sessizdir, arızayı ayrıntılı hata koduyla bildirir. Bir alarm sistemi için ikisi
de yanlıştır — sessizlik "çalışıyor mu, asıldı mı" sorusunu cevapsız bırakır,
hata kodu ise karşı tarafa yorumlayamayacağı bir yük bindirir.

`/v1/ask` sözleşmeyi üç kelimeye indirir:

| Karşı sistem ne görür | Anlamı | Ne yapar |
|---|---|---|
| `wait` | İstek alındı, cevap hazırlanıyor | Beklemeye devam eder |
| `done` | Cevap hazır — **tamamı, tek parça** | Cevabı kullanır |
| `problem` | Cevap alınamadı (**neden söylenmez**) | **Yedek sistemine geçer** |

Neden söylenmez, çünkü karşı sistemin yapacağı şey nedene göre değişmez. Ayrıntı
Telegram'dan sana gelir (§6).

---

## 1. Sözleşme

### İstek

```
POST https://<servis>.onrender.com/v1/ask
Authorization: Bearer <API anahtarı>
Content-Type: application/json

{"prompt": "Soru metni"}
```

| Alan | Zorunlu | Açıklama |
|---|---|---|
| `prompt` | evet¹ | Sorulacak metin |
| `system` | hayır | Sistem talimatı (ör. "Türkçe ve kısa cevap ver") |
| `model` | hayır | Model adı (`GET /v1/models`). Verilmezse `ASK_DEFAULT_MODEL`, o da boşsa servis varsayılanı |
| `messages` | hayır¹ | `prompt` yerine OpenAI biçiminde mesaj listesi. İkisi birden gönderilemez |

¹ `prompt` ya da `messages`'tan tam biri. Tanınmayan alanlar yok sayılır.

Adresteki `/v1` öneki isteğe bağlıdır: `/ask` aynı uçtur.

**Her soru bağımsızdır:** sorular arasında bağlam taşınmaz; her istek upstream'de
yeni bir sohbet açar (`SESSION_REUSE` açık olsa bile).

### Yanıt

HTTP `200`, `Content-Type: text/event-stream`. Bağlantı açık kalır ve satırlar
aktıkça gelir:

```
: ok

data: {"status":"wait","elapsed":30}

data: {"status":"wait","elapsed":45}

data: {"status":"done","text":"...cevabın tamamı...","model":"max","elapsed":52.4,"id":"req_..."}
```

Arızada son satır şudur:

```
data: {"status":"problem","id":"req_..."}
```

Kurallar:

1. Yalnız `data:` ile başlayan satırlar sözleşmenin parçasıdır. Öneki at, kalanı
   JSON olarak çöz. Boş satırlar ve `:` ile başlayan satırlar (`: ok`) yok sayılır.
2. Yalnız `status` alanına bakılır. `done`'da cevap `text` alanındadır. Diğer
   alanlar (`elapsed`, `model`, `id`) bilgi içindir; ileride yenileri eklenebilir.
3. `done` ve `problem` **son satırdır.** Her istek tam bir tanesiyle biter.
4. Cevap, isteğin kabulünden sonraki 30 saniye içinde hazırsa hiç `wait` gelmez;
   ilk satır doğrudan `done`'dır.
5. Her `data:` satırı yalnız ASCII karakter içerir; Türkçe harfler ve emoji JSON
   kaçışıyla (`\u0131`, `\ud83d\ude00`) yazılır. Her JSON çözücü bunları kendiliğinden
   geri çevirir — elle bir şey yapmak gerekmez. (Nedeni: §9.)

### Karşı sistemin "problem" sayacağı durumlar

Sunucu çökse, yeniden başlasa ya da ağ kopsa bile yedeğe geçilebilmesi için
kuralın yarısı istemcidedir. **Şunların hepsi `problem`dir:**

| # | Durum | Örnek |
|---|---|---|
| 1 | `"status":"problem"` satırı | Kota doldu, oturum düştü, upstream cevap vermedi… |
| 2 | HTTP durum kodu `200` değil | `401` yanlış anahtar · `400` boş `prompt` · `404` bilinmeyen model · `429` hız sınırı · `5xx` |
| 3 | **45 saniye** boyunca hiçbir veri gelmedi | Sunucu asıldı, ara vekil bağlantıyı yuttu |
| 4 | Bağlantı `done`/`problem` gelmeden kapandı ya da hiç kurulamadı | Servis yeniden başlıyor (deploy) |
| 5 | Satır JSON olarak çözülemedi, `status` tanınmıyor ya da `done` geldi ama `text` boş | — |

45 saniye nereden geliyor: sunucu en geç 30. saniyede ilk `wait`'i, sonra 15
saniyede bir yenisini gönderir. En uzun olağan sessizlik 30 saniyedir; 45 saniye
ağ gecikmesi için 15 saniye pay bırakır.

> Daha önce konuşurken bu sınırı "40 sn" ve satırları `data:` öneksiz anlatmıştım.
> Uygulanan ve test edilen değerler bu belgedekilerdir: **45 sn** ve **`data:` önekli** satırlar.

---

## 2. Karşı sisteme verilecek hazır talimat

Aşağıdaki metni olduğu gibi karşı sistemin (ya da onu yazan yapay zekânın)
talimatlarına yapıştır. `{BASE_URL}` ve `{API_KEY}` yerlerini doldur.

```text
ApiWrapper'a soru sorma kuralları

AMAÇ
Bir metin (prompt) gönderip cevabını almak. Cevap alınamazsa YEDEK SİSTEME geçmek.

İSTEK
- POST {BASE_URL}/v1/ask
- Başlıklar: "Authorization: Bearer {API_KEY}" ve "Content-Type: application/json"
- Gövde (JSON): {"prompt": "<soru metni>"}
  İsteğe bağlı alanlar: "system" (talimat metni), "model" (model adı).
- Her soru ayrı bir istektir. Sorular arasında bağlam taşınmaz; gereken her şeyi
  prompt'un içine yaz.

YANIT
- HTTP 200 döner ve bağlantı açık kalır; veri satır satır gelir.
- Yalnız "data:" ile başlayan satırlara bak. Öneki at, kalanını JSON olarak çöz.
  Boş satırları ve ":" ile başlayan satırları yok say.
- JSON'da yalnız "status" alanına bak:
    "wait"    -> İstek alındı, cevap hazırlanıyor. Bir şey yapma, okumaya devam et.
                 (İlki yaklaşık 30 saniye sonra, sonrakiler 15 saniyede bir gelir.
                 Cevap 30 saniyeden önce hazırsa hiç gelmez.)
    "done"    -> Cevap hazır. Cevabın tamamı "text" alanındadır, tek parçadır.
                 Bağlantıyı kapat ve cevabı kullan.
    "problem" -> Cevap alınamadı. Bağlantıyı kapat ve YEDEK SİSTEME geç.
- "done" ve "problem" son satırdır; ardından başka bir şey gelmez.
- Diğer alanları ("elapsed", "id", "model") yok say. İleride yeni alanlar eklenebilir.

ŞUNLARIN HEPSİ "problem" SAYILIR — YEDEK SİSTEME GEÇ:
1. "status" değeri "problem" olan satır.
2. HTTP durum kodunun 200 olmaması (401, 400, 404, 429, 5xx...).
3. 45 saniye boyunca hiçbir veri gelmemesi.
4. Bağlantının "done" ya da "problem" gelmeden kapanması ya da hiç kurulamaması.
5. Satırın JSON olarak çözülememesi, "status" değerinin tanınmaması ya da "done"
   geldiği hâlde "text" alanının boş olması.

ZAMAN AŞIMI AYARLARI
- Bağlanma zaman aşımı: 10 saniye.
- Okuma zaman aşımı (iki veri arasındaki en uzun sessizlik): 45 saniye.
- TOPLAM süre sınırı koyma. Kullandığın HTTP kütüphanesinin varsayılan bir toplam
  süre sınırı varsa (ör. 30 sn, 60 sn, 5 dk) kapat ya da en az 240 saniye yap;
  yoksa hazırlanmakta olan geçerli bir cevabı yarıda kesersin.
- Kendi işin gereği bir üst sınırın varsa dolduğunda bağlantıyı kapat ve yedek
  sisteme geç. Bağlantıyı kapatman yeterlidir; sunucu işi iptal eder.

YAPMA
- "problem" aldığın soruyu yeniden gönderme; o soru için yedek sistemi kullan.
- Bir sonraki soruda yine önce burayı dene. Arıza sürüyorsa "problem" cevabı
  beklemeden, anında gelir; düzeldiğinde kendiliğinden yeniden cevap vermeye başlar.
- Arızanın nedenini anlamaya çalışma. Neden sana bildirilmez; servis sahibine
  ayrıca bildirilir.

BİLGİ
- İstekler sunucuda sırayla, tek tek işlenir ve iki istek arasında 5-10 saniye
  beklenir. Art arda birkaç soru gönderebilirsin; sırada bekleyen istek de "wait"
  satırlarını almaya devam eder.
- Bir istek işlenmeye başladıktan sonra en geç 180 saniye içinde "done" ya da
  "problem" gelir.
- Satırların tamamı ASCII'dir; Türkçe harfler JSON kaçışıyla (\u0131 gibi) gelir.
  JSON çözücün bunları kendiliğinden doğru karaktere çevirir.
```

---

## 3. Örnek istemci

### En basit hâli (Python, `requests`)

Satır satır okumak zorunlu değildir. Gövdenin tamamını alıp **son** `data:`
satırına bakmak yeterlidir; okuma zaman aşımı "iki veri arasındaki sessizlik"
olarak çalıştığı için `wait` satırları bağlantıyı kendiliğinden canlı tutar.
(`requests` ve `httpx` ile gerçek sunucuya karşı denendi.)

```python
import json
import requests

def ask(prompt: str) -> str | None:
    """Cevabı döndürür; None ise yedek sisteme geç."""
    try:
        response = requests.post(
            "https://<servis>.onrender.com/v1/ask",
            json={"prompt": prompt},
            headers={"Authorization": "Bearer sk-..."},
            timeout=(10, 45),          # (bağlanma, sessizlik) — toplam sınır YOK
        )
        if response.status_code != 200:
            return None
        lines = [line for line in response.text.splitlines() if line.startswith("data:")]
        event = json.loads(lines[-1][5:])
        if event.get("status") == "done" and event.get("text"):
            return event["text"]
        return None
    except Exception:                  # ağ hatası, zaman aşımı, bozuk satır, boş gövde
        return None
```

### Satır satır okuyan hâli (ek paket gerekmez)

[`scripts/ask_client.py`](../scripts/ask_client.py) — yalnız standart kitaplık,
`wait` satırlarını da bildirir. Karşı sisteme olduğu gibi kopyalanabilir; aynı
dosya elle deneme aracıdır:

```bash
python scripts/ask_client.py https://<servis>.onrender.com sk-... "Tek kelimeyle cevap ver: merhaba"
```

```
[bekle] 30 sn            ← stderr (cevap 30 sn'den uzun sürerse)
merhaba                  ← stdout: yalnız cevap
[bitti] 34.2 sn, 7 karakter
```

Çıkış kodu `0` = cevap alındı, `1` = problem. Bu dosyanın sözleşmenin bütün
kurallarını uyguladığı `tests/test_ask_client_script.py` ile sınanır.

### cURL (elle bakmak için)

```bash
curl -N https://<servis>.onrender.com/v1/ask \
  -H "Authorization: Bearer sk-..." -H "Content-Type: application/json" \
  -d '{"prompt":"Tek kelimeyle cevap ver: merhaba"}'
```

`-N` çıktının tamponlanmadan, geldiği anda gösterilmesini sağlar.

---

## 4. Sıra ve süreler

Upstream'e giden **bütün** istekler — `/v1/ask`, `/v1/chat/completions`, Telegram
mesajları ve keepalive probe'u — tek sıradan geçer.

```
istek 1 ──▶ işleniyor ──▶ cevap ──▶ 5–10 sn bekleme ──▶ istek 2 ──▶ işleniyor ──▶ …
istek 2 ──▶ sırada (wait, wait, …)
istek 3 ──▶ sırada (wait, wait, …)
```

| Kural | Değer | Ayar |
|---|---|---|
| Aynı anda işlenen istek | **1** | `REQUEST_QUEUE_ENABLED` |
| İki istek arası bekleme | rastgele **5–10 sn**, önceki isteğin bitişinden sayılır | `REQUEST_GAP_MIN_SECONDS` / `REQUEST_GAP_MAX_SECONDS` |
| Sıra düzeni | geliş sırası (FIFO); öncelik yok | — |
| Sırada bekleyebilecek istek | en çok **20**; fazlası beklemeden `problem` | `REQUEST_QUEUE_MAX_WAITING` |
| İlk `wait` | kabulden **30 sn** sonra | `ASK_FIRST_WAIT_SECONDS` |
| Sonraki `wait`'ler | **15 sn**'de bir | `ASK_WAIT_INTERVAL_SECONDS` |
| İşlem süresi sınırı | **istek başına 180 sn**; saat sıra gelince başlar | `ASK_REQUEST_TIMEOUT_SECONDS` |
| Sırada en uzun bekleme | **600 sn**; dolarsa `problem` | `ASK_MAX_QUEUE_WAIT_SECONDS` |

Ayrıntılar:

* **`wait` sayacı kabul anından sayar**, sıra beklemesi dahil. Sırada bekleyen
  istek de 30. saniyede ilk `wait`'ini alır; karşı sistem "asıldı" sanmaz.
* **180 sn sırada geçen süreyi kapsamaz.** Dördüncü sıradaki istek, önündekiler
  yüzünden cezalandırılmaz: sırası geldiğinde 180 saniyenin tamamı onundur.
* **Aralık yalnız upstream'e gerçekten gidilmiş isteklerden sonra uygulanır.**
  Geçersiz gövdeyle reddedilen ya da sırada beklerken vazgeçilen istek, arkasındakini
  5–10 sn bekletmez.
* **Karşı sistem bağlantıyı kapatırsa iş iptal edilir:** sırada bekliyorsa sıradan
  çıkar, işleniyorsa upstream bağlantısı kapatılır ve sıra bir sonrakine geçer.
  Vazgeçilmiş bir istek ne kotayı harcar ne arkadakileri bekletir.
* **Telegram mesajları da aynı sıradadır.** Bot wrapper'dan cevabı 180 sn bekler;
  önünde uzun istekler varsa "zaman aşımı" yazar ve isteği sıradan çıkar. Yeniden
  yazman yeterlidir.

Ölçülen örnek (süreler küçültülmüş ayarlarla, gerçek soket üzerinde): arka arkaya
4 istek → upstream'e aynı anda en çok **1** istek gitti, cevaplar geliş sırasıyla
döndü, ardışık iki upstream isteği arasında işlem süresi + aralık kadar boşluk vardı.

---

## 5. `problem` ne zaman döner

| Durum | İç kod | Tür |
|---|---|---|
| Kota doldu (`429 … spend limit reached` vb.) | `upstream_quota_reached` | geçici |
| Upstream isteği yavaşlattı (`429`, kota işareti yok) | `upstream_rate_limited` | geçici |
| reCAPTCHA reddi | `recaptcha_rejected` | geçici |
| Upstream 180 sn içinde cevap vermedi | `upstream_timeout` | geçici |
| Upstream 5xx / ağ hatası | `upstream_failure`, `upstream_unavailable` | geçici |
| Cevap boş geldi | `empty_response` | geçici |
| Sıra dolu / sırada 600 sn bekledi | `queue_full`, `queue_timeout` | geçici (yük) |
| **Oturum düştü** (`401`) | `upstream_unauthorized` | **kalıcı** |
| **cURL eskidi** (`409`/`426`) | `upstream_refresh_required` | **kalıcı** |
| reCAPTCHA token'ı üretilemiyor | `recaptcha_unavailable` | **kalıcı** |

Karşı sistem bu ayrımı görmez; sen Telegram mesajındaki `Tür:` satırında görürsün.

### Arıza bilinirken: beklemeden `problem`

Ciddi bir arızadan sonra **300 saniye** (`ASK_PROBLEM_HOLD_SECONDS`) boyunca yeni
`/v1/ask` istekleri upstream'e **gönderilmeden**, anında `problem` alır. Alarm
sistemi kotası dolmuş bir hesaba 180 saniye boyunca boşuna bakmaz; yedeğine
milisaniyeler içinde geçer.

```
12:00:00  istek A → upstream 429 → problem          (Telegram: 1 mesaj)
12:00:40  istek B → upstream'e gidilmedi → problem   (sayıldı, mesaj yok)
12:03:10  istek C → upstream'e gidilmedi → problem   (sayıldı, mesaj yok)
12:05:01  istek D → süre doldu: TEK deneme → upstream 200 → done
                                                     (Telegram: "yeniden çalışıyor · 3 dış istek problem aldı")
```

* Süre dolunca **tek istek** deneme olarak geçirilir; o sürerken gelenler yine
  anında `problem` alır. Deneme başarılıysa kapı herkese açılır, başarısızsa
  yeni bir 300 sn başlar.
* Upstream `Retry-After` gönderdiyse ve 300 sn'den uzunsa o süre kullanılır
  (en çok 1800 sn).
* Arıza öğrenildiği anda **sırada bekleyen** `/v1/ask` istekleri de hemen
  `problem` alıp çıkar; tek tek denenmezler.
* Bekletme **yalnız `/v1/ask`'i** etkiler. Telegram mesajların ve probe her zaman
  denenir — ve onlardan birinin başarısı bekletmeyi hemen kaldırır. Tersine, bir
  Telegram mesajının ya da probe'un gördüğü arıza da `/v1/ask`'i korur.
* `ASK_PROBLEM_HOLD_SECONDS=0` → bekletme kapalı, her istek upstream'e gider.

---

## 6. Sana ne gelir: Telegram

Karşı sisteme tek kelime, sana ayrıntı. Kural: **olay başına tek mesaj.**

### İlk arıza

```
⚠️ ApiWrapper — dış istek cevaplanamadı; karşı sisteme "problem" döndü.
Ne oldu: Upstream hesabı geçici olarak kilitledi (kota penceresi doldu). [upstream_quota_reached]
Tür: GEÇİCİ sayılır — önce bekleyin, dokunmayın.
Yapılacak: Retry-After / pencere süresi dolana kadar bekleyin; … Israrla denemek kilidi uzatır.
Upstream cevabı: HTTP 429 · {"error":"daily spend limit reached"}
İstek req_u06DD9xj4fHkcimoab9b · sırada 0 sn · işlem 0 sn
Bundan sonra: dış isteklere 5 dk boyunca upstream'e gidilmeden "problem" denir; sonra tek istek denenir.
2026-11-18 19:56 UTC
```

| Satır | Ne işe yarar |
|---|---|
| `Ne oldu … [kod]` | Arızanın adı ve kodu |
| `Tür: GEÇİCİ / KALICI` | **Dokunmalı mıyım?** Geçici → bekle. Kalıcı (🔴) → elle müdahale |
| `Yapılacak` | O koda özel adım |
| `Upstream cevabı` | Upstream'in **kendi** cevabı: durum kodu + gövdenin özeti. Etikete değil buna güven (18 Kasım dersi, `LESSONS §16`). Token/çerez gibi uzun değerler `[gizlendi]` yazılır |
| `İstek req_…` | Render logunda aynı `request_id` ile aranır |
| `Bundan sonra` | Bekletmenin ne kadar süreceği |

Oturum düşmesi aynı biçimde ama kırmızı ve kalıcı olarak gelir:

```
🔴 ApiWrapper — dış istek cevaplanamadı; karşı sisteme "problem" döndü.
Ne oldu: Upstream oturum kimliğini reddetti (HTTP 401): oturum düştü. [upstream_unauthorized]
Tür: KALICI — elle müdahale gerekir.
Yapılacak: Kendiliğinden düzelmez. Google ile giriş yapılmış tarayıcıdan taze cURL alıp UPSTREAM_CURL'ü güncelleyin ve yeniden deploy edin; …
Upstream cevabı: HTTP 401 · {"error":"unauthorized"}
```

### Arıza sürerken

Aynı arıza sürdükçe gelen istekler **sayılır, mesaj atılmaz.** Arıza kodu
değişirse (ör. kota → oturum düştü) yeni mesaj gelir. Arıza
`TELEGRAM_ALERT_REPEAT_SECONDS` (6 saat) boyunca sürerse tek bir hatırlatma gelir
ve o ana dek kaç dış isteğin `problem` aldığını yazar.

### Düzelince

```
✅ ApiWrapper — upstream yeniden çalışıyor.
Önceki arıza: upstream_quota_reached (Upstream hesabı geçici olarak kilitledi (kota penceresi doldu).)
Süre: 2 sa 14 dk · 2026-11-18 22:10 UTC · HTTP 200
Arıza boyunca "problem" cevabı alan dış istek: 7
```

Son satır, alarm sisteminin o arıza boyunca kaç kez yedeğe düştüğünü söyler.

### Arıza kaydı açmayan olaylar

Upstream'le ilgisi olmayan sorunlar (`queue_full`, `queue_timeout`, wrapper içi
hata) arıza sayılmaz: bekletme başlatmaz, "düzeldi" mesajı üretmez. Tür başına
**saatte en çok bir** mesaj gelir; aradaki tekrarlar bir sonraki mesajda
"aynı türden N olay daha oldu" diye yazılır.

### Karşı sistemin kendi hatası: `REDDEDİLDİ`

Yanlış anahtar, bilinmeyen model adı ya da boş `prompt` akış başlamadan HTTP 4xx
olarak döner. Karşı sistem bunu da `problem` sayıp yedeğine geçer (kural 2) —
yani bir **kurulum hatası** düzeltilmezse her soru sessizce yedeğe gider ve asıl
sistemin hiç kullanılmadığı fark edilmez. Bu yüzden sana bildirilir:

```
⚠️ ApiWrapper — dış istek REDDEDİLDİ (isteğin kendisi hatalı); karşı sistem bunu "problem" sayıp yedeğine geçer.
Ne oldu: API anahtarı reddedildi (401). [invalid_api_key]
Tür: KURULUM HATASI — düzeltilene dek aynı istekler hep yedeğe gider. Upstream'de sorun yok.
Yapılacak: Karşı sistemdeki API anahtarını kontrol edin: 'Authorization: Bearer <anahtar>' başlığı servisteki API_KEYS değerlerinden biriyle birebir aynı olmalı.
Sunucunun cevabı: HTTP 401 · Incorrect API key provided.
İstek req_J1FGPS41Nkc0qPMpiVwU
```

Arıza kaydı açmaz, bekletme başlatmaz (upstream'de sorun yoktur). Tür başına
**saatte en çok bir** kez yazılır. Gönderilen anahtar mesaja yazılmaz.
`/v1/chat/completions` ucundaki 4xx'ler bildirilmez; o ucun istemcisi hatayı
kendi görür.

### Probe (keepalive) ile ilişkisi

Probe ve `/v1/ask` **aynı arıza kaydını** kullanır: arızayı hangisi önce görürse
mesajı o tetikler, diğeri yalnız sayılır. Böylece tek arıza için iki mesaj gelmez.

Probe ile ilgili değişenler:

* Son aralıkta gerçek bir istek başarıyla cevaplandıysa **probe atılmaz**, o
  istekten bir aralık sonraya ertelenir (`UPSTREAM_KEEPALIVE_SKIP_WHEN_ACTIVE`).
  Gerçek trafik oturumu zaten tazeler; probe kotayı boşa harcamaz.
* Sıra meşgulken probe bekler. Araya istek girerse sırada en çok 60 sn bekler;
  sıra yüzünden atlanan probe **arıza sayılmaz** ve yarım dakika sonra yeniden denenir.
* Cevabı geciken probe artık `upstream_timeout` olarak bildirilir.
  `wrapper_unreachable` yalnız bağlantı hiç kurulamadığında kullanılır
  (18 Kasım'daki yanlış etiketin düzeltmesi, `LESSONS §16.2`).
* `429 … spend limit reached` artık kota olarak tanınır ve **tekrar denenmez**
  (önceden 3 kez deneniyor, istek dakikalarca asılı kalıyordu).

---

## 7. Teşhis

### `GET /v1/admin/queue`

"Karşı sistem neden `problem` alıyor?" sorusunun tek bakışta cevabı (API anahtarı ister):

```bash
curl -s https://<servis>.onrender.com/v1/admin/queue -H "Authorization: Bearer sk-..."
```

```json
{
  "queue": {"enabled": true, "active": "ask", "active_seconds": 12.4,
            "waiting": 2, "waiting_labels": ["ask", "telegram"],
            "gap_seconds": [5.0, 10.0], "gap_remaining_seconds": 0.0,
            "max_waiting": 20, "served_total": 41},
  "upstream_health": {"failing": true, "code": "upstream_quota_reached", "failures": 1,
                      "failing_seconds": 95.2, "hold_remaining_seconds": 204.8,
                      "trial_in_flight": false, "hold_seconds": 300.0,
                      "seconds_since_success": 1840.5},
  "ask": {"first_wait_seconds": 30.0, "wait_interval_seconds": 15.0,
          "request_timeout_seconds": 180.0, "max_queue_wait_seconds": 600.0,
          "problem_hold_seconds": 300.0, "default_model": null, "in_flight": 3},
  "keepalive": {"interval_seconds": 3600.0, "skip_when_active": true},
  "alerts": {"enabled": true, "active_code": "upstream_quota_reached",
             "failures": 4, "ask_problems": 3, "sent_total": 1}
}
```

| Alan | Okuması |
|---|---|
| `queue.active` / `waiting_labels` | Şu an kim işleniyor, kimler bekliyor: `ask`, `api`, `telegram`, `keepalive` |
| `upstream_health.failing` + `hold_remaining_seconds` | Arıza biliniyor mu; `/v1/ask` daha kaç saniye anında `problem` dönecek |
| `upstream_health.seconds_since_success` | Son başarılı upstream cevabından bu yana geçen süre |
| `alerts.ask_problems` | Süren arızada `problem` alan dış istek sayısı |

Bu uç upstream'e istek atmaz; kota harcamaz, oturuma dokunmaz.

### Loglar (Render live tail)

| Olay | Anlamı |
|---|---|
| `ask_done` | Dış istek cevaplandı (`waited_ms`, `duration_ms`, `content_chars`) |
| `ask_problem` | Dış isteğe `problem` döndü — `code` arızayı, `reason` yolu söyler: `failed` (upstream'e gidildi, başarısız), `held` (bekletme: gidilmedi), `drained` (sırada beklerken arıza öğrenildi), `empty` (boş cevap), `overload` (sıra dolu / sırada süre doldu), `rejected`, `internal` |
| `ask_abandoned` | Karşı sistem beklemeden bağlantıyı kapattı; iş iptal edildi |
| `ask_rejected` | Karşı sistemin isteği hatalıydı (HTTP 4xx): `code` nedenini söyler |
| `request_queued` / `request_dequeued` | İstek sıraya girdi / sırası geldi (`waited_seconds`) |
| `upstream_hold_started` / `upstream_hold_cleared` | Bekletme başladı / kalktı |
| `upstream_http_error … body=` | Upstream'in ham cevabı — **asıl kanıt** |
| `upstream_keepalive_deferred` | Gerçek trafik olduğu için probe ertelendi |
| `upstream_keepalive_skipped` | Probe sırada kaldı; arıza sayılmadı |
| `request_queue_stale_slot` | Emniyet supabı devreye girdi (olmaması gerekir; görürsen bildir) |

`ask_problem` satırındaki `ask_id`, Telegram mesajındaki `İstek req_…` ile aynıdır.

### Metrikler (`/metrics`)

`apiwrapper_ask_total{status=done|problem|abandoned|rejected}` ·
`apiwrapper_request_queue_waiting` · `apiwrapper_request_queue_wait_seconds` ·
`apiwrapper_request_queue_rejected_total{reason=full|timeout|stale}`

---

## 8. Ayarlar ve canlıya alma

Hiçbir ayar zorunlu değildir; varsayılanlar bu belgede anlatılan davranışı verir.
Tam liste ve açıklamalar: `.env.example` ("istek sırası" ve "/v1/ask" bölümleri).

| Ayar | Varsayılan | |
|---|---|---|
| `ASK_FIRST_WAIT_SECONDS` | `30` | İlk `wait` |
| `ASK_WAIT_INTERVAL_SECONDS` | `15` | Sonraki `wait`'ler |
| `ASK_REQUEST_TIMEOUT_SECONDS` | `180` | İstek başına işlem süresi |
| `ASK_MAX_QUEUE_WAIT_SECONDS` | `600` | Sırada en uzun bekleme |
| `ASK_PROBLEM_HOLD_SECONDS` | `300` | Arıza bilinirken bekletme (`0` = kapalı) |
| `ASK_DEFAULT_MODEL` | boş | İstekte model yoksa kullanılacak |
| `REQUEST_QUEUE_ENABLED` | `true` | Tek sıra (`false` = eski, eşzamanlı davranış) |
| `REQUEST_GAP_MIN_SECONDS` / `…MAX…` | `5` / `10` | İstekler arası rastgele bekleme |
| `REQUEST_QUEUE_MAX_WAITING` | `20` | Sıra derinliği |
| `UPSTREAM_KEEPALIVE_SKIP_WHEN_ACTIVE` | `true` | Trafik varken probe atma |

> ⚠️ **Sessizlik sınırıyla bağlantı:** karşı sistem 45 sn sessizlikte vazgeçer.
> `ASK_FIRST_WAIT_SECONDS` 45'e yaklaşırsa ya da `ASK_WAIT_INTERVAL_SECONDS`
> büyütülürse karşı taraf sağlıklı istekleri de `problem` sayar. Bu ikisini
> değiştireceksen karşı sistemin sınırını da birlikte değiştir.

> ⚠️ **Her ayar değişikliği restart, her restart oturum ölümüdür** (`LESSONS §16.5`).
> Ayarları tek tek deneme; cURL'ün zaten yenileneceği ana sakla.

Canlıya alma ve sonrasındaki deneme adımları: [`ROLLBACK_2026_11_16.md` §13](ROLLBACK_2026_11_16.md).

---

## 9. Bilinen sınırlar (dürüst liste)

1. **Gerçek upstream'e karşı henüz denenmedi.** 605 otomatik test var; zamanlama
   ve bağlantı kopması gerçek bir sunucu (uvicorn) üzerinde, sahte bir upstream'e
   karşı sınandı. Gerçek servise karşı ilk deneme deploy'dan sonra yapılacak.
2. **Sıra ve bekletme bellektedir.** Servis yeniden başlarsa sıradaki istekler
   kopar (karşı sistem kural 4 ile `problem` sayar), bekletme sıfırlanır. Tek
   süreç varsayılır (`Dockerfile`: `--workers 1`, `render.yaml`: `numInstances: 1`);
   ikinci bir süreç ikinci bir sıra demektir.
3. **Öncelik yok.** Alarm isteği, önündeki Telegram mesajının ya da probe'un
   bitmesini bekler (en kötü durumda 180 sn + 5–10 sn). Günde ~100 istekte bu
   nadirdir; sıklaşırsa öncelik eklenebilir.
4. **Cevap tek parçadır.** Upstream 180 sn içinde bitiremediği cevabı kısmen de
   olsa vermez; `problem` döner.
5. **Yineleme denetimi yok.** Aynı soru iki kez gönderilirse iki kez işlenir
   (iki upstream isteği). Karşı sistem beklemekten vazgeçip bağlantıyı kapatırsa
   iş iptal edilir; cevap tam o anda hazırlanmışsa atılır ve o upstream isteği
   boşa gitmiş olur.
6. **Bekletme arıza türünü ayırt etmez.** Tek seferlik bir 5xx de 300 sn bekletme
   başlatır; o sürede gelen alarmlar yedeğe düşer. Bedeli birkaç dakikalık
   gereksiz yedek kullanımıdır; karşılığında alarm sistemi arızalı bir upstream'i
   hiç beklemez. Fazla gelirse `ASK_PROBLEM_HOLD_SECONDS` düşürülür.
7. **Hız sınırı `/v1/ask` için de geçerlidir** (anahtar başına 120 istek/dk,
   patlama 30). Aşılırsa akış başlamadan `429` döner — karşı sistem için `problem`.
8. **`prompt` uzunluğu** diğer uçlarla aynı sınırlara tabidir (gövde 4 MB,
   `MAX_PROMPT_CHARS`). Aşılırsa `400`.

### Uygulama sırasında ölçülüp düzeltilenler

Bunlar "olabilir" diye değil, gerçek sunucuda **görülerek** bulundu
(ayrıntı: `LESSONS_2026_11_14.md` §17):

| Bulgu | Sonuç |
|---|---|
| Cevapta U+2028 / U+2029 / U+0085 geçince `data:` satırı Python'un satır okuyucularında ikiye bölünüyor, JSON çözülemiyordu | Satırlar yalnız ASCII (kural 5) |
| `request.is_disconnected()` akışsız uçta kopmayı 3 sn sonra bile görmüyordu | `receive()` dinleniyor; kopma ≤0,5 sn'de algılanıyor |
| Yarıda biten akış yanıtı sırayı kilitliyordu (6 sn sonra hâlâ kilitli; emniyet supabı 30 dk) | Akış yanıtları her çıkışta üreticiyi kapatıyor |
| Upstream bir emojiyi iki parçaya bölerse akışsız uç HTTP 500 dönüyor, akışlı uç kesiliyordu | Yarımlar birleştiriliyor |
| `SESSION_REUSE=true` iken alarm soruları birbirinin ve Telegram sohbetinin bağlamına karışıyordu | `/v1/ask` her soruda yeni sohbet açıyor |
| Günlük akış kontrolü, gövde içinde bildirilen hatayı "veri aktı" diye başarı sayıyordu | Hata olarak sınıflanıyor; yanlış "düzeldi" mesajı üretmiyor |
