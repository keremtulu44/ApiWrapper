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

### 3.1 Eklenti destekleyen Android tarayıcı + Cookie-Editor

1. Chromium tabanlı, **masaüstü eklentisi** çalıştırabilen bir Android tarayıcı
   kurun (ör. **Mises**, **Lemur**; **Kiwi** arşivlenmiş olsa da hâlâ çalışıyor).
2. Mağazadan **Cookie-Editor** eklentisini ekleyin.
3. Sitede giriş yapın → Cookie-Editor → **Export → Header String**
   (çıktı: `ad=değer; ad2=değer2`).
4. Telegram'a: `/cookie <yapıştır>`.

Wrapper, oturum çerezi gibi görünmeyen metni reddeder (yanlışlıkla çalışan
oturumun silinmemesi için).

### 3.2 Firefox (Android) + başlık/çerez eklentisi

Firefox Android eklenti listesi kısıtlıdır; çerez/başlık dışa aktaran bir eklenti
varsa aynı yöntem işler: çerez satırını kopyala → `/cookie ...`.

### 3.3 İleri seviye: Termux + mitmproxy

Telefonda `mitmproxy` çalıştırıp trafiği izlemek teknik olarak mümkündür ama sertifika
kurulumu/Android sürüm kısıtları nedeniyle zahmetlidir. Önerilmez.

### 3.4 En pratik alternatif

Bir kereliğine masaüstü/başka bir cihazda: DevTools → Network → ilgili istek →
**Copy as cURL** → Telegram `/curl <...>`. Sonrasında sunucu tarayıcısı devralır.

## 4. Sınırlar (dürüstçe)

* Sunucu **senin yerine giriş yapamaz**: şifre/2FA/OAuth akışı senin cihazında
  olmalı. Şifreni Telegram'a/loglara yazmayın; wrapper şifre kabul etmez.
* `UPSTREAM_CURL`/çerez **bellekte** uygulanır; süreç yeniden başlarsa Render'daki
  secret değerine döner. Kalıcı olsun istiyorsan secret'ı da güncelle.
* `/cookie` ve `/curl` yalnızca yetkili Telegram sohbetinden çalışır
  (`TELEGRAM_ALLOWED_CHAT_IDS`) ve değerler hiçbir yanıtta/logda görünmez.
