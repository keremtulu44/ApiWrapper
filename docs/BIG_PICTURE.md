# Büyük resim: neden sürekli cURL/çerez yeniliyorduk, şimdi nasıl çalışıyor

> Tek sayfalık zihinsel model. Yeni bir oturuma başlarken önce burayı oku.
> Ayrıntılar: [`COOKIE_LIFECYCLE.md`](COOKIE_LIFECYCLE.md),
> [`MOBILE_CURL.md`](MOBILE_CURL.md), [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md),
> [`MERGE_TARGET.md`](MERGE_TARGET.md).

## 1. Sorun

Upstream oturumu, çerezin içinde **≈1 saatlik** bir `access_token` taşıyor
(`expires_in: 3600`). Token ölünce her istek `401` dönüyor. Eski yaklaşım
"ölünce elle tazele" olduğu için sürekli cURL/çerez taşınıyordu — yani belirti
tedavi ediliyordu.

## 2. Çerezleri üç katmana ayır

| Katman | Ne | Ömür | Kim tazeler |
|---|---|---|---|
| **1. Kimlik** | `arena-auth-prod-v1.0/.1` içinde `access_token` + `refresh_token` | access **1 saat**, refresh **haftalar** | **Wrapper** (otomatik) |
| **2. Türev** | `dpl_…` sürüm damgası, `cx`, `referer` | upstream her deploy'unda | **`/yenile`** |
| **3. Gürültü** | `__cf_bm`, `_dd_s`, `_ga*`, `ph_*`, `_vcrr`, `user_country_code`, `arena_visit_id` | dakikalar–yıllar | **Kimse** (kimlik değil) |

Sürekli değişenlerin çoğu katman 3'tür; onları taşımak boşunadır.

## 3. Yeni mantık: "ölünce tazele" değil, "ölmesine izin verme"

1. **Token'ı wrapper kendi yeniler.** `app/services/token_refresh.py` 60 sn'de bir
   bakar; bitmesine 10 dk kalınca `app/upstream/refresh.py` ile
   `grant_type=refresh_token` isteği atar, yeni token'ı belleğe **ve** oturum
   çerezine yazar (parçalı çerez aynı sınırlarla yeniden bölünür).
2. **Türevleri `/yenile` tazeler.** Sunucudaki Chromium sayfayı açar, yeni
   `dpl_…`/`cx`/`referer` değerlerini yakalar; 409 sınıfı sorunlar böyle çözülür.
3. **Kimlik yalnız gerçekten öldüğünde elle girer.** Oturum iptal edildiyse ya da
   süreç yeniden başlayıp eski refresh token geçersiz kaldıysa.
4. **Teşhis tahmin yerine:** `/tani` "neyim eksik" der (çerezde token var mı,
   süresi ne, refresh token var mı, otomatik yenileme açık mı).

## 4. Hedef son durum

```
Çerez bir kez girilir
        ↓  wrapper her saat kendi token'ını yeniler   (otomatik)
        ↓  upstream deploy'unda /yenile türevleri tazeler  (tek komut)
Elle müdahale: yalnız oturum iptal edilirse (haftalar)
```

**cURL'ün kaldığı tek yer:** kimliği sıfırdan kurmak gerektiğinde ve
Cookie-Editor yoksa. `create-evaluation` isteği **şart değil**; `cookie` başlığını
taşıyan herhangi bir istek yeterli.

## 5. Komut haritası (telefon)

| Komut | Ne yapar | Ne sıklıkta |
|---|---|---|
| `/ping` | Servis + son upstream kontrolü | istendiğinde |
| `/tani` | Kimlik teşhisi + tek satır "ne yapmalı" | bir şey bozulunca |
| `/token` | Token'ı **şimdi** yenile (otomatik olanı elle tetikler) | test için |
| `/yenile` | Türev değerleri tazele (409 çözümü) | upstream deploy'unda |
| `/cookie` | Kimlik ver · parçalı yapıştırma: `/cookie <parça>` … `/cookie bitir`, `/cookie iptal`, `/cookie!` zorla | haftalar |
| `/curl` | Kimlik ver (PC'de *Copy as cURL*) | nadiren |

## 6. Dürüst sınırlar

- Wrapper **giriş yapamaz** (parola/OTP yolu yok); ölü oturumu diriltemez.
- `/yenile` yalnız türevleri tazeler, oturumu diriltmez.
- **429 kota** kimlik sorunu değildir; çerezle çözülmez.
- Refresh token her kullanımda **döner**; token'lar bellekte olduğundan
  **restart/deploy sonrası** Render'daki eski çerez geçersiz kalabilir → bir kez
  taze çerez gerekir.
- Upstream `apikey` isterse tek ayar yeterli: `UPSTREAM_REFRESH_API_KEY`.

## 7. Kalan yol haritası

1. **Refresh token'ı süreç dışında sakla** (Render secret güncellemesi ya da
   küçük bir KV) → restart sonrası bile kendini toparlar. *En değerli adım.*
2. `apikey`'i otomatik keşfet (sitenin HTML/JS'inden anon anahtar) → ayar gerekmez.
3. `/yenile`: sayfa daha iyi bir token ürettiyse onu da uygula
   (`apply_capture_to_settings` şu an yalnız `logged_in` ise çerezi yazar).
4. Token bitmesine 10 dk kala **tek** uyarı (spam değil).

## 8. Komut/akış özeti (bir cümle)

**Kimlik bir kez, türev otomatik, token kendiliğinden:** elle müdahale yalnız
"oturum gerçekten öldü" durumunda gerekir.
