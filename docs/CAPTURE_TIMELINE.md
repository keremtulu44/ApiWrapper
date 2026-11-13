# Yakalama zaman çizelgesi — üç cURL'ün ölçülmüş farkı

Bu belge **tahmin değil ölçüm** kaydıdır: aynı hesaptan alınmış üç cURL
karşılaştırıldı ve hangi değerin hangi süreçte değiştiği zaman sırasına göre
çıkarıldı. Amaç "her seferinde çerez al" döngüsünü kırmak: hangi değer **istek
başına**, hangisi **sayfa yüklemesi başına**, hangisi **giriş başına**
döner.

> Sır kuralı: bu dosyada token, çerez değeri, e-posta **yok**. Yalnız adlar,
> zaman damgaları, süreler ve kısaltılmış kimlikler var.
>
> Uyarı: `base64-` ön eki **gizleme değildir**. Oturum çerezi tek satırda
> base64'e çevrilmiş JSON'dur; çözüldüğünde `access_token`, `refresh_token`,
> `provider_token` ve Google'ın **offline** (`1//…`) refresh token'ı düz
> metindir. Yakalama dosyası bu yüzden secret gibi saklanır, paylaşılmaz.

> **Alan adı notu:** yakalamada ana alan adı `arena.ai` olarak **maskelenmişti**;
> gerçek hedef host farklı. Bu belgedeki ölçümlerin hiçbiri host'a bağlı değil —
> okunan her şey çerez/başlık/gövde içinden geldi (çerez ve başlık adları hedef
> yığından geliyor: `arena-auth-prod-v1*`, `x-arena-web-deployment`). Host'a bağlı
> olan **tek** şey `TARGET_DOMAIN` üzerinden üretilen `origin`, `referer` ve
> çerez alanıdır (`app/core/config.py:415-440`, `app/upstream/headers.py`).
> Maskeli adı yapılandırmaya yazmak, kimlik doğru olsa bile 401/403 üretir.

## 0. Ölçüm yöntemi

Zaman damgaları tahmin edilmedi; her yakalamanın kendi içinden okundu:

| Kaynak | Nasıl okunur |
|---|---|
| Gövdedeki `id`, `userMessageId`, `modelAMessageId` | **UUIDv7** — ilk 48 bit milisaniye zaman damgasıdır |
| `arena-auth-prod-v1.0` + `.1` | `base64-` ön eki atılır, JSON çözülür → `expires_in`, `expires_at`, `refresh_token` |
| Aynı JSON'un `access_token` alanı | JWT payload'ı → `iat`, `exp`, `session_id`, `amr[].timestamp` |
| `_dd_s` | `created`, `expire` (milisaniye) |
| `arena_visit_id` | `started`, `lastSeen` (milisaniye) |
| `__cf_bm` | değerin sonundaki `-<ts>-1.0.1.1-` alanı |
| `_ga` / `_ga_<ID>` | `s` = oturum başlangıcı, `t` = son isabet, `j` = isabet sayısı |

## 1. Üç yakalamanın çıpası

| | cURL 1 | cURL 2 | cURL 3 |
|---|---|---|---|
| İsteğin gerçek zamanı (gövde `id`) | 2026-11-11 14:17:49.430Z | 2026-11-12 17:45:30.118Z | 2026-11-12 17:46:12.416Z |
| Öncekinden fark | — | **+27 sa 27 dk 41 s** | **+42.3 s** |
| Giriş zamanı (`last_sign_in_at`) | 2026-11-11 14:17:39 | 2026-11-12 17:45:19 | (aynı) |
| İstek, girişten kaç saniye sonra | 10 s | 11 s | 53 s |

Üç yakalama da **girişten hemen sonra** alınmış. Bu yüzden token'ların
"taze" hâli görülüyor; süresi dolmuş hâli değil.

## 2. Katman katman: ne değişti, ne değişmedi

### 2.1 İstek başına değişenler (her POST'ta yeni)

