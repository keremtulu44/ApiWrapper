# ApiWrapper — Tüketici Rehberi (başka projeler için)

Bu servis **OpenAI-uyumlu** bir API'dir. Kendi projende yeni bir istemci
yazmana gerek yok: mevcut OpenAI SDK'nda yalnız **iki değeri** değiştirirsin.

```python
base_url = "https://<servis-adin>.onrender.com/v1"   # 1
api_key  = "sk-…"                                    # 2
```

> Anahtarı sana servisi işleten kişi verir. Her projeye **ayrı** anahtar verilir;
> böylece kullanım proje bazında görülür ve bir proje iptal edildiğinde diğerleri
> etkilenmez.

> ## ⚠️ SÜRÜM NOTU (19 Kasım 2026)
>
> Bu rehber `77d4011` sürümü için yazıldı; canlıdaki kod bir geri dönüş
> sürümüdür (`ROLLBACK_2026_11_16.md`). Bölüm bölüm:
>
> | Bölüm | Bu sürümde |
> |---|---|
> | §1 Hızlı başlangıç · §3 Hatalar · §4 Hız sınırları | ✅ |
> | §2 Modeller | ⚠️ Yalnız **`max`**. `web` modeli ve `Kaynaklar:` bloğu **yok**; `model=web` isteği `model_not_found` alır |
> | §5 İşletici tarafı | ❌ `ADMIN_API_KEYS`, `/v1/admin/keys`, `API_KEY_LABELS` **yok**. Aşağıya bak |
> | §6 Bilinen sınırlar | ✅ |
> | §7 `/v1/ask` · §8 İstek sırası | PR #56 (canlıda değil). §8'deki "tek sıra", ikinci hesap eklenince **hesap başınadır** (`TWO_ACCOUNTS.md §4`) |
>
> **⚠️ Güvenlik — §5'teki "önce `ADMIN_API_KEYS` ayır" adımı bu sürümde hiçbir
> şeyi korumaz.** O ad okunmaz. Bu sürümde `API_KEYS`'teki **her** anahtar
> `/v1/admin/*` uçlarını da açar (hesap sıfırlama, oturum temizleme, ayar özeti).
> Yani bir tüketiciye `API_KEYS`'ten anahtar vermek ona yönetim yetkisi de
> vermektir. Anahtar üretmek/iptal etmek için uç da yok: liste Render env'indeki
> `API_KEYS`'tir ve değiştirmek bir **restart**'tır (oturum ölür).
>
> Yalnız soru soracak bir otomasyon için PR #56 ayrı bir liste getirir:
> **`ASK_API_KEYS`** — yalnız `/v1/ask`'te geçerlidir, başka hiçbir ucu açmaz
> (`ASK_API.md §8`).

---

## 1. Hızlı başlangıç

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<servis-adin>.onrender.com/v1",
    api_key="sk-…",
)

resp = client.chat.completions.create(
    model="max",
    messages=[{"role": "user", "content": "Merhaba, kendini tanıt."}],
)
print(resp.choices[0].message.content)
```

### Akışlı (stream)

```python
stream = client.chat.completions.create(
    model="max",
    messages=[{"role": "user", "content": "Kısa bir şiir yaz."}],
    stream=True,
    stream_options={"include_usage": True},   # istersen token sayacı da gelir
)
for chunk in stream:
    delta = chunk.choices[0].delta.content if chunk.choices else None
    if delta:
        print(delta, end="", flush=True)
