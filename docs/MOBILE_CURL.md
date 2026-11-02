# Telefondan oturum/cURL alma — ne mümkün, ne değil

Bu belge "mobildeyim, DevTools yok, nasıl taze oturum alırım" sorusunun kısa ve
dürüst cevabıdır.

## 0. Özet

| Durum | Yol | Komut |
|---|---|---|
| Sunucudaki tarayıcı oturumu yenileyebiliyorsa | **en kolay** | `/yenile` |
| Oturum tamamen düşmüş, elinde çerez satırı var | kolay | `/cookie ad=değer; ...` |
| Elinde tam cURL var (masaüstü/başka cihaz) | kolay | `/curl <cURL>` |
| Ne çerez ne cURL üretebiliyorsun | tarayıcı/araç gerekir | aşağıdaki 3. bölüm |
| Emin değilsin: çerez işe yarar mı? | hiçbir şeyi bozmaz | `?dry_run=true` |

**Altın kural:** `/yenile` ve `/capture` **asla giriş yapmaz** (parola/OTP/SMS
yolu yoktur). Oturum öldüyse tek çözüm, giriş yapmış tarayıcıdan **çerez**
getirmektir.

## 1. Neden telefondan doğrudan cURL alınamıyor?

* **Chrome (Android) / Safari (iOS):** DevTools yok; "Copy as cURL" yalnızca
  masaüstü DevTools'ta var.
* **Chrome (Android):** eklenti desteklemez, `javascript:` adresleri adres
  çubuğundan çalıştırılamaz.
* **Safari (iOS):** Web Inspector yalnızca bir Mac'e bağlanınca açılır.
* **USB uzaktan hata ayıklama:** bilgisayar gerektirir.

Yani "telefonda DevTools aç" diye bir şey yok. Bu yüzden yakalamayı servis
tarafına taşıdık.

## 2. Servis tarafı (telefonda hiçbir şey gerekmez)

### `/yenile` — sunucu tarayıcısı

Sunucudaki Chromium (reCAPTCHA için zaten çalışıyor) hedef sayfayı **elimizdeki
çerezle** açar, gerçek bir `create-evaluation` isteğini yakalar ve cookie +
`dpl_...` sürüm damgası + `cx` + referer yolunu uygular.

### `/yenile` neyi yapar, neyi **asla** yapamaz

| Yapar | Yapamaz |
|---|---|
| Elindeki çerezle sayfayı açar, sayfa açılırken giden gerçek API isteğinden sürüm damgasını, `cx`'i ve referer yolunu okur | **Senin yerine giriş yapamaz.** Sunucuda e-posta/şifre/2FA yok, olmayacak da |
| Sayfa yüklenirken upstream'in tazelediği `Set-Cookie` değerlerini jar'dan alır | Ölü bir oturumu diriltemez; çerezde oturum yoksa yakalama **hiçbir şey** yapamaz |
| Damga değiştiği için 409 alıyorsan bunu tek komutla düzeltir | Kota/429 sorununu çözmez |

**Yani:** `/yenile` "oturumu yenile" değil, "elimdeki oturumun **türevlerini**
(damga, cx, referer) tazele" demektir. Oturum çerezini tazelemek için tarayıcıda
giriş yapmış olman ve `/cookie` göndermen gerekir.

* `✓ Oturum yakalandı` → bitti, mesaj atıp test edin.
* `⚠ Oturum yakalanamadı` → mesaj artık **hangi çerezlerin geldiğini** de yazar
  (`Gelen çerezler: __cf_bm, _dd_s ...`). Üç ayrı durum:
  1. `hiç çerez yüklenemedi` → elde çerez yok/bozuk,
  2. `oturum taşıyan ad yok (gelenler: ...)` → **eksik çerez seti**: giriş yapmış
     tarayıcıdan *tüm* çerezleri aktar (`auth`, `session`, `token`, `sb-*` adlı
     çerez mutlaka gelmeli),
  3. `sayfa giriş ekranına yönlendi` → oturum gerçekten ölmüş, yeniden giriş şart.
* **Önemli:** yakalama başarısızsa çalışan çerez **değiştirilmez** (anonim çerez
  seti mevcut oturumun üzerine yazmaz). Sürüm damgası/referer yolu yine uygulanır.

API: `POST /v1/admin/upstream/capture` (gövdesiz).

## 3. Elle yakalama: cURL değil, **çerez** yeterli

Wrapper'ın ihtiyacı olan asıl şey `Cookie` başlığıdır (içinden `access_token`
ayıklanır). Bu tek satırdır; cURL'ün tamamını kopyalamak gerekmez.

### 3.1 Neden tarayıcı eklentisi gerekiyor?

Oturum çerezleri neredeyse her zaman **HttpOnly**'dir: sayfa JavaScript'i
(`document.cookie`) onları **göremez**. Bu yüzden "adres çubuğuna küçük bir kod
yazdırıp çerezleri kopyalama" yöntemi genelde çalışmaz — görünse bile oturum
çerezi eksik kalır. Eklentiler tarayıcının çerez API'sini kullandığı için
HttpOnly çerezleri de görebilir.

| Telefon | Durum |
|---|---|
| **Android + normal Chrome** | Eklenti yok → çerez dışa aktaramaz |
| **Android + eklenti destekli tarayıcı** | ✅ En pratik yol (aşağıdaki adımlar) |
| **iPhone / iPad (Safari, Chrome)** | Eklentiyle çerez dışa aktarma fiilen yok → masaüstünden bir kez `/curl`, ya da ağ paneli gösteren bir tarayıcı uygulaması |
| **Masaüstü (bir kez)** | En kolayı: DevTools → `create-evaluation` → Copy as cURL → `/curl` |

### 3.2 Android: eklenti destekli tarayıcı + Cookie-Editor (adım adım)

