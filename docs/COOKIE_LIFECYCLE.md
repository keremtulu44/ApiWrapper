# Çerez ömrü: neyin süresi dolar, neyin dolmaz, ne sürekli alınmalı

Bu belge `/tani` ve `/yenile` çıktısındaki çerezleri tek tek açıklar. Amaç
"her seferinde çerez almak" döngüsünü kırmak: hangi değer **kendiliğinden**
döner, hangisi **yalnız oturum ölünce** elle taşınır.

## 0. Üç katman — karışan şey bu

| Katman | Örnek | Ömrü | Kim tazeler |
|---|---|---|---|
| **1. Oturum (kimlik)** | `arena-auth-prod-v1`, `.0`, `.1`, `-code-verifier` | Günler–haftalar; sunucu **her an iptal edebilir** | Yalnız **giriş yapmış tarayıcı**; wrapper bunu üretemez |
| **2. Türev değerler** | `dpl_...` sürüm damgası, `cx`, `referer` yolu | Saatler–günler (her deploy'da değişir) | **`/yenile`** otomatik yakalar |
| **3. Geçici/telemetri** | `__cf_bm`, `_dd_s`, `_ga*`, `_vcrr_*` | 15 dk – 2 yıl | Site kendi yazar; **kimlik değeri yok** |

**Sonuç:** Elle sürekli alınması gereken bir şey **yok**. Katman 1 yalnız oturum
gerçekten öldüğünde, katman 2 zaten otomatik, katman 3 tamamen ilgisiz.

## 1. /tani çıktısındaki çerezler — ne işe yarar

Kerem'in 2026-11-02 çıktısından (14 çerez):

| Çerez | Ne | Ömrü | Wrapper'a etkisi |
|---|---|---|---|
| `arena-auth-prod-v1` | Oturum çerez ailesinin anahtarı (v1 sürümü) | Oturum boyu | **Kritik.** Yoksa oturum yok |
| `arena-auth-prod-v1.0`, `.1` | Aynı oturum çerez **parçaları** (4 KB sınırı için bölünmüş; `.0` + `.1` birleşince tek değer) | Ana çerezle aynı | **Kritik.** Parçalardan biri eksikse token okunamaz |
| `arena-auth-prod-v1-code-verifier` | OAuth/PKCE giriş akışının geçici anahtarı | ~10 dk | **İlgisiz.** Giriş bitince işe yaramaz |
| `__cf_bm` | Cloudflare bot yönetimi | ~30 dk, sürekli döner | İlgisiz (kimlik değil) |
| `_dd_s` | Datadog RUM oturumu | 15 dk boşta / 4 saat | İlgisiz (telemetri) |
| `_ga`, `_ga_<ID>` | Google Analytics | ~2 yıl | İlgisiz (telemetri) |
| `_vcrr_*` | Vercel hız/izleme | Kısa | İlgisiz |
| `+6` (görünmeyen) | Genelde dil/tema/deney bayrakları | Uzun | İlgisiz |

Tek gerçek kimlik taşıyıcısı `arena-auth-prod-v1*` ailesidir. Telemetri
çerezlerini taşımak zorunlu değil; zararı da yok.

## 2. "Token yok" ne demek?

Oturum çerezi iki biçimde olabilir:

1. **Düz JWT** — içinden `exp` okunur, süre bilinir.
2. **Şifreli/kodlanmış oturum (JWE, base64 JSON, parçalı)** — wrapper değeri
   çözebilirse içindeki `access_token`'ı (JWT) ve `refresh_token`'ı çıkarır;
   çözemezse "token yok" der. Bu bir arıza değil, **okuma sınırıdır**.

`/tani` bu yüzden artık iki satır yazıyor:

```
Çerezde token: var ama süresi dolmuş (JWT)   ← çerezin içinde ne var
Kullanılan token: yok                        ← istekte ne kullanılıyor
Authorization başlığı: gitmeyecek
```

Üçünün aynı anda görülmesi şu demek: istek upstream'e **kimliksiz** gidiyor →
401 beklenir. Çözüm, oturumu taşıyan çerezin tazelenmesi.

## 3. `/yenile` neyi tazeler, neyi tazelemez

| Tazeler | Tazelemez |
|---|---|
| `dpl_...` sürüm damgası | Oturum çerezini (parola/OTP yok) |
| `cx` bayrağı | Süresi dolmuş `access_token`'ı |
| `referer` yolu | Sunucuda iptal edilmiş oturumu |
| Sayfanın yeni `Set-Cookie`'leri | 429 kotasını |

`/yenile` "oturum yakalanamadı" derse üç nedenden biri: çerez hiç yüklenmedi,
oturum adı yok, ya da token süresi dolmuş. Üçü de **tarayıcıdan çerez** ister.

## 4. Ne sıklıkta elle müdahale gerekir

| Durum | Sıklık | Yapılacak |
|---|---|---|
| Damga eskimesi (409) | Her upstream deploy'unda | `/yenile` |
| Erişim token'ı süresi (≈1 saat, JWT ise) | Saatte bir | **Refresh token varsa otomatik olmalı** (aşağıya bak) |
| Oturum iptali / çıkış (401 "User not found") | Nadir | `/cookie` ile taze çerez |
| Telemetri/Cloudflare çerezleri | Sürekli | **Hiçbir şey** |

## 4b. Gerçek biçim (2026-11-02 gözlemi)

`arena-auth-prod-v1.0` + `.1` parçalarının birleşimi:

```
base64-eyJhY2Nlc3NfdG9rZW4i...   →  {"access_token": "eyJ...", "expires_in": 3600,
                                     "expires_at": 1793630815,
                                     "refresh_token": "test-refresh-token",
                                     "user": {...}, "provider_token": "..."}
```

Yani oturum **1 saatlik** bir erişim token'ı taşıyor; süre dolunca upstream 401
döner ve `Authorization` başlığı ya hiç gitmez ya bayat gider. Önemli sonuçlar:

* Token ~**saatte bir** ölür → elle çerez taşımak sürdürülebilir değil,
* `refresh_token` **var** → otomatik yenileme yapılabilir (§5),
* Parçalardan biri eksik/bozuk kopyalanırsa base64 çözülmez ve wrapper "token
  yok" der; bu yüzden `inspect_cookie` artık `problems` listesi döndürür.

## 5. Refresh token: kalıcı çözüm (uygulandı)

Oturum çerezinde `refresh_token` varsa erişim token'ı **elle** değil, arka plan
döngüsüyle yenilenir:

| Parça | Davranış |
|---|---|
| Zamanlayıcı | Açılışta hemen, sonra 60 sn'de bir bakar; yalnız token'ın bitmesine **10 dk** kalınca istek atar |
| İstek | `POST {iss}/auth/v1/token?grant_type=refresh_token` · gövde `{"refresh_token": "..."}` |
| `apikey` | Upstream istiyorsa sitenin HTML/JS'inden **otomatik keşfedilir** (veya `UPSTREAM_REFRESH_API_KEY` / `/apikey`) |
| Başarı | Yeni `access_token` + dönen yeni `refresh_token` (rotation) **bellekte**, **oturum çerezinde**, **`AccountPool` + `UpstreamClient` cookie jar'larında** ve **süreç dışı durum deposunda (`UPSTREAM_STATE_FILE`)** güncellenir |
| Başarısızlık | Mevcut token **korunur**, tek satır Türkçe neden döner; süre ≤10 dk ise tek seferlik uyarı gönderilir |
| Reaktif 401 | Sohbet isteği 401 alırsa istemciye hata dönmeden önce anında refresh denenir ve istek tekrarlanır |
| Elle tetik | Telegram `/token` ya da `POST /v1/admin/upstream/refresh` |
| Kapalıysa | `UPSTREAM_REFRESH_ENABLED=false` → eski davranış (elle çerez) |

Ayarlar: `UPSTREAM_REFRESH_ENABLED`, `UPSTREAM_REFRESH_LEEWAY_SECONDS` (600),
`UPSTREAM_REFRESH_CHECK_INTERVAL_SECONDS` (60), `UPSTREAM_REFRESH_URL` (boşsa
çerezdeki JWT `iss` alanından türetilir), `UPSTREAM_REFRESH_API_KEY`,
`UPSTREAM_REFRESH_TOKEN`.

**Free Render kalıcılığı:** Yeni token'lar `UPSTREAM_STATE_FILE` atomik dosyasına
yazılır ancak Free Render dosya sistemi kalıcı değildir. Asıl restart/deploy
koruması `RENDER_API_KEY` + otomatik `RENDER_SERVICE_ID` ile yapılan Render env
senkronizasyonudur. API key yoksa container restart'ında eski environment/cURL
tohumuna dönülür ve dönen refresh token geçersiz kalabilir. Docker Compose'daki
named volume yalnız yerel Docker kullanımında aynı dosyayı korur.

Token yenilemesi yeni istekleri taze client/cookie jar'ına geçirir; açık SSE
akışlarının client'ı akış bitene kadar kapatılmaz. Upstream'in cevap
`Set-Cookie` başlıklarıyla döndürdüğü ana hesap kimliği de tekil bir arka plan
işçisiyle Render env'e yazılır; cevabın ilk token'ı bu yazımı beklemez. Boş
refresh/token/header alanları da anlık görüntüye dahildir ki restart'ta önceki
hesabın secret'ı yeniden dirilmesin.

## 6. Kerem'in 2026-11-02 vakası — okuma

```
Çerez: 14 ad (arena-auth-prod-v1, .0, .1, code-verifier dahil)
Token: yok          → çerezden token okunamadı
Authorization: hayır → istek kimliksiz gidiyor
/yenile → 401 + "cookie'deki access_token süresi dolmuş"
```

Çıkarım:

1. Oturum çerezi **var** (adlar doğru) ama içindeki erişim token'ı **süresi
   dolmuş**; wrapper'ın okuyabildiği değer bu.
2. Çerezde token bulunamadığı için istek **Authorization başlığı olmadan**
   gidiyor → upstream 401 veriyor. Yani sorun "çerez eksik" değil, "çerezdeki
   kimlik bayat".
3. `/yenile` bunu düzeltemez: sunucu tarayıcısı sizin oturumunuzla **giriş
   yapamaz**, yalnız mevcut çerezle sayfayı açar.
4. Tek yol: giriş yapmış tarayıcıdan **tüm** çerez seti (özellikle
   `arena-auth-prod-v1`, `.0`, `.1`) → `/cookie`. Sonrasında `/tani`'de
   `Çerezde token: var (≈N dk kaldı)` görülmeli.
5. Kalıcı çıkış: çerez setinde `refresh_token` varsa §5'teki akış.

## 7. Ölçümle doğrulanan düzeltmeler (2026-11, üç cURL)

Ayrıntı ve zaman çizelgesi: **[`docs/CAPTURE_TIMELINE.md`](CAPTURE_TIMELINE.md)**.
Bu bölüm yalnız bu belgedeki ifadeleri düzeltir.

| Bu belgede yazan | Ölçülen | Sonuç |
|---|---|---|
| Erişim token'ı "~1 saat" | cURL 1: `expires_in=3600`; cURL 2 ve 3: **`expires_in=1200`** | Ömür sunucu ayarıdır ve **kısaldı**. Kod kararı `exp` + leeway (600 s) üzerinden verdiği için davranış doğru; "1 saat" beklentisi yanlış. TTL'i asla sabit varsayma, `expires_in`/`exp` oku. |
| `_dd_s` "15 dk boşta" | `expire = lastSeen + 900 s` (her istekte kayan) | Doğrulandı. Kayan alan olduğu için donmuş çerez setinde "saatlerdir boşta" görünür; kimlik değeri değil. |
| `_vcrr_*` "Vercel hız/izleme, kısa" | Değeri **her zaman** bir önceki deploy'un `dpl_` kimliği; `x-arena-web-deployment` başlığıyla asla eşleşmiyor | Damga **çerezden türetilemez**; `deployment.py`'nin sayfayı okuması doğru tasarım. |
| `.0`/`.1` "4 KB sınırı için bölünmüş" | 3180 + 1958 karakter; bölünme noktası base64 bloğunun **ortasında** ve oturum JSON'u uzadıkça **kayıyor** | Birleştirme ayırıcısız (`""`) yapılmalı; `rewrite_session_cookie` boyları koruyarak yeniden böler. |
| `arena-auth-prod-v1` "oturumun anahtarı" | Çerez **boş**; gerçek değer yalnız `.0`+`.1` | Boş kök çerez "oturum yok" sayılmamalı (`merge_chunked_cookies` bunu zaten doğru yapıyor). |
