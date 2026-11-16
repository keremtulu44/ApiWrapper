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
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) § captcha | reCAPTCHA hata → çözüm | Captcha reddederse |
| [`CAPTURE_TIMELINE.md`](CAPTURE_TIMELINE.md) | Yakalama zaman çizelgesi | Geçmiş kararları anlamak |

### Geçmiş / arşiv
`AUDIT_2026_11.md`, `HANDOFF.md`, `START_HERE.md`, `DELIVERY_REPORT.md`,
`MERGE_TARGET.md` — tarihsel kayıtlar. Güncel doğru için önce yukarıdaki
tablolara bak.

---

## Aktif kurulum (ana dal / `main`)

```env
RECAPTCHA_PROVIDER=static        # KALICI
UPSTREAM_REFRESH_ENABLED=false   # KALICI
```

> **`main` dalında otomatik tarayıcı girişi (Chromium) YOKTUR.** Aşağıdaki
> tabloya bakın — kod nerede duruyor, hangi dalda, hangi commit'te.

---

## Chromium / otomatik tarayıcı girişi — nerede duruyor?

**Kod yazıldı, test edildi, ama `main`'de DEĞİL.** Bilerek dışarıda tutuldu:
şu an gerekmiyor ve `main`'e giren tek şeyin doküman olması istendi.

| Nerede | Ne var | Commit |
|---|---|---|
| **`main`** | ❌ Yok. `browser_login.py` bulunmaz, `UPSTREAM_LOGIN_*` env tanımlı değil | — |
| **`arena/1kasim-test`** | ✅ **1 Kasım kodu + otomatik giriş.** Deploy edilmiş, test edilmiş (393 test) | `e1e8d5a` |
| **`arena/0715c3e6-apiwrapper`** | ✅ Tam sistem: giriş + kalıcı state + `mode` düzeltmesi | `e403f1d` |

### Ne yapıyor

Sunucudaki Chromium'u **açık tutar** ve hedef siteye **e-posta + şifre** ile
girer; taze çerezi ayarlara yazar. Periyodik döngü (varsayılan 600 s) çerezi
kontrol eder, eskidiyse önce sıcak sayfayı dürtüp kendi token'ını yeniletir,
yetmezse yeniden giriş yapar.

**CAPTCHA'ya dokunmaz.** Yalnızca `upstream_cookie`, `upstream_access_token`,
`upstream_refresh_token` yazar. `recaptcha_provider` / `recaptcha_static_token`
değişmez — uçtan uca doğrulandı (9 birim testi + sahte site testi).

### Bekleme modu (o dalda deploy edilirse)

| Env | Davranış |
|---|---|
| `UPSTREAM_LOGIN_ENABLED` boş/`false` | Chromium **hiç açılmaz**. `SessionKeeper` oluşturulmaz. Sıfır RAM/CPU |
| `true` + e-posta + şifre | Chromium açılır, çerezi tazeler |
| `true` ama eksik | Açılmaz; log'a `upstream_login_not_configured` |

### ⚠️ Neden `main`'e alınmadı / dikkat

1. **Gerekmiyor.** Çerez 15+ saat yaşıyor; otomatik giriş gereksiz.
2. **Çakışma riski.** Otomatik giriş, kullanıcının kendi tarayıcı oturumuyla
   çakışabilir — iki taraf birbirinin oturumunu iptal edebilir. Çerezi öldüren
   şey tam olarak bu (yukarıdaki 1. kural).
3. **RAM.** 512 MB planda ikinci bir Chromium risklidir.

**Ne zaman alınır:** çerez sürekli ölüyorsa ve tarayıcıya girmediğin hâlde
düzelmiyorsa.

**Nasıl alınır:** ilgili daldan `app/upstream/browser_login.py`,
`app/core/config.py` (6 alan + `login_url()`), `app/main.py` (lifespan),
`app/api/v1/admin.py` (`POST /v1/admin/upstream/login`), `tests/test_browser_login.py`.

**Denemek için (Render):**

1. Render → servis → Settings → **Branch** = `arena/1kasim-test` → Save
2. Environment'a ekle:
   ```env
   UPSTREAM_LOGIN_ENABLED=true
   UPSTREAM_LOGIN_EMAIL=<hedef site e-postası>
   UPSTREAM_LOGIN_PASSWORD=<şifre>
   ```
3. Deploy sonrası log'da bekle: `upstream_login_keeper_started` →
   `upstream_login_refresh ok=true`
4. Başarısızsa `notes` alanında sebep yazar
   (`e-posta alanı bulunamadı` = giriş formu değişmiş)

**Geri dönmek için:** Branch'i tekrar eski değere al + `UPSTREAM_LOGIN_ENABLED=false`.

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