| Değer | cURL 1 | cURL 2 | cURL 3 | Not |
|---|---|---|---|---|
| gövde `id` / `userMessageId` / `modelAMessageId` | 3 ayrı UUIDv7 | 3 yeni UUIDv7 | 3 yeni UUIDv7 | Sıra korunur: `id` → `userMessageId` → `modelAMessageId` (aynı ms) |
| `traceparent` | `…a4a5a151a5c5e173-5dbc8eba516d5329` | `…7f7e5a2017b9c64b-775bf30613588fea` | `…7e9e7903c801e9ce-5fd35f19a57a3081` | Her istekte tamamen yeni |
| `recaptchaV3Token` | `0cAFcWeA7OhK…` | `0cAFcWeA65Pg…` | `0cAFcWeA75A0…` | **Ön ek sabit** (`0cAFcWeA`), gövde her istekte farklı |
| gövde `cx` | `Auki-MxrLpnYqjUms7vE` | `Alr7BUop3IrB8owmHBV1` | `Alr7B0op3QqhsowknBV1` | 42 s arayla **değişti** → oturum değil, istek/istemci türevi. 2 ile 3 aynı sayfa yüklemesinden, ilk 3 baytı (`02 5a fb`) paylaşıyor; 16 baytlık opak gövde |

> `cx` için önemli sonuç: yakalanan değer **yeniden kullanılabilir sabit değil**.
> Bir kez alınan `cx` ile saatler sonra istek atmak, ilk isteğin baytlarını
> tekrar göndermek demektir.

### 2.2 Sayfa yüklemesi / oturum ziyareti başına değişenler

| Değer | cURL 1 | cURL 2 | cURL 3 | Ömür |
|---|---|---|---|---|
| `__cf_bm` zaman damgası | `1794406646.798` | `1794505500.571` | **aynı** | ~30 dk (site kendi yazar) |
| `cf_clearance` | `CuTWSF…` | `CjD2Z2…` | **aynı** | Cloudflare'in verdiği ömür; IP + UA'ya bağlı |
| `arena_visit_id.id` | `01a1caf8…` | `01a1d0dc…` | **aynı** | Ziyaret başına |
| ↳ `started` / `lastSeen` | 14:17:26.897 / 14:17:57.017 | 17:45:00.890 / 17:45:37.946 | 17:45:00.890 / **17:46:22.649** | `lastSeen` **kayan** |
| `_dd_s.id` | `8b8f82ba…` | `d8265c7b…` | **aynı** | RUM oturumu |
| ↳ `created` / `expire` | 14:16:26.876 / 14:32:56.922 | 17:44:59.988 / 17:59:57.284 | 17:44:59.988 / **18:00:41.961** | `expire` = son etkinlik + **900 s** (kayan) |
| `_ga_L5C4D55WJJ` | `s1794406647 t1794406677 j30` | `s1794505501 t1794505537 j24` | `s1794505501 t1794505582 j51` | `s` oturum, `t` kayan, `j` isabet sayısı |
| PostHog `$device_id` | `01a1caf8…bc40` | `01a1d0dd…4edd` | **aynı** | **Değişti** → tarayıcı deposu sıfırlanmış |

`_dd_s` ve `arena_visit_id`'nin `expire`/`lastSeen` alanı **kayan pencere**:
her istek değeri ileri taşır. Donmuş bir çerez setinde bu iki alan "bu oturum
saatlerdir boşta" der.

### 2.3 Giriş (sign-in) başına değişenler

| Değer | cURL 1 | cURL 2 ve 3 |
|---|---|---|
| `access_token` `iat` / `exp` | 14:17:39 / 15:17:39 | 17:45:19 / 18:05:19 |
| **`expires_in` / gerçek TTL** | **3600 s** | **1200 s** |
| İstek anında kalan ömür | 3589 s | 1188 s / 1146 s |
| `refresh_token` | `xuza2f…` | `zhnxk…` (döndü) |
| JWT `session_id` | `ac18d748…` | `9cf9b409…` |
| `amr[0].timestamp` | `iat` ile aynı | `iat` ile aynı |