1. Play Store'dan **masaüstü eklentisi** çalıştırabilen bir Chromium tarayıcı
   kurun: **Mises**, **Lemur** veya **Kiwi** (arşivlenmiş olsa da çalışıyor).
2. Bu tarayıcıda siteye **giriş yapın** (önemli: çerezler bu tarayıcıda oluşur).
3. Aynı tarayıcıya **Cookie-Editor** eklentisini ekleyin (Chrome Web Store'dan).
4. Eklentiyi açın → **Export** → iki biçim de olur:
   * **Header String** → `ad=değer; ad2=değer2`
   * **JSON** → `[{"name": "...", "value": "..."}, ...]`
5. Telegram'a yapıştırın: `/cookie <çıktı>`

**Önce denemek isterseniz (hiçbir şeyi değiştirmez):**

```bash
curl -X POST "https://<servis>/v1/admin/upstream/cookie?dry_run=true" \
  -H "Authorization: Bearer <API_KEY>" -H 'content-type: application/json' \
  -d '{"cookie":"<çerez>"}'
```

Yanıt: `cookie_names`, `session_cookie_found`, `access_token_found`,
`safe_to_apply`. `safe_to_apply: false` ise o set **oturum taşımıyor** —
uygulanırsa çalışan oturum ezilebilir.

Bot tarafında koruma: `/cookie` komutu çerezi önce inceler ve oturum çerezi
yoksa **uygulamaz**, gelen adları yazar. Yine de uygulamak isterseniz:
**`/cookie! <çerez>`** (kontrolü atlar).

### 3.3 iPhone / iPad

Safari'de HttpOnly çerezleri dışa aktarmanın pratik bir yolu yok. Seçenekler:

1. **Masaüstünden bir kez** `create-evaluation` → *Copy as cURL* → `/curl <cURL>`.
   Sonrasında damga eskimesi gibi sorunlarda `/yenile` yeterli olur.
2. Ağ/Web Inspector paneli sunan bir iOS tarayıcı uygulaması (ör. **Inspect
   Browser** benzeri araçlar) ile isteğin `Cookie` başlığını kopyalamak —
   uygulamaya göre değişir, garanti değil.
3. iPhone'da **Firefox** kurup eklenti listesinde çerez dışa aktaran bir eklenti
   varsa aynı yöntem işler (liste kısıtlıdır).

### 3.4 Masaüstü: en garanti yol (tek seferlik)

1. Chrome/Firefox'ta siteye girin, bir sohbet mesajı gönderin.
2. DevTools → **Network** → `create-evaluation` isteği → sağ tık →
   **Copy → Copy as cURL**.
3. Telegram'a: `/curl <yapıştır>` (veya `POST /v1/admin/upstream/curl`).

cURL'de hem `cookie` hem `authorization` başlığı varsa ikisi de uygulanır ve
sonraki istekler tam kimlikle gider.

**Yanlış istek kopyalarsanız** (ör. ana sayfanın kendisi) wrapper çerez/token'ı
uygular ama **canlı akış yolunu korur** ve yanıtta uyarı döner; servis 404'e
düşmez.

`create-evaluation` listede yok mu? → [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
bölüm **1c**.

### 3.5 Telefonda DevTools (Kiwi / Lemur) — "istek listede yok" sorunu

Kiwi ve Lemur masaüstü DevTools'u açar; ama mobilde iki tuzak var:

1. **`Fetch/XHR` filtresi.** Sohbet akışı `fetch` ile açılan bir **SSE** akışıysa
   listede `Fetch/XHR` altında görünür; `EventSource` ise **`Other`** altında
   çıkar. → Filtre olarak **`All`** seçin, filtre kutusunu tamamen boşaltın.
2. **Panel sonradan açıldı.** DevTools açılırken sayfa yenilenir; istek
   yenilemeden **önce** atıldıysa listede olmaz. → DevTools'u açın → **Preserve
   log** işaretleyin → sayfayı yenileyin → **ilk** mesajı gönderin.

Ek kontrol listesi:

* Arama kutusuna `stream`, `evaluation` veya `nextjs` yazın (isim farklı olabilir).
* Kırmızı `ERR_BLOCKED_BY_CLIENT` → reklam/script engelleyiciyi kapatın.
* DevTools'u **masaüstü modunda** açın (Lemur/Kiwi "Desktop site").
* Hâlâ yoksa: **Cookie-Editor → Export → Header String → `/cookie <satır>`**.
  Network'e hiç bakmanız gerekmez; iki dokunuş.

### 3.6 İleri seviye: Termux + mitmproxy

Telefonda `mitmproxy` çalıştırıp trafiği izlemek teknik olarak mümkündür ama sertifika
kurulumu/Android sürüm kısıtları nedeniyle zahmetlidir. Önerilmez.

### 3.7 En pratik alternatif

Bir kereliğine masaüstü/başka bir cihazda: DevTools → Network → ilgili istek →
**Copy as cURL** → Telegram `/curl <...>`. Sonrasında sunucu tarayıcısı devralır.

## 4. Sınırlar (dürüstçe)

* Sunucu **senin yerine giriş yapamaz**: şifre/2FA/OAuth akışı senin cihazında
  olmalı. Şifreni Telegram'a/loglara yazmayın; wrapper şifre kabul etmez.
* `UPSTREAM_CURL`/çerez **bellekte** uygulanır; süreç yeniden başlarsa Render'daki
  secret değerine döner. Kalıcı olsun istiyorsan secret'ı da güncelle.
* `/cookie` ve `/curl` yalnızca yetkili Telegram sohbetinden çalışır
  (`TELEGRAM_ALLOWED_CHAT_IDS`) ve değerler hiçbir yanıtta/logda görünmez.
