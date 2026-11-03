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

1. **Token'ı wrapper kendi yeniler ve aktif istemcilere anında yayar.**
   `app/services/token_refresh.py` 60 sn'de bir bakar; bitmesine 10 dk kalınca
   `app/upstream/refresh.py` ile `grant_type=refresh_token` isteği atar, yeni
   token'ı belleğe, parçalı oturum çerezine, `AccountPool` yuvalarına **ve**
   `UpstreamClient` cookie jar'larına yazar.
2. **İstek anında (reaktif) 401 kurtarma.** Arka plan döngüsü çalışmadan önce
   bir istek 401 alırsa, `CompletionService` hata dönmek yerine anında refresh
   token ile yeni token alıp aynı isteği kesintisiz tekrarlar.
3. **`apikey` otomatik keşfedilir.** Upstream yenileme ucu Supabase `apikey`
   (anon anahtarı) isterse, sitenin HTML/JS'inden `"role": "anon"` JWT'si veya
   tarayıcı yakalamasındaki `apikey` başlığı otomatik bulunur (`/apikey` ile
   elle de tetiklenebilir).
4. **Süreç dışı kalıcılık (`app/upstream/state_store.py`).** Dönen refresh
   token ve türev değerler yerel durum dosyasına (`UPSTREAM_STATE_FILE`) atomik
   yazılır ve opsiyonel olarak Render servis env değişkenlerine
   (`RENDER_API_KEY` + `RENDER_SERVICE_ID`) senkronize edilir. Restart sonrası
   `.env` değişmemişse dönmüş taze oturum geri yüklenir; `.env`'e daha yeni bir
   cURL konduysa yeni cURL tercih edilir.
5. **Türevleri kanarya ve `/yenile` tazeler.** Sürüm damgası (`dpl_…`)
   sayfadan otomatik okunur; `/yenile` ayrıca daha taze bir token üretildiyse
   (`logged_in` tam tetiklenmese bile) onu da uygular.
6. **Teşhis ve tek seferlik uyarı:** `/tani` "neyim eksik" der; token bitmesine
   ≤10 dk kalıp otomatik yenilenemediyse aynı token için **yalnızca bir kez**
   Telegram uyarısı gönderilir (spam yok).

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
| `/apikey` | Yenileme `apikey` değerini siteden otomatik keşfet veya `/apikey <anahtar>` ile ayarla | gerekirse |
| `/yenile` | Türev değerleri tazele (409 çözümü) | upstream deploy'unda |
| `/cookie` | Kimlik ver · parçalı yapıştırma: `/cookie <parça>` … `/cookie bitir`, `/cookie iptal`, `/cookie!` zorla | haftalar |
| `/curl` | Kimlik ver (PC'de *Copy as cURL*) | nadiren |

## 6. Dürüst sınırlar

- Wrapper **giriş yapamaz** (parola/OTP yolu yok); ölü oturumu diriltemez.
- `/yenile` yalnız türevleri tazeler, oturumu diriltmez.
- **429 kota** kimlik sorunu değildir; çerezle çözülmez.
- Refresh token her kullanımda **döner**. Free Render'da kalıcı disk olmadığı için
  `RENDER_API_KEY` ile env senkronizasyonu yapılandırılmalıdır; aksi hâlde eski
  token restart/deploy'da kaybolabilir ve `/cookie` ile tazeleme gerekir.
- Upstream `apikey` isterse tek ayar yeterli: `UPSTREAM_REFRESH_API_KEY`.

## 7. Tamamlanan yol haritası (otomatik sürdürülebilirlik)

1. ✅ **Refresh token süreç dışında saklanıyor (`app/upstream/state_store.py`):**
   `UPSTREAM_STATE_FILE` (yerel atomik dosya) ve Free Render'da `RENDER_API_KEY` +
   otomatik `RENDER_SERVICE_ID` (Render env senkronizasyonu) ile restart sonrası dönmüş
   refresh token korunur; kullanıcı `.env`'e daha yeni bir cURL koyarsa yeni
   tohum tercih edilir.
2. ✅ **`apikey` otomatik keşfediliyor (`discover_refresh_api_key`):**
   Upstream `apikey` istediğinde sitenin HTML ve Next.js script'lerinden public
   anon anahtar otomatik çekilir; `/yenile` sırasında ağ isteklerinden de
   yakalanır.
3. ✅ **`/yenile` daha taze token'ı da uyguluyor (`_has_fresher_token`):**
   `apply_capture_to_settings` yalnız `logged_in` değil, yakalanan çerezde daha
   ileri `exp` süreli bir `access_token` veya yeni `refresh_token` varsa onu da
   uygular.
4. ✅ **Token bitmesine 10 dk kala tek uyarı (`maybe_warn_expiring_token`):**
   Otomatik yenileme yapılamazsa ve süre ≤10 dk'ya inerse token başına yalnızca
   bir kez uyarı gönderilir.
5. ✅ **Canlı istemci senkronizasyonu ve reaktif 401 kurtarma:**
   Token yenilendiğinde `AccountPool` ve `UpstreamClient` cookie jar'ları anında
   tazelenir; istek sırasında 401 alınırsa hata dönmeden önce anında refresh
   denenip istek tekrarlanır.

## 8. Komut/akış özeti (bir cümle)

**Kimlik bir kez, türev otomatik, token kendiliğinden:** elle müdahale yalnız
"oturum gerçekten öldü" durumunda gerekir.
