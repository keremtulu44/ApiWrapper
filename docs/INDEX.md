# ApiWrapper — Doküman Rehberi

**Buradan başla.** 16 doküman var; burada hangisini ne zaman okuyacağın yazıyor.

---

## 60 saniyede sistem

```
İstemci ──(OpenAI şeması)──▶ ApiWrapper ──(tarayıcı taklidi)──▶ Hedef Web LLM
        ◀──(SSE stream)──                ◀──(AI SDK stream)──
```

Hedefin **erişim token'ı ~20 dakikada ölür** gibi bilinen bir yanlış var; doğrusu
`docs/LESSONS_2026_11_14.md`. Kısaca: **çerez 15+ saat yaşar, onu öldüren şey
tarayıcıya giriş yapmaktır.**

---

## ⚠️ Önce bunu oku (yeni oturum açan herkes)

| # | Kural | Neden |
|---|---|---|
| 1 | **Sunucu çerezin tek sahibi olsun.** Aynı hesapla tarayıcıda oturum açma. | Tarayıcıda giriş yapınca platform yeni oturumu tek geçerli sayar, sunucudaki çerez iptal olur → `401 User not found` |
| 2 | `401` görünce **hemen cURL arama.** Tarayıcıya girmeden bekle. | Kod 401'de çerezi silmez, devre kesiciyi açmaz; kısa düşmeler kendi kendine döner |
| 3 | `RECAPTCHA_PROVIDER=static` **kalıcı.** | Sunucudan üretilen token (`browser`) reddediliyor — `403 recaptcha validation failed`. Ölçüldü |
| 4 | "Bildirim aldım" ≠ "oturum öldü" | Bildirim tek başına çerezi bozmaz. Bozan şey **giriş yapmak** |
| 5 | `UPSTREAM_REFRESH_ENABLED=false` **kalıcı.** | Yenileme ucu Supabase `apikey` istiyor; elde edilemiyor |
| 6 | Docker'da `--with-deps` kullanma | `libatomic.so.1` eksikliği → build `exit 127` |

Tam liste: [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) ve
[`../README.md#sık-yapılan-hatalar-bunları-tekrar-yapma`](../README.md#sık-yapılan-hatalar-bunları-tekrar-yapma).

---

## Hangi dokümanı ne zaman okuyacağım

### Sistemi anlamak
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) | **Ölçülmüş gerçekler.** Çerez/token ömrü, çakışma modeli, static vs browser | **İlk bu** |
| [`BIG_PICTURE.md`](BIG_PICTURE.md) | Büyük resim, 3 çerez katmanı | Sistemi kavramak için |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Modül yapısı, istek akışı | Koda girmeden önce |
| [`COOKIE_LIFECYCLE.md`](COOKIE_LIFECYCLE.md) | Çerez yaşam döngüsü | ⚠️ §5 (otomatik yenileme) bu kurulumda **çalışmaz** |

### Kurulum ve işletim
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`RENDER.md`](RENDER.md) | Render kurulumu, env, uyku | Deploy ederken |
| [`MOBILE_CURL.md`](MOBILE_CURL.md) | Telefondan cURL alma | Çerez tazelerken |
| [`ENV_FULL.md`](ENV_FULL.md) | Tüm env değişkenleri (otomatik üretilir) | Env ararken |
| [`../README.md`](../README.md) | Genel bakış + **sık yapılan hatalar** | Her zaman |

### Arıza
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Hata → çözüm tablosu | Bir şey bozulduğunda |
| [`CAPTCHA_TEKNIK_INCELEME.md`](CAPTCHA_TEKNIK_INCELEME.md) | reCAPTCHA Enterprise analizi | Captcha reddederse |
| [`CAPTURE_TIMELINE.md`](CAPTURE_TIMELINE.md) | Yakalama zaman çizelgesi | Geçmiş kararları anlamak |

### Geçmiş / arşiv
`AUDIT_2026_11.md`, `HANDOFF.md`, `START_HERE.md`, `DELIVERY_REPORT.md`,
`MERGE_TARGET.md`, `SAHTE_CURL_TEST_RAPORU.md` — tarihsel kayıtlar. Güncel
doğru için önce yukarıdaki tablolara bak.

---

## Aktif kurulum (14-15 Kasım 2026)

```env
RECAPTCHA_PROVIDER=static        # KALICI
UPSTREAM_LOGIN_ENABLED=false     # Chromium bekleme modunda
UPSTREAM_REFRESH_ENABLED=false   # KALICI
```

**Otomatik tarayıcı girişi** (`app/upstream/browser_login.py`) kod olarak hazır
ama **kapalı**. `UPSTREAM_LOGIN_ENABLED=false` iken Chromium hiç açılmaz —
sıfır RAM, sıfır CPU. Açmak tek env ile mümkün; ayrıntı `../README.md`
"Otomatik tarayıcı girişi" bölümünde.

**Ne zaman açılır:** çerez sürekli ölüyorsa ve tarayıcıya girmene rağmen
düzelmiyorsa. Şu an gerekmiyor.

---

## Hızlı komutlar

**Çerez tazele (deploy gerekmez):**
```powershell
$B = "https://apiwrapper" + ".onrender.com"
$body = @{ curl = '<cURL>'; allow_path_change = $false } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri ($B + "/v1/admin/upstream/curl") `
  -Headers @{ "Authorization" = "Bearer ADMIN_ANAHTARIN" } `
  -ContentType "application/json" -Body $body
```

**Teşhis:** `POST /v1/admin/auth` · **Sağlık:** `GET /healthz`
**Telegram:** `/ping`, `/tani`, `/curl <cURL>`
