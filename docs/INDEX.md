# ApiWrapper — Doküman Rehberi

**Buradan başla.** 18 doküman var; burada hangisini ne zaman okuyacağın yazıyor.

> ## 🔙 Bu ağaç bir **geri dönüş** sürümüdür (16/17 Kasım 2026)
>
> Kod **`e1e8d5a`**'ye (14 Kasım 16:42 UTC) geri sarıldı — kesintisiz **15 saat**
> çalıştığı ölçülen tek sürüm. **`docs/` bilerek eksiltilmedi**, o yüzden aşağıdaki
> dokümanların bir kısmı bu sürümde **olmayan** özellikleri anlatıyor:
>
> | Doküman | Bu sürümdeki durumu |
> |---|---|
> | `SESSION_FORENSICS.md` | ⚠️ **Tarihsel.** `/zaman`, olay defteri ve forensic retry bu sürümde **yok** |
> | `CONSUMERS.md` | ⚠️ **Kısmen tarihsel.** Proje anahtarı üretimi (`/v1/admin/keys`) ve `web`/`image` modelleri **yok**; OpenAI şeması, hata kodları ve hız sınırı geçerli |
> | `ENV_FULL.md`, `env.mobile.example` | ⚠️ Anahtarların bir kısmı bu sürümün `Settings`'inde yok (üreticileri `scripts/env_template.py` de kaldırıldı) |
> | `ROLLBACK_2026_11_16.md` | ✅ **Bu sürümün kaydı — önce bunu oku** |
> | Diğerleri | ✅ Geçerli |
>
> **Bu sürümün işletim kuralı:** kalıcılık katmanı yok (`state_store.py` bu
> sürümde mevcut değil). `UPSTREAM_CURL`, `UPSTREAM_MODE=direct-battle` ve
> `UPSTREAM_STREAM_PATH` **Render env'inde** durmalı, yoksa her restart'ta servis
> ölür ya da her istek `400 ZodError · path ["mode"]` döner. Ayrıntı:
> [`ROLLBACK_2026_11_16.md §6`](ROLLBACK_2026_11_16.md).
>
> ### ✅ Sonuç geldi: geri dönüş **çalıştı** (17 Kasım)
>
> Bu sürüm merge edildikten sonra **10 saat+ kesintisiz** çalıştı; sonraki
> commit'lerde aynı koşullar 4 saat ve 2 sa 46 dk sürmüştü. Giriş yöntemi
> confound'u da elendi (dört koşunun cURL'ü de **Google** ile alınmış).
> Aday tek dosyaya indi: `upstream/headers.py`. Kayıt:
> **[`ROLLBACK_2026_11_16.md §11`](ROLLBACK_2026_11_16.md)**.
>
> **İki yeni işletim kuralı** ([`LESSONS_2026_11_14.md §15`](LESSONS_2026_11_14.md)):
>
> 1. **cURL'ü yalnız Google ile giriş yapılmış oturumdan al.** E-posta + şifre
>    ile girilmiş oturumun cURL'ü `recaptcha_rejected` veriyor.
> 2. **`recaptcha_rejected` görürsen 10-15 dakika BEKLE.** `static` token
>    değişmez, bu sürümde retry yok ⇒ red hedef tarafında **geçici**; ölçüldü,
>    kendiliğinden geçti. **Taze cURL almaya kalkma** — tarayıcıya giriş sağlıklı
>    oturumu öldürür. Belgelerdeki eski çareler (`TROUBLESHOOTING §4`,
>    `diagnostics.py`) **çürütüldü**, öncelik `LESSONS §15`'te.
>
> Bu aynı zamanda **Chromium/keeper yolunun kesin olarak kapandığı** anlamına
> geliyor: `browser_login.py:14`'teki *"(2FA/captcha yok)"* varsayımı yanlış ve
> keeper Google akışını hiç tanımıyor (`LESSONS §15.2`).
>
> **18 Kasım eki — ep3 bitti, ep4 başladı.** ep3 **~28 saat** yaşadı ve
> kendiliğinden ölmedi; operatörün env değişikliği + redeploy'u bitirdi. ep4
> aynı kodla, probe aralığı 10 dk → **60 dk**, hedef **72 saat dokunmadan**:
> **[`ROLLBACK_2026_11_16.md §12`](ROLLBACK_2026_11_16.md)**.
>
> **Üç yeni işletim kuralı** ([`LESSONS_2026_11_14.md §16`](LESSONS_2026_11_14.md)):
>
> 1. **`429 daily spend limit reached` ve `wrapper_unreachable` (≈180 sn) ölüm
>    değildir — BEKLE.** Kota tanınmadığı için uyarı yanlış kodla geliyor.
> 2. **Env değişikliği = restart = oturum kaybı.** Düzeltmeleri biriktir.
> 3. **`/ping` hedefi göstermez** (yalnız süreç canlılığı); `ROLLBACK §11.6`'daki
>    satır düzeltildi.

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
| [`ROLLBACK_2026_11_16.md`](ROLLBACK_2026_11_16.md) | **Bu sürümün kaydı.** Neden `e1e8d5a`'ye dönüldü, ölçülen oturum ömürleri, 16 Kasım ölümünün olay defteri, tarayıcı bulgusu, kaldırılanların listesi, işletim kuralı, ileri dönüş tarifi | **Önce bu** |
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
| [`ASK_API.md`](ASK_API.md) | **`/v1/ask` (PR #56, canlıda değil)**: alarm/otomasyon sistemleri için `wait` / `done` / `problem` sözleşmesi, karşı sisteme verilecek hazır talimat, örnek istemci, tek istek sırası, Telegram mesajlarının okunuşu | Bir otomasyonu bağlarken; "karşı sistem neden `problem` alıyor" diye bakarken |
| [`CONSUMERS.md`](CONSUMERS.md) | **Tüketici rehberi**: OpenAI SDK ile bağlanma, modeller, hata kodları, hız sınırı + anahtar üretme/iptal | API'yi başka bir projeye verirken |
| [`../README.md`](../README.md) | Genel bakış + **sık yapılan hatalar** | Her zaman |

### Arıza
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Hata → çözüm tablosu | Bir şey bozulduğunda |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) § captcha | reCAPTCHA hata → çözüm | Captcha reddederse |
| [`SESSION_FORENSICS.md`](SESSION_FORENSICS.md) | **Ölçüm protokolü**: 401 bizden mi hedeften mi (H1–H4), `/zaman` olay defteri, 24 saatlik deney planı | `502 upstream_unauthorized` tekrarlıyorsa |
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

**Proje anahtarı ver (başka bir proje bu API'yi kullanacaksa):**
```powershell
Invoke-RestMethod -Method Post -Uri ($B + "/v1/admin/keys") `
  -Headers @{ "Authorization" = "Bearer ADMIN_ANAHTARIN" } `
  -ContentType "application/json" -Body (@{ label = "proje adı" } | ConvertTo-Json)
```
Kullanım: `GET /v1/admin/keys` · İptal: `DELETE /v1/admin/keys/<key_id>` ·
Rehber: [`CONSUMERS.md`](CONSUMERS.md)
