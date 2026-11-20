# ApiWrapper — Doküman Rehberi

**Buradan başla.** 20 doküman var; burada hangisini ne zaman okuyacağın yazıyor.

> ## 🔙 Bu ağaç bir **geri dönüş** sürümüdür (16/17 Kasım 2026)
>
> Kod **`e1e8d5a`**'ye (14 Kasım 16:42 UTC) geri sarıldı — kesintisiz **15 saat**
> çalıştığı ölçülen tek sürüm. **`docs/` bilerek eksiltilmedi**, o yüzden aşağıdaki
> dokümanların bir kısmı bu sürümde **olmayan** özellikleri anlatıyor:
>
> | Doküman | Bu sürümdeki durumu |
> |---|---|
> | `ROLLBACK_2026_11_16.md` | ✅ **Bu sürümün kaydı — önce bunu oku** (§12 ep4, §13-§14 canlıya alma planı) |
> | `LESSONS_2026_11_14.md` | ✅ Ölçümler. Eski bölümlerdeki çürütülmüş satırlar yerinde işaretli; **§19 belge denetimi ve geçerli kurallar**; **§20 gönderme tarafı denetimi** |
> | `ASK_API.md`, `TWO_ACCOUNTS.md` | ✅ PR #56'nın belgeleri (kod **canlıda değil**) |
> | `TROUBLESHOOTING.md` | ⚠️ **Kısmen.** Başındaki sürüm notu hangi bölümün geçerli olduğunu söyler. §3 (EK), §4 (DÜZELTME), §6-§9 geçerli; §1b, §1c, §2'deki Telegram komutları ve `/v1/admin/upstream/{curl,cookie,capture,refresh,apikey}` çareleri **yok** |
> | `RENDER.md` | ⚠️ **Kısmen.** Dağıtım sırası, `/readyz` kodları, log okuma geçerli; `RENDER_API_KEY` kalıcılığı, token yenileme, `/cookie` `/yenile` `/apikey` **yok** |
> | `CONSUMERS.md` | ⚠️ **Kısmen tarihsel.** Proje anahtarı üretimi (`/v1/admin/keys`) ve `web`/`image` modelleri **yok**; OpenAI şeması, hata kodları ve hız sınırı geçerli |
> | `ARCHITECTURE.md` | ⚠️ **Kısmen.** Modül yapısı ve istek akışı geçerli; proje anahtarları, `session_capture.py`, `Kaynaklar:` **yok**; PR #56'nın sırası ve hatları anlatılmıyor |
> | `SESSION_FORENSICS.md` | ⚠️ **Tarihsel.** `/zaman`, olay defteri ve forensic retry bu sürümde **yok** |
> | `BIG_PICTURE.md`, `COOKIE_LIFECYCLE.md` | ⚠️ **Tarihsel model.** Otomatik yenileme, kalıcılık, telefon komutları **yok**; çerez katmanlarının tarifi bilgi olarak geçerli |
> | `MOBILE_CURL.md` | ⚠️ **Tarihsel.** `/yenile`, `/cookie`, `/curl` **yok**: telefondan oturum tazelenemez. Çerezi/cURL'ü telefonda nasıl elde edeceğin kısmı bilgi olarak geçerli |
> | `ENV_FULL.md`, `env.mobile.example` | ⚠️ **Bayat.** `ENV_FULL.md` üreticisi (`scripts/env_template.py`) kaldırıldı: bu sürümde olmayan adlar içerir, PR #56'nın adlarını içermez. Güncel şablon **`.env.example`** (testle denetlenir) |
> | `CAPTURE_TIMELINE.md` | ✅ Ölçüm kaydı (bir satırı `LESSONS §13`'te düzeltildi) |
> | `HANDOFF.md`, `START_HERE.md`, `AUDIT_2026_11.md`, `MERGE_TARGET.md`, `DELIVERY_REPORT.md` | 🗄️ **Arşiv.** Yazıldıkları ağacın dosya/satır haritaları; satır numaraları ve bir kısım dosya artık yok |
>
> *(19 Kasım: bu tablo eskiden dört satırdı ve geri kalanı için "Diğerleri ✅
> Geçerli" diyordu; yanlıştı. Denetimin kaydı: `LESSONS_2026_11_14.md §19`.)*
>
> **Bu sürümün işletim kuralı:** kalıcılık katmanı yok (`state_store.py` bu
> sürümde mevcut değil). `UPSTREAM_CURL` **Render env'inde** durmalı ve her
> restart'ta **taze** olmalı, yoksa servis açılır ama cevap veremez. Ayrıntı:
> [`ROLLBACK_2026_11_16.md §6`](ROLLBACK_2026_11_16.md).
>
> *(19 Kasım düzeltmesi: bu paragraf "`UPSTREAM_MODE=direct-battle` ve
> `UPSTREAM_STREAM_PATH` de env'de durmalı, yoksa her istek `400 ZodError`
> döner" diyordu. `UPSTREAM_CURL` doluyken o iki env değeri **okunmaz**; `mode`
> ve yol cURL'den gelir — denendi, `ROLLBACK §6`. Env'de duruyorlarsa
> zararsızdır; silmek için restart yapma.)*
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
> 2. **`recaptcha_rejected` görürsen BEKLE** (ölçülen süre 10-15 dakika;
>    **2 saate kadar** bekle). `static` token değişmez, bu sürümde retry yok ⇒
>    red hedef tarafında **geçici**; ölçüldü, kendiliğinden geçti. **Taze cURL
>    almaya kalkma** — tarayıcıya giriş sağlıklı oturumu öldürür. Belgelerdeki
>    eski çareler (`TROUBLESHOOTING §4`, `diagnostics.py`) **çürütüldü**, öncelik
>    `LESSONS §15`'te (`diagnostics.py` metinleri PR #56'da düzeltildi; canlı
>    kodda eski hâliyle duruyor).
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
>
> **19 Kasım eki — karar ve belge denetimi.**
>
> 1. **Canlıya alma sırası (operatör kararı):** PR #56 önce **tek hesapla**
>    canlıya alınır ve **12 saat** denenir; sorunsuz geçerse ikinci hesap
>    (`UPSTREAM_CURL_2`) ayrı bir restart'la eklenir. 12 saat dolmadan oturum
>    ölürse ikinci hesap eklenmez; neden aranır, yine tek hesapla denenir:
>    **[`ROLLBACK_2026_11_16.md §14`](ROLLBACK_2026_11_16.md)**.
> 2. **cURL'ü aldıktan sonra sekmeyi kapat, ÇIKIŞ YAPMA.** Belgeler "çıkış yap"
>    diyordu; operatörün uzun koşulardaki uygulaması bunun tersidir.
> 3. **Süren `401` oturumun ölümüdür; tek 401'de önce doğrula.** İki zıt cümle
>    ("genelde kendi döner" / "kendiliğinden düzelmez") tek kurala indirildi.
> 4. **Şablonlar düzeltildi:** `.env.example` sürüm damgası ayarlarını uygulamanın
>    okumadığı adlarla (`UPSTREAM_DEPLOYMENT_…`) veriyordu; `.env.example` ve
>    `render.yaml` `RECAPTCHA_PROVIDER=browser` taşıyordu.
>
> Bulguların tamamı, hangi tarafın neden doğru olduğu ve **geçerli kuralların
> tek tablosu**: **[`LESSONS_2026_11_14.md §19`](LESSONS_2026_11_14.md)**.
>
> **19 Kasım (2) — gönderme tarafı denetimi (operatör kuralı: ana cURL ve
> gönderme tarafı değişmez, `mode` dahil).** Canlı commit (`03aca2c`) ile PR #56
> aynı girdilerle iki ağaçta çalıştırıldı: upstream'e giden isteklerin **içeriği
> bayt bayt aynı** (102 istek; başlık ve gövde sırası, `mode`, çerezler dahil).
> Davranışta bulunan üç fark canlıdaki hâline döndürüldü (kota işareti
> `spend limit reached`, akışsız istekte kopma, iki hesapta ana hesabın probe
> cümlesi) ve sözleşme testle kilitlendi. PR #56'nın bilerek getirdiği tek fark
> **zamanlamadır** (sıra + 5–10 sn aralık, trafik varken probe erteleme):
> **[`LESSONS_2026_11_14.md §20`](LESSONS_2026_11_14.md)**.
>
> **19 Kasım (3) — üç operatör kararı (PR #56; canlıda henüz değil).**
> (a) **İmajın içi sabitlendi:** taban imaj özetle (`python:3.12-slim@sha256:…`
> = Python 3.12.16, OpenSSL 3.5.7), Python paketleri `requirements.lock`'taki
> tam sürümlerle kurulur; derlemenin son adımı imajın içini denetler ve
> uyuşmazlıkta derlemeyi durdurur; CI imajı derleyip bütün testleri (798) **içinde** koşar
> ([`LESSONS_2026_11_14.md §20.6.1`](LESSONS_2026_11_14.md)).
> (b) **`render.yaml` planı `free`** (canlı servisin gerçek planı; Render'ın Free
> plan sınırları [`RENDER.md`](RENDER.md) başında).
> (c) **`429 daily spend limit reached` koruması açılacak** — kodla değil,
> operatörün deploy sırasında gireceği `UPSTREAM_LIMIT_MARKERS` ile; satır ve
> doğrulaması [`ROLLBACK_2026_11_16.md §13.3`](ROLLBACK_2026_11_16.md).

---

## 60 saniyede sistem

```
İstemci ──(OpenAI şeması)──▶ ApiWrapper ──(tarayıcı taklidi)──▶ Hedef Web LLM
        ◀──(SSE stream)──                ◀──(AI SDK stream)──
```

Hedefin **erişim token'ı ~20 dakikada ölür** gibi bilinen bir yanlış var; doğrusu
`docs/LESSONS_2026_11_14.md`. Kısaca: token ~20 dakikada bir **yenilenir** (hedef,
biz istek attıkça yeniler); **oturum 15 saat+ ve ~28 saat yaşadı, onu öldürdüğü
bilinen şeyler tarayıcıya giriş yapmak ve restart'tır.**

---

## ⚠️ Önce bunu oku (yeni oturum açan herkes)

| # | Kural | Neden |
|---|---|---|
| 1 | **Sunucu çerezin tek sahibi olsun.** Aynı hesapla tarayıcıda oturum açma. cURL'ü aldıktan sonra **sekmeyi kapat, çıkış yapma.** | Tarayıcıda giriş yapınca platform yeni oturumu tek geçerli sayar, sunucudaki çerez iptal olur → `401 User not found`. Sunucu tarayıcının oturumunun aynısını taşır; çıkış da onu bitirebilir |
| 2 | `401` görünce **hemen tarayıcıya girme.** Önce doğrula: bir sonraki probe / bota düz mesaj. Hâlâ 401 ise taze cURL + yeniden deploy. | Kod 401'de çerezi silmez, devre kesiciyi açmaz; kısa bir 401 dönemi bir kez kendiliğinden geçti (14-15 Kasım). **Süren** 401 oturumun ölümüdür ve dönmez (16 Kasım) |
| 3 | `RECAPTCHA_PROVIDER=static` **kalıcı.** | Sunucudan üretilen token (`browser`) reddediliyor — `403 recaptcha validation failed`. Ölçüldü |
| 4 | "Bildirim aldım" ≠ "oturum öldü" | `429`, `recaptcha_rejected` ve ≈180 sn süren probe uyarısı **ölüm değildir**: 2 saate kadar bekle, dokunma. Ölüm yalnız süren 401'dir |
| 5 | **Env değişikliği = restart = oturum ölümü.** Canlı dala merge de bir restart'tır. | Çerez yalnız bellekte; açılışta env'deki (bayat) cURL'e dönülür. 18 Kasım'da ~28 saatlik oturumu bitirdi |
| 6 | cURL'ü **yalnız Google ile** giriş yapılmış oturumdan al. | E-posta + şifre oturumunun cURL'ü `recaptcha_rejected` veriyor (17 Kasım) |
| 7 | Docker'da `--with-deps` kullanma | `libatomic.so.1` eksikliği → build `exit 127` |

*(19 Kasım: 1., 2. ve 4. satır düzeltildi, 5. ve 6. satır eklendi. Eski 5. satır
"`UPSTREAM_REFRESH_ENABLED=false` kalıcı" diyordu; o ayar bu sürümde **yok**,
otomatik yenileme kodu geri dönüşle kalktı.)*

Tam liste: [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) ve
[`../README.md#sık-yapılan-hatalar-bunları-tekrar-yapma`](../README.md#sık-yapılan-hatalar-bunları-tekrar-yapma).

---

## Hangi dokümanı ne zaman okuyacağım

### Sistemi anlamak
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`ROLLBACK_2026_11_16.md`](ROLLBACK_2026_11_16.md) | **Bu sürümün kaydı.** Neden `e1e8d5a`'ye dönüldü, ölçülen oturum ömürleri, 16 Kasım ölümünün olay defteri, tarayıcı bulgusu, kaldırılanların listesi, işletim kuralı, ileri dönüş tarifi | **Önce bu** |
| [`LESSONS_2026_11_14.md`](LESSONS_2026_11_14.md) | **Ölçülmüş gerçekler.** Çerez/token ömrü, çakışma modeli, static vs browser; §15-§18 bu sürümün ölçümleri; **§19 geçerli kurallar ve belge denetimi**; **§20 gönderme tarafı canlıyla aynı mı (ölçüm + test)** | **İlk bu** |
| [`BIG_PICTURE.md`](BIG_PICTURE.md) | Büyük resim, 3 çerez katmanı | ⚠️ Tarihsel model: "kendiliğinden yenilenir" kısmı bu sürümde **yok** |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Modül yapısı, istek akışı | Koda girmeden önce (⚠️ proje anahtarları yok; sıra/hatlar anlatılmıyor) |
| [`COOKIE_LIFECYCLE.md`](COOKIE_LIFECYCLE.md) | Çerez yaşam döngüsü | ⚠️ §5 (otomatik yenileme) bu kurulumda **çalışmaz** |

### Kurulum ve işletim
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`RENDER.md`](RENDER.md) | Render kurulumu, env, uyku | Deploy ederken (⚠️ kalıcılık/`RENDER_API_KEY` kısımları bu sürümde **yok**) |
| [`MOBILE_CURL.md`](MOBILE_CURL.md) | Telefondan cURL alma | ⚠️ Tarihsel: telefon komutları **yok**; cURL güncellemek Render panosu ister |
| [`ENV_FULL.md`](ENV_FULL.md) | Tüm env değişkenleri (`77d4011` anında üretildi) | ⚠️ **Bayat.** Env ararken **`.env.example`**'a bak |
| [`TWO_ACCOUNTS.md`](TWO_ACCOUNTS.md) | **İkinci hesap: `UPSTREAM_CURL_2` (PR #56, canlıda değil)**: isteklerin iki hesaba yarı yarıya bölünmesi, hesap başına sıra ve probe, bir hesap arıza verince ne olduğu, Telegram mesajları, teşhis | İkinci hesabı eklerken; "bir hesap neden iş almıyor" diye bakarken |
| [`ASK_API.md`](ASK_API.md) | **`/v1/ask` (PR #56, canlıda değil)**: alarm/otomasyon sistemleri için `wait` / `done` / `problem` sözleşmesi, karşı sisteme verilecek hazır talimat, örnek istemci, tek istek sırası, Telegram mesajlarının okunuşu | Bir otomasyonu bağlarken; "karşı sistem neden `problem` alıyor" diye bakarken |
| [`CONSUMERS.md`](CONSUMERS.md) | **Tüketici rehberi**: OpenAI SDK ile bağlanma, modeller, hata kodları, hız sınırı + anahtar üretme/iptal | API'yi başka bir projeye verirken |
| [`../README.md`](../README.md) | Genel bakış + **sık yapılan hatalar** | Her zaman |

### Arıza
| Doküman | Ne anlatıyor | Ne zaman |
|---|---|---|
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Hata → çözüm tablosu | Bir şey bozulduğunda — **önce başındaki sürüm notunu oku** |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) § captcha | reCAPTCHA hata → çözüm | Captcha reddederse (**çare: bekle**; tablodaki eski çareler çürütüldü) |
| [`SESSION_FORENSICS.md`](SESSION_FORENSICS.md) | **Ölçüm protokolü**: 401 bizden mi hedeften mi (H1–H4), `/zaman` olay defteri, 24 saatlik deney planı | ⚠️ Tarihsel: ölçüm sistemi bu sürümde **yok** |
| [`CAPTURE_TIMELINE.md`](CAPTURE_TIMELINE.md) | Yakalama zaman çizelgesi | Geçmiş kararları anlamak |

### Geçmiş / arşiv
`AUDIT_2026_11.md`, `HANDOFF.md`, `START_HERE.md`, `DELIVERY_REPORT.md`,
`MERGE_TARGET.md` — tarihsel kayıtlar. Güncel doğru için önce yukarıdaki
tablolara bak.

---

## Aktif kurulum

Canlı servis **`arena/1kasim-test`** dalını izler (`main` değil; `MERGE_TARGET.md`).

```env
RECAPTCHA_PROVIDER=static        # KALICI
UPSTREAM_CURL=<tam create-evaluation cURL'ü>   # mode ve akış yolu buradan okunur
```

> **19 Kasım düzeltmesi.** Bu bölümün başlığı "Aktif kurulum (ana dal / `main`)"
> idi ve `UPSTREAM_REFRESH_ENABLED=false   # KALICI` satırını taşıyordu. Canlı dal
> `main` değildir; `UPSTREAM_REFRESH_ENABLED` ise bu sürümde **okunmayan** bir
> addır (otomatik yenileme kodu geri dönüşle kalktı) — yazmak ya da yazmamak bir
> şey değiştirmez. `UPSTREAM_MODE` de böyledir: `UPSTREAM_CURL` doluyken
> env'deki değeri okunmaz (`ROLLBACK §6`).
>
> Otomatik tarayıcı girişinin (Chromium) kodu canlı dalda **duruyor ama kapalı**;
> aşağıdaki bölüm kayıt olarak korunuyor, **uygulanmamalı**.

---

## Chromium / otomatik tarayıcı girişi — nerede duruyor?

> ## ⛔ Bu yol KAPANDI (17 Kasım ölçümü) — aşağısı kayıttır, tarif değil
>
> Aşağıdaki "Ne zaman alınır / Nasıl alınır / Denemek için" adımlarını
> **uygulama.** Gerekçe (`LESSONS_2026_11_14.md §15.1-§15.2`):
>
> * Otomatik giriş siteye **e-posta + şifre** ile girer; öyle açılan oturumun
>   istekleri `recaptcha_rejected` alıyor. Çalışan oturumlar **Google** girişiyle
>   alınıyor ve otomatik giriş Google akışını tanımıyor.
> * Sunucunun kendi girişi, env'deki cURL'ün taşıdığı çalışan oturumla
>   **çakışır** (yukarıdaki 1. kural).
>
> Bu bölüm "çerez sürekli ölüyorsa otomatik girişi aç" diye bitiyor; aynı
> belgenin başındaki not ise yolun kapandığını söylüyordu. Geçerli olan
> **kapandığıdır**. Aşağıdaki tabloda `main` için yazılanlar doğrudur; canlı dal
> `arena/1kasim-test`'tir ve kod orada durur (`UPSTREAM_LOGIN_ENABLED=false`).

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

**Bu sürümde geçerli olanlar:**

| Ne | Komut |
|---|---|
| Süreç ayakta mı | `GET /healthz` · Telegram `/ping` (ikisi de upstream'i **denemez**) |
| Upstream gerçekten cevap veriyor mu | Bota **düz bir mesaj** yaz (gerçek soru) |
| Token okunuyor mu, ne zaman doluyor | `GET /v1/admin/auth` (ikinci hesap: `?slot=2`) |
| Etkin ayarlar (gizli olmayan) | `GET /v1/admin/config` |
| Hesap/kota durumu | `GET /v1/admin/accounts` |
| Sıra, upstream durumu, hesap hatları *(PR #56)* | `GET /v1/admin/queue` |
| **Çerez/cURL tazele** | Render → Environment → `UPSTREAM_CURL` → kaydet (**yeniden deploy = restart**) |

Yönetim uçları `Authorization: Bearer <API_KEYS'ten bir anahtar>` ister.

> ### ⚠️ Aşağıdaki komutlar bu sürümde YOK (kayıt olarak duruyor)
>
> `POST /v1/admin/upstream/curl`, Telegram `/tani` ve `/curl`, `/v1/admin/keys`
> geri dönüşle kalktı. `POST /v1/admin/auth` satırı da yanlıştı: uç **`GET`**'tir.
> Çalışan servise deploy'suz cURL yüklenemez; proje anahtarı üretilemez
> (anahtar vermek = Render env'inde `API_KEYS` / `ASK_API_KEYS`).

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