**En kritik bulgu:** erişim token'ının ömrü iki giriş arasında **3600 s → 1200 s**
oldu. Kod ve belgeler "~1 saat" varsayıyordu. Wrapper'ın yenileme kararı `exp`
alanına baktığı için (`needs_refresh`, leeway 600 s) bu değişiklik kırılma
üretmez; ama "1 saat" yazan her yorum artık yanlış ve teşhisi yanlış yöne
çekiyor: 20 dakikalık token'la 40 dakika bekleyen bir işletim, 401'i "oturum
öldü" sanar.

### 2.4 Deployment (sürüm) başına değişenler

| Değer | cURL 1 | cURL 2 | cURL 3 |
|---|---|---|---|
| `x-arena-web-deployment` **başlığı** | `dpl_GnSeJvBQwtBGZdgqb9q3watbeLQA` | `dpl_E5rHNBKTvpszHmuHHhJEkzUqdNJt` | aynı |
| `_vcrr_<hash>` **çerezi** | `dpl_scbQMQFHWgVxxmipjyFJCcaj8xcq\|0.7219` | `dpl_GomHkYLr5JFzVagyxcwM51guF3uM\|0.1532` | aynı |

**İki değer hiçbir zaman eşit değil.** `_vcrr_` bir önceki ziyaretin
deployment'ını taşıyor; güncel damga sayfanın kendisinden (yanıt başlığı veya
HTML) okunuyor. Yani:

* Damga **çerezden türetilemez**. `app/upstream/deployment.py` bu yüzden
  sayfayı okuyor — tasarım doğru.
* Ama okunan değerin **yazılabilmesi** gerekir; bkz. §4.1.

### 2.5 Hiç değişmeyenler

| Değer | Açıklama |
|---|---|
| `arena_acq_source` | İlk temas damgası (`at` 1793885141106, `path` `/text/direct`); bir daha yazılmaz |
| `user_country_code=TR`, `arena_build_bucket=stable` | Bölge/kanal bayrağı |
| `_ga` istemci kimliği `1965015662`, `_ga` ilk damga `1793627177` | Analitik istemci kimliği |
| `_dd_s.aid` | Datadog uygulama kimliği (site geneli) |
| PostHog `distinct_id` | Kullanıcı kimliği (girişe bağlı, oturuma değil) |
| `modelAId` `019b24bb-5caf-71c3-b854-37d0c7086f21` | Model kaydı; deploy'lar arası sabit |
| `accept-language`, `user-agent`, `sec-ch-ua*`, `origin`, `referer` | Tarayıcı parmak izi |

### 2.6 Oturum çerezinin biçimi (üçünde de aynı)

* `arena-auth-prod-v1` **boş**, `arena-auth-prod-v1-code-verifier` **boş**.
  Gerçek oturum yalnız `.0` + `.1` parçalarında: **3180 + 1958 = 5138 karakter**.
* Parça sınırı **base64 bloğunun ortasına** denk geliyor; birleştirme
  `""` ile (ayırıcısız) yapılmalı. cURL 1'de sınır `…Imh0d` / `HM6Ly9…`,
  cURL 2'de `…Imh0d` / `HBzOi8v…` — **sınır yeri oturum JSON'unun uzunluğuna
  göre kayıyor**, sabit değil.
* Üç yakalamada da `_dd_s` değeri `&amp;` ile kaçışlı geldi (`aid=…&amp;rum=2…`).
  Kaçışın kendi `;` karakteri, ayrıştırıcıyı böler; bkz. §4.2.

## 3. Özet tablo — hangi değer ne zaman tazelenir

| Süreç | Tazelenmesi gereken | Tetikleyici |
|---|---|---|
| Her istek | `id`, `userMessageId`, `modelAMessageId`, `traceparent`, `cx`, `recaptchaV3Token` | İstek üretimi |
| Her sayfa yüklemesi | `x-arena-web-deployment`, `cf_clearance`, `__cf_bm` | Sayfa/kanarya okuması |
| Her etkinlikte | `_dd_s.expire`, `arena_visit_id.lastSeen`, `_ga.t/j` | Site kendi yazar; wrapper'ın işi değil |
| Her giriş | `.0`/`.1` oturum çerezi, `refresh_token`, `session_id` | Yalnız tarayıcıdan giriş veya `refresh_token` |
| Her deploy'da | `dpl_...` damgası | Kanarya (`/yenile`) |

