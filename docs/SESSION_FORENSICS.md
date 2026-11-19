# Oturum ölçüm sistemi — "neyin ne zaman olduğu"

**Bu dosya bir deney protokolüdür.** Amaç: `502 upstream_unauthorized` geldiğinde
"kimin yüzünden" sorusunu tahminle değil **ölçümle** cevaplamak.

> ## ⚠️ SÜRÜM NOTU (19 Kasım 2026) — bu ölçüm sistemi bu sürümde YOK
>
> Olay defteri (`app/services/session_timeline.py`), `GET /v1/admin/timeline`,
> Telegram `/zaman` ve forensic retry (`UPSTREAM_FORENSIC_RETRY`) 16/17 Kasım
> geri dönüşüyle kalktı. Aşağıdaki protokol **uygulanamaz**; kayıt olarak ve
> ileri dönüş (`ROLLBACK_2026_11_16.md §10`, `§11.7`) için duruyor.
>
> **Sistemin ürettiği son veri** (16 Kasım): `LESSONS_2026_11_14.md §13` ve
> `ROLLBACK §2`. Otomatik hüküm **`H2 DESTEKLENDİ`** idi (oturum hedef tarafında
> düştü: 401 anında çerezde taze token yoktu). Süresi geçmiş token'la başarılı
> istek **görülmedi** (en yaşlı çalışan token 1185 sn < ömür 1200 sn) ve 401'ler
> donmuş token'ın `exp` anında başladı; ölümün imzası "rotasyon durur, 401
> ~20 dakika sonra gelir".
>
> ⚠️ **Etiket uyarısı.** `ROLLBACK §2` ve `LESSONS §13` bu sonucu *"H1
> çürütüldü"* diye yazar. Bu dosyanın tanımına göre (§1 tablosu ve §4 karar
> tablosu) `H1 ÇÜRÜTÜLDÜ` **tersini** söyler: "hedef yaşı denetlemiyor". Ölçümün
> söylediği o değildir. Geçerli olan kelimelerle yazılan sonuçtur: *"hedef token
> yaşını denetlemiyor" kaydının dayanağı çürüdü* — yani aşağıdaki karar
> tablosunun diliyle sonuç `H1 ÇÜRÜTÜLDÜ` **değildir** (`LESSONS §19.4/24`).
>
> **Bu sürümde `401` nasıl teşhis edilir:** `upstream_http_error … body=` log
> satırı + bir sonraki probe'un sonucu. Kural: süren 401 oturumun ölümüdür; tek
> 401'de önce doğrula (`LESSONS §19.2`). Geçerli işletim kuralları: [`LESSONS_2026_11_14.md §19.1`](LESSONS_2026_11_14.md).

Bugüne kadar her 401'de aynı tartışma baştan yapılıyordu, çünkü loglar yalnızca
sonucu söylüyordu:

```
access_token_expired  expired_seconds_ago=82
upstream_http_error   status=401  body={"message":"User not found"}
request_failed        status_code=502
```

Bu üç satırdan şunların **hiçbiri** çıkmıyordu: token kaç kez rotasyona girdi,
rotasyon ne zaman durdu, istek hangi kimlikle gitti, süresi dolmuş görünen bir
token'la başarılı istek oldu mu, 401 anında çerezde taze token var mıydı.

---

## 1. Dört hipotez ve hangi ölçümün hangisini ayırt ettiği

| # | Hipotez | İmza (olay defterinde) | Kesinleştiren ölçüm |
|---|---|---|---|
| **H1** | Hedef artık token **yaşını** denetliyor | `identity_rejected` hep `exp` anından hemen sonra | **Çürüten:** `upstream_ok` içinde `age_s > ttl_s` ya da `max_exp_overrun_s > 0` — süresi geçmiş token'la istek geçtiyse hedef yaşı denetlemiyordur |
| **H2** | Oturum **hedef tarafında** düştü | `forensic_retry outcome=rejected` veya `skipped_no_fresh_token`; rotasyon durmuş | Taze çerez token'ı da 401 verdiyse ya da çerezde taze token hiç yoksa oturum ölmüştür. Tek çözüm: yeni cURL |
| **H3** | Sabitlenmiş `UPSTREAM_ACCESS_TOKEN` taze çerezi **gölgeliyor** | `pinned_token_shadowing` + `forensic_retry outcome=recovered` | 401 sonrası çerezdeki taze token'la yapılan deneme **geçti** ⇒ suçlu bizim env değerimiz. Çözüm: anahtarı Render env'den **sil** (boşaltma) |
| **H4** | Aynı çerezi kullanan **ikinci bir süreç/deploy** | `token_rotated` sık ama `identity_used` hep aynı; `state_saved` bizde görünmüyor | Doğrudan ölçülemez (süreç kendi dışını göremez). Dolaylı kanıt: rotasyon var ama istekler bayat token'la gidiyor ve biz hiç cURL/çerez uygulamadık |