```

### PowerShell

```powershell
$B = "https://<servis-adin>.onrender.com"
$K = "sk-…"
$body = @{
  model    = "max"
  messages = @(@{ role = "user"; content = "Merhaba" })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$B/v1/chat/completions" `
  -Headers @{ Authorization = "Bearer $K" } `
  -ContentType "application/json" -Body $body
```

> Depodaki hazır betik: `scripts\windows\chat.ps1 "sorun" -BaseUrl $B -ApiKey $K`.

### cURL

```bash
curl https://<servis-adin>.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer sk-…" \
  -H "Content-Type: application/json" \
  -d '{"model":"max","messages":[{"role":"user","content":"Merhaba"}]}'
```

---

## 2. Modeller

Mod seçimi **yalnız `model` alanıyla** yapılır. Ayrı bir uç nokta, ayrı bir şema
yok.

| `model` | Ne yapar | Yanıt biçimi |
|---|---|---|
| `max` | Genel sohbet (varsayılan) | Düz metin |
| `web` | Güncel bilgi için web araması | Metin + sonda **`Kaynaklar:`** listesi |

Hangi modellerin açık olduğunu her zaman canlıdan oku:

```bash
curl https://<servis-adin>.onrender.com/v1/models -H "Authorization: Bearer sk-…"
```

Listede görünmeyen bir mod çalışıyor olabilir ama **desteklenmez** (ör. görsel
üretimi vitrinden indirildi: hedef taraf sık sık `429` veriyor).

`web` yanıtının sonundaki blok:

```
Kaynaklar:
- [Başlık](https://…)
- YouTube
```

Kaynakları ayıklamak istersen son `Kaynaklar:` satırından sonrasını kes; metnin
geri kalanı normal yanıttır.

---

## 3. Hatalar ve tekrar deneme

Yanıt gövdesi her zaman OpenAI zarfındadır:

```json
{"error": {"message": "…", "type": "…", "param": null, "code": "…"}}
```

| Durum | `type` | `code` | Ne yapmalısın |
|---|---|---|---|
| `401` | `invalid_request_error` | `invalid_api_key` | Anahtar yanlış ya da iptal edilmiş. **Tekrar deneme**; işleticiye sor |
| `400` | `invalid_request_error` | `invalid_request` | Gövde şeması bozuk (ör. `messages` yok). Gövdeyi düzelt |
| `404` | `invalid_request_error` | `model_not_found` | `model` adını `GET /v1/models` listesinden seç |
| `429` | `rate_limit_error` | `rate_limit_exceeded` | **`Retry-After` başlığındaki saniye kadar bekle**, sonra devam et |
| `429` | `rate_limit_error` | `upstream_rate_limited` · `upstream_quota_reached` | Hedef servis kısıtlıyor: en az 30-60 sn bekle |
| `502` | `upstream_error` | `upstream_failure` | Geçici: üstel geri çekilmeyle (1-2-4 sn) en fazla 3 deneme |
| `503` | `upstream_error` | `upstream_unavailable` | Devre kesici açık: 30-60 sn sonra tekrar dene |
| `504` | `upstream_error` | `upstream_timeout` | Hedef yanıt vermedi: bir kez daha dene, süreyi uzat |

Kurallar:

1. **`401`'de asla otomatik tekrar deneme.** Anahtar iptal edilmiş olabilir.
2. **`429`'da `Retry-After`'a uy.** Başlık yoksa 60 sn bekle.
3. **`400`'i yeniden denemekle düzeltmeye çalışma.** Gövdeyi düzelt.
4. İstemci tarafında **zaman aşımı ≥ 120 sn** olsun: hedef servis ilk token'ı
   15+ saniyede verebiliyor (ölçüldü). Kısa zaman aşımı "çalışmıyor" gibi görünür.

---

## 4. Hız sınırları

Anahtar **başına** token-bucket:

| Ayar | Varsayılan |
|---|---|
| Dakikada istek (`RATE_LIMIT_RPM`) | **120** |
| Anlık patlama (`RATE_LIMIT_BURST`) | **30** |

Sınır anahtar başına tutulur: başka bir projenin yoğunluğu senin kotanı
**yemez**. Ama hedef servis tek bir oturum üzerinden çalıştığı için toplam
kapasite ortaktır — dakikada yüzlerce istek planlıyorsan önce işleticiyle konuş.

Sağlık kontrolü (anahtar gerektirmez):

```bash
curl https://<servis-adin>.onrender.com/health
```

---

## 5. İşletici tarafı: anahtar vermek ve kullanımı okumak

> Bu bölüm servisi işleten kişi içindir. Tüketicinin bunları bilmesi gerekmez.

### ⚠️ Önce bunu yap: `ADMIN_API_KEYS` ayır

`ADMIN_API_KEYS` **boşsa her `API_KEYS` anahtarı admin sayılır** (geriye dönük
uyumluluk). Yani tüketiciye verdiğin anahtar çerezi değiştirebilir, tüm
oturumları silebilir ve başka anahtar üretebilir. Anahtar dağıtmadan önce:

```env
ADMIN_API_KEYS=sk-senin-yonetici-anahtarin   # yalnız sende kalır
API_KEYS=sk-senin-yonetici-anahtarin         # tüketici anahtarları buraya eklenir
```

Ayrım yapıldıysa `GET /v1/admin/keys` yanıtında `admin_separation: true` ve
tüketici satırlarında `is_admin: false` görürsün. Yapılmadıysa **her satır
`is_admin: true` döner** (gerçek yetki budur) ve her üretimde log'a
`project_key_minted_without_admin_separation` uyarısı düşer.

**Anahtar üret** (redeploy gerekmez, değer yalnız bu yanıta bir kez yazılır):

```powershell
$B = "https://<servis-adin>.onrender.com"
$A = "ADMIN_ANAHTARIN"
Invoke-RestMethod -Method Post -Uri "$B/v1/admin/keys" `
  -Headers @{ Authorization = "Bearer $A" } -ContentType "application/json" `
  -Body (@{ label = "proje adı" } | ConvertTo-Json)
```

**Kullanımı oku** (proje başına istek / token / hata):

```powershell
Invoke-RestMethod -Uri "$B/v1/admin/keys" -Headers @{ Authorization = "Bearer $A" }
```

**İptal et** (`key_id` bir önceki yanıttan):

```powershell
Invoke-RestMethod -Method Delete -Uri "$B/v1/admin/keys/cli_abc123def456" `
  -Headers @{ Authorization = "Bearer $A" }
```

Davranış notları:

* `POST` yanıtındaki `api_key` değeri **bir kez** gösterilir; kaydetmezsen yeni
  anahtar üretmen gerekir. Log'larda ve listelemede yalnız maskeli hâli
  (`sk-***GI`) ve parmak izi (`cli_<sha256[:12]>`) görünür — anahtar değeri
  hiçbir yüzeye yazılmaz.
* İptal **anında** etkilidir (doğrulama ayarları canlı okur) ve kalıcıdır:
  liste hem durum dosyasına hem Render env'ine (`API_KEYS`) yazılır.
* **Son anahtar iptal edilemez** — wrapper her tüketiciyi reddederdi.
* Sayaçlar **bellek içidir**: restart'ta sıfırlanır (Free Render'da kalıcı disk
  yok). Kalıcı muhasebe istiyorsan `/metrics` çıktısını dışarıda topla:
  `apiwrapper_project_requests_total{project="…"}`,
  `apiwrapper_project_errors_total`, `apiwrapper_project_tokens_total`.
* Üretim/iptal **hem durum dosyasına hem Render env'ine** (`API_KEYS`,
  `API_KEY_LABELS`) yazar; ikisi de `render.yaml`'da `sync: false`, yani
  blueprint bu değerleri yönetmez ve deploy onları eskiye döndürmez.
  Anahtar listesini Render panosundan **elle** değiştirirsen değişiklik ancak
  restart'ta okunur; çalışan süreç kendi listesini yazmaya devam eder. Üretim ve
  iptal için daima API'yi kullan.
* Bu katman **yalnız muhasebe** yapar: model izni, günlük bütçe ve kuyruk yok.
  Koruma, anahtar başına hız sınırıdır (§4).

---

## 6. Bilinen sınırlar (dürüst liste)

1. **Tek upstream oturumu.** Hedef siteye bir kişinin çereziyle gidiliyor. İki
   proje de aynı kapasiteyi paylaşır; çerez ölürse ikisi de durur
   (bkz. [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) — çerezi öldüren şey
   tarayıcıdan giriş yapmaktır).
2. **İlk token yavaş olabilir** (15+ sn ölçüldü). Bu hedef servisin davranışı,
   bir hata değil.
3. **`429` her zaman bizim değil.** Hedef servis kendi kotasını uyguluyor;
   `Retry-After` yoksa bekle ve tekrar dene.
4. **Görsel üretimi desteklenmiyor** (vitrinden indirildi, hedef `429` veriyor).
5. **Sayaçlar kalıcı değil** (§5). Fatura/raporlama gerekiyorsa `/metrics`'i
   dışarıda sakla.

---

## 7. Otomatik sistemler için: `/v1/ask` (PR #56 — henüz canlıda değil)

Projen bir **alarm / otomasyon sistemi** ise — yani bir metin gönderip cevabını
alacak, alamazsa yedeğine geçecekse — OpenAI uçları yerine `/v1/ask` kullan:

```
POST /v1/ask   {"prompt": "…"}
→ data: {"status":"wait"}            (30 sn sonra, sonra 15 sn'de bir)
→ data: {"status":"done","text":"…"} (cevabın tamamı, tek parça)
→ data: {"status":"problem"}         (cevap alınamadı: yedeğe geç)
```

Hata kodu yorumlamak, tekrar deneme kurmak, zaman aşımı hesaplamak gerekmez.
Sözleşmenin tamamı, karşı sisteme verilecek hazır talimat ve örnek istemci:
**[`ASK_API.md`](ASK_API.md)**.

## 8. İstek sırası (PR #56 ile gelir — henüz canlıda değil)

O sürümden itibaren **bütün** tamamlama istekleri (bu rehberdeki OpenAI uçları
dahil) tek sıradan geçer:

* Aynı anda **bir** istek işlenir; diğerleri geliş sırasıyla bekler.
* Ardışık iki istek arasına rastgele **5–10 sn** girer.
* Sırada en çok 20 istek bekler; fazlası hemen şu cevabı alır:

| Durum | `type` | `code` | Ne yapmalısın |
|---|---|---|---|
| `429` | `rate_limit_error` | `queue_full` | `Retry-After` (30 sn) kadar bekle, sonra tekrar dene |

Pratik sonuçlar:

1. **İstemci zaman aşımını uzun tut.** Önünde istek varsa cevabın, kendi işlem
   süresine ek olarak öndekilerin süresi kadar gecikir. §3'teki "≥ 120 sn"
   önerisi tek istek içindir; paralel istek gönderiyorsan daha uzun tut.
2. **Paralel istek hız kazandırmaz.** On isteği aynı anda göndermek ile sırayla
   göndermek aynı sürede biter.
3. **Bağlantıyı kapatırsan:** sırada bekleyen isteğin sıradan çıkar (upstream'e
   hiç gitmez). **Akışlı** istek işleniyorsa durdurulur. **Akışsız** istek
   upstream'e gönderilmişse durdurulmaz: sonuna kadar sürer, cevabı atılır
   (canlı sürümdeki davranış; gönderme tarafı değişmesin diye korundu —
   `LESSONS_2026_11_14.md §20`). Yani akışsız istekte zaman aşımını kısa tutup
   yeniden denemek, öndeki isteğin bitmesini beklemekten hızlı değildir.

---

## İlgili dokümanlar

| Doküman | Ne için |
|---|---|
| [`../README.md`](../README.md) | Genel bakış, mod tabloları, sık yapılan hatalar |
| [`ENV_FULL.md`](ENV_FULL.md) | `API_KEYS`, `API_KEY_LABELS`, hız sınırı ayarları |
| [`ASK_API.md`](ASK_API.md) | `/v1/ask`: otomatik sistemler için bekle / bitti / problem ucu, istek sırası |
| [`TWO_ACCOUNTS.md`](TWO_ACCOUNTS.md) | İkinci hesap (`UPSTREAM_CURL_2`): isteklerin hesaplara bölünmesi; tüketici için değişen bir şey yok |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Hata → çözüm tablosu |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | İstek akışı ve modül yapısı |
| [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) | Ölçülmüş gerçekler (çerez ömrü, captcha, `mode`) |