## 4. Bu ölçümün ortaya çıkardığı yazılım hataları

### 4.1 Damga başlığı ada duyarlı değiştiriliyordu (düzeltildi)

`build_stream_headers` taze damgayı yalnız başlık adı **birebir küçük harf**
ise yazıyordu (`if name in headers`). cURL aktarımı adları küçük harfe indirdiği
için (`curl_import.py:93`) bu yol çalışıyordu; ama `UPSTREAM_EXTRA_HEADERS` elle
`X-Arena-Web-Deployment=...` yazıldığında kanarya değeri **okuyup yazamıyordu**:
istek donmuş damgayla gidiyor, upstream `409 refresh_required` dönüyor ve
operatör çareyi CAPTCHA token'ında arıyordu.

* Düzeltme: `app/upstream/headers.py` — `build_stream_headers`, `DEPLOYMENT_HEADERS`
  eşleşmesini `name.lower()` ile yapar; anahtarın yazımı korunur.
* Test: `tests/test_deployment_canary.py::test_canary_replaces_stamp_configured_with_mixed_case_header_name`

### 4.2 `&amp;` kaçışı çerez paketini bölüyordu (düzeltildi)

`parse_cookie_header` metni `;` ile bölüyordu. `&amp;` **kendi içinde `;`
taşıdığı** için tek telemetri çerezi altı sahte çereze ayrılıyordu
(`_dd_s`, `rum`, `id`, `created`, `expire`, `logs`) ve `_dd_s` değeri
`aid=…&amp` olarak kesiliyordu. Aynı bozulma upstream'e giden başlıkta da
oluşuyordu.

* Düzeltme: `app/upstream/auth.py` — `normalize_cookie_header()` + kaçış çözümü
  bölmeden **önce**; `curl_import.py` çerezi saklamadan önce normalize eder.
* Testler: `tests/test_auth_token.py::test_parse_cookie_header_unescapes_html_entities_before_splitting`,
  `tests/test_curl_to_env.py::test_escaped_ampersand_in_cookie_is_normalized`

### 4.3 Belge/kod yorumlarındaki "1 saat" varsayımı (düzeltildi: belge)

Ölçüm 1200 s gösteriyor. Karar `exp` alanına dayandığı için davranış doğru;
ama teşhis metinleri yanlış beklenti üretiyordu. `docs/COOKIE_LIFECYCLE.md`
güncellendi.

## 5. Bu belgenin kapsamadığı şey (bilerek)

`recaptchaV3Token` alanı **istek başına tek ve farklı**. Yani token'ı üretip
100 saniye önbellekte tutup birden çok istekte kullanmak, ilk istekten sonra
reddedilmek demektir. Wrapper'ın mevcut tasarımı (`recaptcha_token_ttl`,
`SingleFlightValue`) tam olarak bunu yapıyor.

Bu **teşhis** burada kayıtlı; **düzeltme bilerek yapılmadı**. Sunucu tarafında
tarayıcı otomasyonuyla token üretmek, hedef servisin bot korumasını (reCAPTCHA
+ Cloudflare) atlatmaya yönelik bir çalışmadır; bu depo üzerinde o katmanı
güçlendirmek bu oturumun kapsamı dışında bırakıldı. Sürdürülebilir yollar:

1. **İnsan tarayıcıda kalsın:** isteği kullanıcının kendi oturumu/sekmesi
   üzerinden gönderen bir akış (eklenti/userscript) — token'ı sayfa üretir.
2. **Resmî erişim:** hedef servisin API/partner kanalı varsa onu kullanmak.
3. **Kapsamı daraltmak:** wrapper'ı yalnız kendi oturumunun ömrü boyunca,
   düşük hacimle, gözlem amaçlı çalıştırmak.

Geri kalan bütün katmanlar (kimlik, damga, çerez biçimi, hata sınıflandırma)
bu belgedeki ölçümlerle uyumlu ve düzeltilmiş durumda.