### H2'nin iki alt sebebi

| Sebep | Neden olur | Not |
|---|---|---|
| **H2a** Tarayıcıdan aynı hesaba giriş | Platform yeni oturumu tek geçerli sayar, sunucudaki çerez iptal olur | `INDEX.md` kural 1. Ölçümde H2 imzası verir — defter "tarayıcıya girdin" diyemez, ama "oturum sunucu tarafında öldü" der |
| **H2b** Platform kendiliğinden düşürdü | Güvenlik olayı, parola değişimi, uzun hareketsizlik | Aynı imza. Ayırt etmek için tek yol: sen hiçbir şey yapmadan ölmesi |

> **Önemli:** H2a ile H2b aynı ölçüm imzasını üretir. İkisini ayıran şey
> ölçüm değil, senin ne yaptığındır. Bu yüzden protokol Faz 2'de "hiç dokunma"
> kuralı var.

---

## 2. Kaydedilen olaylar

Olay defteri bellekte sınırlı bir halkadır (`SESSION_TIMELINE_SIZE`, varsayılan
400 olay). Kritik toplamlar halkadan bağımsız birikir, yani halka dolsa bile
"en yaşlı çalışan token" kaybolmaz.

| Olay | Ne zaman yazılır | Neyi kanıtlar |
|---|---|---|
| `process_started` | Süreç açılışı | Restart sınırı: ölçüm bundan sonrasını anlatır |
| `identity_used` | Her upstream isteğinde; **yalnızca kimlik değişince** ya da 15 dk'da bir | `source=pinned` mi `cookie` mi — hangi kimlikle gidiyoruz |
| `token_rotated` | Upstream çerezinde **farklı** bir erişim token'ı görülünce | Rotasyon sürüyor mu, `exp`/`ttl` ne, aralık kaç saniye |
| `pinned_token_shadowing` | Rotasyon oldu ama env'deki sabit token farklı ve yerinde kaldı | **H3'ün imzası.** Canlı arızada bu satır hiç yoktu, çünkü ölçülmüyordu |
| `token_unchanged` | Çerez döndü ama aynı token çıktı (5 dk'da bir seyreltilir) | Rotasyon durmuş — H2'nin erken işareti |
| `upstream_ok` | Başarılı yanıt (60 sn'de bir seyreltilir), token yaşıyla | **H1'i çürüten ölçüm:** yaşı `ttl`'i aşmış token çalışıyorsa hedef yaşı denetlemiyor |
| `identity_rejected` | 401/403 kimlik reddi; `body_class` ile sınıflı | `user_not_found` = ölüm, `auth_body` = diğer ret, `captcha` = captcha |
| `forensic_retry` | Ölüm anındaki tek doğrulama denemesinin sonucu | `recovered` → H3 · `rejected` → H2 · `skipped_no_fresh_token` → H2 · `skipped_disabled` → ölçüm kapalı |
| `state_saved` | Durum dosyasına her yazım (`source` ile) | cURL/çerez yenileme sınırları: "ölüm yenilemeden kaç saniye sonra oldu" |

**Gizlilik:** hiçbir olay ham token içermez. Yalnızca parmak izi (`#a1b2c3d4e5`),
`iat`/`exp` damgaları ve uzunluk kaydedilir. Bunu test doğrular
(`tests/test_session_timeline.py::test_report_and_events_never_contain_a_raw_token`).

---

## 3. Nereden okunur

| Yüzey | Ne verir |
|---|---|
| Telegram **`/zaman`** (diğer adlar: `/olaylar`, `/olaydefteri`, `/timeline`) | Telefon ekranına sığan Türkçe rapor + otomatik yorum |
| `GET /v1/admin/timeline?limit=50&report_events=10` | Tam JSON: `analysis` (toplamlar + `verdict`), `events` (olay listesi), `report` (metin) |
| `GET /v1/admin/auth` → `identity_forensics` | `/tani` çıktısının içinde tek ekranlık özet: kaynak, rotasyon sayısı, ölüm sayısı, en yaşlı çalışan token, verdict |
| Log olayları | `upstream_identity_forensic_retry`, `upstream_identity_forensic_result`, `upstream_identity_forensic_skipped` |
| Metrik | `apiwrapper_upstream_identity_forensic_total{outcome=…}` |

Rapor saatleri **UTC + yerel** birlikte yazılır (`SESSION_TIMELINE_UTC_OFFSET_HOURS`,
Türkiye için `3`). Sebep: Telegram bildirimlerinin damgası UTC'dir
(`alerting._stamp`) ve "15:48'te geldi" denen mesaj logda 12:48 olarak bulunur.

---

## 4. Test protokolü

### Faz 0 — Hazırlık (5 dk)

1. **`UPSTREAM_ACCESS_TOKEN` anahtarını Render env'den SİL.** Boşaltmak yetmez:
   canlı kod boş değeri PUT ediyor ve Render 400 dönüyor, anahtar silinmiyor.
   (PR #54'ten sonra boş değer DELETE'e çevriliyor.) Silmezsen H3 ölçümü
   çalışır ama arıza tekrarlar.
2. **Sunucu çerezin tek sahibi ol:** hedef sitede tarayıcı oturumunu kapat.
3. **Taze cURL:** düz bir sohbet isteği → `create-evaluation` → Copy as cURL →
   Telegram `/curl <yapıştır>`. Redeploy gerekmez.
4. **Doğrula:** `/tani` → `used_for_authorization: true`, "Erişim token'ı geçerli";
   `/ping` → ✓; `/zaman` → `identity_used source=pinned|cookie` görünmeli.

### Faz 1 — Temel çizgi (24 saat, dokunma yok)

Bu fazın tek kuralı: **hedef siteye tarayıcıdan girme, hiçbir ayar değiştirme.**
Amaç sağlıklı durumu ölçmek; karşılaştırma çizgisi olmadan ölüm anı yorumlanamaz.

Günde 2-3 kez `/zaman` çek ve şu üç sayıyı not al:

| Ölçü | Sağlıklı değer | Ne anlama gelir |
|---|---|---|
| `Token: N rotasyon` | Saatte ≥1 artıyor | Çerez rotasyonu sürüyor |
| `En yaşlı ÇALIŞAN token` | `ttl`'in çok üstünde (örn. 15+ saat) | **H1 çürütüldü:** hedef yaşı denetlemiyor |
| `Ölüm: 0` | 0 | Oturum ayakta |

> `UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=0` ise (varsayılan) ölçüm yalnızca
> **gerçek istek** geldiğinde ilerler. Temel çizgi istiyorsan keepalive'i aç
> (örn. `10800` = 3 saatte bir): maliyeti kota/captcha harcamasıdır, karar senin.

### Faz 2 — Ölümü yakala

401 geldiğinde (Telegram 🔴 bildirimi ya da `/ping` ⚠) **hemen** `/zaman` çek ve
çıktıyı kaydet. Sonra hiçbir şey yapma — cURL tazelemek ölçümü bozar.

Okuma sırası:

1. `YORUM:` satırı — otomatik hüküm (H1/H2/H3).
2. `Forensic retry:` — deneme yapıldı mı, sonucu ne.
3. `Son rotasyondan bu yana: N sn` — rotasyon ölmeden ne kadar önce durdu.
4. `Son kullanılan kimlik: pinned|cookie` — istek hangi kimlikle gitti.

### Faz 3 — Karar tablosu

| Verdict | Eylem |
|---|---|
| `H3 DOĞRULANDI` | `UPSTREAM_ACCESS_TOKEN`'ı Render env'den **sil**. Sorun çözüldü; cURL tazelemeye gerek yok |
| `H2 DOĞRULANDI` / `H2 DESTEKLENDİ` | Tarayıcıdan taze cURL → `/curl`. `/yenile` **diriltmez** (`UPSTREAM_REFRESH_ENABLED=false` kalıcı karar) |
| `H1 ÇÜRÜTÜLDÜ` | Kayıt doğru: hedef yaşı denetlemiyor. `docs/LESSONS_2026_11_14.md §2`'yi bu ölçümle güncelle |
| `H1 OLASI` | Veri yetersiz. `UPSTREAM_FORENSIC_RETRY=true` kalsın ve bir ölüm bekle |
| `Yetersiz veri` | Süreç yeni açılmış ya da hiç istek gitmemiş. Keepalive'i aç |

İki ölüm üst üste **H2a** (sen bir şey yaptın) değilse ve sen hiçbir şey
yapmadıysan ⇒ **H2b**: platform oturumu kendisi düşürüyor. O zaman asıl çözüm
token yenilemeyi açmaktır (`POST /v1/admin/upstream/apikey` + `UPSTREAM_REFRESH_ENABLED=true`),
ki bu "refresh kapalı kalsın" kararını değiştirmek demektir — ölçüm olmadan
değiştirme.

---

## 5. Bu sistemi kuran olay (15-16 Kasım vakası)

Canlı logdan iki satır:

```
12:48:33 UTC  access_token_expired  expired_seconds_ago=82
13:47:59 UTC  access_token_expired  expired_seconds_ago=3648
```

Aralarındaki fark **3566 sn**, iki olay arasındaki gerçek süre **59.4 dakika**.
Yani iki istekte de **aynı token** kullanıldı ve bir saat boyunca yenilenmedi —
üstelik arada `upstream_state_saved source=upstream_set_cookie has_cookie=true`
satırı vardı, yani çerez dönmüştü.

Bu tablo H3'e işaret ediyor (sabitlenmiş token taze çerezi gölgeliyor), ama o
gün **kanıtlanamadı**: defter tutulmuyordu. Artık aynı durumda şu üç satır
kendiliğinden oluşur:

```
pinned_token_shadowing  pinned_id=2c50cfe65b  pinned_expired=True  fresh_id=41c8002152
identity_used           source=pinned  age_s=3600.5  expired=True
forensic_retry          outcome=recovered  first_status=401  status=200
```

ve `/zaman` tek cümleyle söyler: *"H3 DOĞRULANDI … Render env'den bu anahtarı SİL."*

---

## 6. Ölçümün sınırları (dürüstlük bölümü)

* **Bellek içi.** Restart'ta sıfırlanır; Free Render uykuya dalınca ölçüm durur.
  Kalıcı tarihçe istiyorsan dışarıda topla (`/metrics` Prometheus'a akar, olay
  defteri akmaz).
* **`exp`/`iat` yoksa yaş ölçülemez.** JWT olmayan (opak) token'da `age_s` ve
  `ttl_s` boş kalır; verdict "veri yetersiz" der, uydurmaz.
* **Forensic retry yalnızca `user not found` sınıfında** ve istek başına **bir**
  kez çalışır. 403 captcha, 409 damga ve 429 kota bu yolu izlemez.
* **Deneme bir ek istektir.** Ölüm başına 1 fazla upstream isteği; kota/captcha
  tüketir. Kapatmak için `UPSTREAM_FORENSIC_RETRY=false`.
* **H4'ü doğrudan göremez.** İkinci bir süreç aynı çerezi kullanıyorsa bu süreç
  yalnızca kendi gördüğünü ölçer.
* **Ölüm anını yakalamak istek gelmesine bağlıdır.** Keepalive kapalıysa arıza
  ancak bir tüketici isteği geldiğinde ölçülür; "ne zaman öldü" cevabı o isteğin
  zamanı olur, gerçek ölüm anı değil.

---

## 7. İlgili dosyalar

| Dosya | Rol |
|---|---|
| `app/services/session_timeline.py` | Olay defteri, toplamlar, verdict, rapor |
| `app/upstream/client.py` | Kanca noktaları: `_forensic_identity_retry`, rotasyon/başarı/ret kayıtları |
| `app/api/v1/admin.py` | `GET /v1/admin/timeline`, `/auth` içinde `identity_forensics` |
| `app/telegram/bot.py` | `/zaman` komutu |
| `tests/test_session_timeline.py` | Birim testler: toplamlar, seyreltme, verdict'ler, gizlilik |
| `tests/test_identity_forensics.py` | Uçtan uca: H2/H3 ayrımı, gölgeleme, tek deneme hakkı |
| `docs/LESSONS_2026_11_14.md §2` | Token ömrü tablosu — H1 ölçümü bu kaydı doğrular ya da çürütür |
| `docs/TROUBLESHOOTING.md §1b, §2` | 401 arıza akışı; bu dosya onun ölçüm katmanıdır |
