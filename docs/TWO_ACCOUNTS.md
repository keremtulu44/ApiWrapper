# İki hesap: `UPSTREAM_CURL_2` — istekleri hesaplar arasında bölmek

> **Durum (20 Kasım 2026):** kod PR #56 ile canlı dala birleştirildi (`c5b5420`) ama
> ikinci hesap **KAPALI**: operatör riskli buluyor, `UPSTREAM_CURL_2` girilmedi
> (`LESSONS_2026_11_14.md §21.5`). Değişken girilmedikçe bu belgedeki hiçbir şey
> devreye girmez; tek hesap çalışır.
> Gerçek upstream'e karşı hiç çalışmadı: buradaki bütün ölçümler sahte upstream
> ile, gerçek soketler üzerinde alındı (`tests/test_two_accounts_live.py`).
> Canlıya alma: `ROLLBACK_2026_11_16.md §14`.
>
> **Canlıya alma sırası (operatör kararı, 19 Kasım):** ikinci hesap **ilk gün
> eklenmez.** Yeni kod önce **tek hesapla** canlıya alınır ve **12 saat** denenir
> (`UPSTREAM_CURL_2` boş); sorunsuz geçerse ikinci hesap ayrı bir restart'la
> eklenir. 12 saat dolmadan oturum ölürse ikinci hesap eklenmez; neden aranır
> ve yine tek hesapla denenir. Bu belgedeki her şey o ikinci restart'tan sonra
> geçerlidir; o güne dek servis §1'deki "verilmezse" hâlinde çalışır.

Bu belge üç soruyu cevaplar: ikinci hesap **nasıl eklenir**, eklenince **ne
değişir**, bir hesap arıza verince **ne olur**. Karşı sistemin (`/v1/ask`)
sözleşmesi **değişmez**; o, kaç hesap olduğunu göremez (`ASK_API.md`).

---

## 1. Tek cümleyle

`UPSTREAM_CURL_2` verilirse gelen istekler iki hesap arasında **yarı yarıya**
bölünür ve **her hesap kendi işini kendi sırasında** yapar. Verilmezse servis
bugünkü gibi tek hesapla çalışır; hiçbir şey değişmez.

Bu bir **"asıl hesap + yedek hesap" düzeni değildir.** İki hesap da sürekli
çalışır: bir hesap günde 100 istek atıyorsa artık her biri ~50 atar. Yedekte
bekleyen bir hesap, sıra ona geldiğinde büyük olasılıkla zaten ölmüş olurdu
(oturum ömrü zamana bağlı: `ROLLBACK §12`); bölüşüm ise her hesabın yükünü
baştan yarıya indirir.

---

## 2. Kurulum

### 2.1 İkinci cURL nasıl alınır

Birinci hesapla aynı yöntem (`MOBILE_CURL.md`, `LESSONS §15.1`), üç farkla:

1. **Başka bir hesap.** Aynı hesabın ikinci cURL'ü işe yaramaz; servis bunu
   fark eder ve **başlamaz** (§2.3).
2. **Ayrı tarayıcı profili** (ya da ayrı cihaz). Aynı profilde hesap değiştirmek,
   birinci hesabın o tarayıcıdaki oturumunu kapatır — ve sunucu o oturumun
   **aynısını** taşıdığı için bu, sunucudaki birinci hesabı da bitirebilir.
   cURL'ü aldıktan sonra **sekmeyi kapat, çıkış yapma**; o hesapla tarayıcıdan bir
   daha girme (`ROLLBACK §6`, 19 Kasım düzeltmesi).
3. **Aynı sayfa, aynı tür istek.** İki cURL de aynı siteden, aynı sohbet
   türünden (`create-evaluation`) alınmalı. Akış yolu, `mode` ve captcha alanının
   adı **1. cURL'den** okunur; 2. cURL'den yalnız hesaba özgü olanlar alınır:

| 2. cURL'den alınan (hesaba özgü) | 1. cURL'den alınan (ortak) |
|---|---|
| çerez / `authorization` | akış yolu |
| `user-agent`, `accept-language` | `mode`, captcha alanının adı |
| referer yolu, URL'deki alan adı | sürüm damgası başlıkları (`x-deployment-id` …) |
| gövdedeki captcha token'ı (`RECAPTCHA_PROVIDER=static` iken) | varsayılan modelin kimliği (`modelAId`) |

İki cURL farklı bir alan adı/yol/`mode` taşıyorsa servis yine başlar ama loga
`upstream_curl_slot_mismatch` yazar (hangi alanların farklı olduğunu söyler).
Alan adı farklıysa 2. hesabın istekleri **kendi** cURL'ündeki alan adına gider;
bu neredeyse her zaman yanlış isteğin kopyalandığı anlamına gelir.

### 2.2 Env

| Değişken | Zorunlu mu | |
|---|---|---|
| `UPSTREAM_CURL` | evet (bugünkü gibi) | 1. hesap |
| `UPSTREAM_CURL_2` | hayır | 2. hesap. Boşsa tek hesap. |
| `UPSTREAM_CURL_3`, `UPSTREAM_CURL_4` | hayır | Aynı kural; en çok 4 hesap |
| `UPSTREAM_ACCOUNT_NAME`, `UPSTREAM_ACCOUNT_2_NAME` | hayır | Mesajlarda görünen ad. Boşsa `hesap-1`, `hesap-2` |
| `ACCOUNT_FAILOVER` | hayır (`true`) | §5 |

Başka hiçbir ayar gerekmez. `UPSTREAM_CURL_2` blueprint'te (`render.yaml`)
tanımlı değildir; Render panelinden (Environment) elle eklenir.

**Sonekli eski alanlar.** Hesap yuvaları ayrıca tek tek alanlarla da
tanımlanabilir (`UPSTREAM_COOKIE_2`, `UPSTREAM_ACCESS_TOKEN_2` …; yerel `.env`
kurulumunda `scripts/curl_to_env.py` bunları yazar). `UPSTREAM_CURL` doluyken
— Render'daki kurulum — kendi `UPSTREAM_CURL_N`'i verilmemiş yuvaların bu
alanları açılışta **boşaltılır**: env'de unutulmuş eski bir `UPSTREAM_COOKIE_2`
ikinci hesap **oluşturmaz**. Kaç hesabın kurulduğunu açılış logu söyler:
`application_started … accounts=1` (ya da `2`).

### 2.3 Hatalı cURL: servis **başlamaz**

| Durum | Sonuç |
|---|---|
| `UPSTREAM_CURL_2` bozuk (URL yok, çerez/`authorization` yok, telemetri isteği) | Süreç açılışta durur; hata metni **hangi değişkenin** bozuk olduğunu söyler |
| `UPSTREAM_CURL_2`, `UPSTREAM_CURL` ile **aynı hesap** | Süreç açılışta durur: `UPSTREAM_CURL_2 ile UPSTREAM_CURL aynı hesaba ait` |

Sessizce tek hesaba düşmek yerine durmak bilinçli: "iki hesap kurdum" sanıp tek
hesapla çalışmak daha kötü olurdu. Render, sağlık denetimini (`/readyz`) geçmeyen
sürüme trafik vermez; eski sürüm çalışmaya devam eder ve hata deploy logunda
görünür. (Render'ın belgelenen davranışı; **bu kurulumda denenmedi**.)

Aynı hesap denetimi oturum token'ındaki hesap kimliğine (`sub`) bakar; token
sonradan yenilenmiş olsa da yakalar. Token çözülemiyorsa yalnız birebir aynı
çerez/token yakalanır.

### 2.4 ★ Her restart'ta **iki** cURL de taze olmalı

Restart, çerez kavanozlarını env'deki cURL'lerden yeniden kurar; o cURL'ler o ana
dek eskimiş olur (`LESSONS §16.6`, `ROLLBACK §12.1`: env değişikliği = restart = oturum ölümü).
İki hesapta bu **iki oturum için de** geçerlidir:

* Bir hesap ölünce onu tazelemek için deploy edersen **çalışan hesabın oturumu
  da ölür.** Deploy edeceksen iki cURL'ü birlikte tazele.
* Acele etmek zorunda değilsin: bir hesap ölünce servis öbür hesapla sürer (§5).
  İkisini birlikte, uygun bir anda tazelemek yeterli.

Arıza mesajları bunu hatırlatır (§7).

---

## 3. Bölüşüm kuralı

İstek, o an **en az işi olan** hesaba gider (işlenen + sırada bekleyen); eşitlikte
**en uzun süredir iş almamış** hesaba.

| Durum | Sonuç |
|---|---|
| İstekler tek tek geliyor | sırayla: 1, 2, 1, 2, … → gün sonunda yarı yarıya |
| Aynı anda 4 istek geldi | 2'si 1. hesaba, 2'si 2. hesaba; iki hesap **aynı anda** çalışır |
| Bir hesap uzun bir işle meşgul, yeni istek geldi | boştaki hesaba gider |
| Günde 100 istek | ~50 + ~50 |

Bölüşüm upstream'e gitmeden **önce** yapılır: istek bir hesaba atanır, o hesabın
işi olur, o hesabın sırasında bekler.

**Kaynak ayrımı yok:** `/v1/ask`, Telegram mesajları ve doğrudan API istekleri
aynı kuralla bölünür. İki istisna:

* **Keepalive probe'u** kendi hesabına sabitlidir (§6).
* **Sohbet yeniden kullanılıyorsa** (`SESSION_REUSE=true` ya da `conversation_id`)
  aynı sohbetin istekleri **aynı hesapta kalır**. Upstream sohbeti bir hesaba
  aittir; bölünseydi her hesap konuşmanın yarısını görürdü. Bunun bedeli: o
  sohbetin trafiği bölünmez. `/v1/ask` soruları her zaman bağımsızdır ve bölünür.

---

## 4. Sıra ve süreler

Tek hesaptaki kuralların hepsi **hesap başına** geçerlidir:

| Kural | Tek hesap | İki hesap |
|---|---|---|
| Aynı anda işlenen istek | 1 | hesap başına 1 → toplam 2 |
| İstekler arası rastgele 5–10 sn | önceki isteğin bitişinden | **o hesabın** önceki isteğinin bitişinden |
| Sırada bekleyebilecek istek | 20 | hesap başına 20 |
| `wait` / 180 sn / 600 sn | `ASK_API.md §4` | aynı |

Ölçüldü (gerçek soket, süreler küçültülmüş): aynı anda 4 istek, upstream 0,4 sn,
aralık 0,3 sn → her hesaba 2 istek, hesap başına aynı anda en çok **1**, toplamda
en çok **2**; her hesabın iki isteği arasında işlem süresi + aralık kadar boşluk.
Tek sırada 2,5 sn sürecek iş ~1,3 sn'de bitti.

---

## 5. Bir hesap arıza verirse (`ACCOUNT_FAILOVER=true`, varsayılan)

Kural: **arızalı hesap yeni iş almaz; işler çalışan hesaba gider; karşı sistem
bunu fark etmez. Arızalı hesap, kendi keepalive denemesi başarılı olunca geri
döner.**

| Ne oldu | O istek | Sonraki istekler | Karşı sistem |
|---|---|---|---|
| Hesabın oturumu düştü (`401`) | cevap gönderilmeden öbür hesaba **taşınır** | öbür hesaba | hiçbir şey görmez |
| Hesap kotaya takıldı (`429` + kota metni) | öbür hesaba **taşınır** | öbür hesaba; kilitli hesap 20 dk hiç denenmez | hiçbir şey görmez |
| Başka arıza (zaman aşımı, `5xx`, captcha reddi, boş cevap) | **taşınmaz**: `problem` | öbür hesaba | **1** `problem` |
| Sırada beklerken hesabı arızalandı | öbür hesabın sırasına geçer | — | hiçbir şey görmez |
| İki hesap da arızalı | — | upstream'e gidilmeden `problem`; 300 sn sonra hesap başına tek deneme | `problem` |

Ayrıntılar:

* **"Kota metni" hangisi?** `UPSTREAM_LIMIT_MARKERS` listesindeki ifadeler.
  Varsayılan liste canlıdakiyle aynıdır ve 18 Kasım'da görülen
  `daily spend limit reached` ifadesini **içermez**: o `429` bu tabloda "başka
  arıza" satırına düşer (canlıdaki gibi tekrar denenir, istek taşınmaz, **1**
  `problem`; sonraki istekler öbür hesaba gider). İkinci satırdaki gibi
  davranması — hemen taşınsın, karşı sistem hiçbir şey görmesin — isteniyorsa
  ifade env'e eklenir (`.env.example` → `UPSTREAM_LIMIT_MARKERS`, yorumlu hazır
  satır). Bu bir operatör tercihidir: gönderme davranışını değiştirir
  (`LESSONS_2026_11_14.md §16.6`, `§20`).
* **Neden yalnız `401` ve kota taşınıyor?** İkisi de hesaba özgü ve hızlıdır
  (upstream hemen reddeder). Zaman aşımı gibi arızalarda istek süresini zaten
  harcamıştır; öbür hesapta baştan denemek 180 sn sınırını ikiye katlardı.
* **Taşınan istek öbür hesabın sırasına girer.** O hesap yine aynı anda tek istek
  işler ve aralığını korur. Bu bekleyiş isteğin 180 sn'sinden düşer; süre orada
  dolarsa istek `problem` alır ama **öbür hesap arızalı sayılmaz** (isteği hiç
  görmedi).
* **Geri dönüş probe ile.** Arızalı hesaba gerçek istek gönderilmez; o hesabın
  keepalive probe'u gönderilir. Probe başarılı olunca hesap yeniden iş alır ve
  Telegram'a "✅ hesap yeniden çalışıyor" gelir. Neden gerçek istekle değil:
  takılı kalan bir hesap, payına düşen her isteği 180 sn bekletip `problem`e
  düşürürdü — öbür hesap çalışırken bu gereksiz.
* **Geri dönüş ne kadar sürer:** arıza tek seferlik bir aksaklık olsa bile hesap,
  **bir sonraki probe'una dek** devre dışı kalır; bu en geç bir keepalive aralığıdır
  (o hesabın son başarılı isteğinden sayılır: aralık 3600 ise en çok ~1 saat). Bu
  sürede bütün işi öbür hesap yapar. Beklemek istemezsen: §8'deki `resume`.
* **Keepalive kapalıysa** (`UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=0`) hesabı geri
  getirecek probe yoktur: arızalı hesap **30 dakikada bir** gerçek bir istekle
  denenir. Beklemek istemezsen: §8'deki `resume`.
* **Probe taşınmaz.** Probe kendi hesabını sınar; reddedilirse öbür hesaptan
  cevaplanıp "başarılı" sayılmaz.

### `ACCOUNT_FAILOVER=false`: katı bölüşüm

Her hesap **yalnız kendi payını** işler. Hesap arızalıyken payına düşen istekler
`problem` alır (300 sn bekletme, sonra tek deneme — tek hesaptaki kural), öbür
hesap kendi payını işlemeye devam eder. İstek taşınmaz, sırada bekleyen öbür
hesaba geçmez.

Ne zaman anlamlı: bir hesabın yükünün **hiçbir koşulda** yarının üstüne çıkmasını
istemiyorsan. Bedeli: bir hesap öldüğünde isteklerin yarısı karşı sistemin
yedeğine gider.

---

## 6. Keepalive (probe)

Her hesabın **kendi** probe döngüsü vardır:

* **Aralık, o hesabın son başarılı gerçek isteğinden sayılır.** Hesaba 13.00'te
  istek geldi ve sonra hiç gelmediyse probe 14.00'te atılır
  (`UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=3600` ise; ± `UPSTREAM_KEEPALIVE_JITTER_SECONDS`,
  varsayılan ± 5 dk). Araya gerçek istek girdikçe probe ertelenir: trafik
  sürdükçe hiç probe atılmaz. Bu, `UPSTREAM_KEEPALIVE_SKIP_WHEN_ACTIVE=true`
  (varsayılan) davranışıdır ve tek hesapta da aynıdır.
* **Sayaçlar ayrıdır.** 2. hesaba gelen istek yalnız 2. hesabın probe'unu
  erteler. Trafik bölündüğü için iki hesap da gerçek isteklerle tazelenir.
* **İlk probe'lar kaydırılır:** 1. hesap deploy'dan ~60 sn, 2. hesap ~120 sn
  sonra. İki hesap aynı saniyede yoklanmaz.
* **İstemler farklı adımdan başlar:** iki hesap aynı cümleleri aynı sırayla
  göndermez. **Ana hesap (`UPSTREAM_CURL`) tek hesaptaki sırayla, ilk cümleden
  başlar**; ikinci hesap bir adım ileriden. İkinci hesabın eklenmesi ana
  hesabın gönderdiklerini değiştirmez (`LESSONS §20.3`).
* **Arızalı hesabı geri getiren probe'dur** (§5).
* **Probe sayısı:** hiç trafik yokken her hesap kendi aralığıyla probe atar; iki
  hesap, toplamda iki kat probe demektir (hesap başına sayı tek hesaptakiyle
  aynı). Trafik varken ikisi de ertelenir.

Ölçüldü (aralık 1 sn'ye küçültülmüş): 2. hesaba 0,9. ve 1,5. saniyelerde gerçek
istek → o hesabın bir sonraki probe'u 2,5. saniyede (son gerçek istekten tam bir
aralık sonra); aradaki sürede probe yok. Hiç istek almayan 1. hesabın probe'ları
birer saniye arayla sürdü.

---

## 7. Telegram'a ne gelir

Kural aynı: **olay başına tek mesaj.** Fark: her hesabın kendi arıza kaydı vardır
ve mesaj hangi hesaptan söz ettiğini `Hesap:` satırıyla söyler. Tek hesapta
metinler değişmez.

**İstek öbür hesaba taşındı** (karşı sistem bir şey görmedi):

```
🔴 ApiWrapper — bir hesap cevap veremedi; istek diğer hesaba taşındı.
Hesap: hesap-1
Ne oldu: Upstream oturum kimliğini reddetti (HTTP 401): oturum düştü. [upstream_unauthorized]
Tür: KALICI — elle müdahale gerekir.
Yapılacak: …
Upstream cevabı: HTTP 401 · {"error":"Unauthorized"}
İstek req_… hesap-2 hesabından cevaplandı; karşı sisteme "problem" dönmedi.
Bundan sonra: yeni istekler çalışan hesaptan cevaplanır, karşı sistem etkilenmez. hesap-1 yeni iş almaz; kendi keepalive denemesi başarılı olunca geri döner.
Not: bu hesabın cURL'ü UPSTREAM_CURL değişkenindedir. Yeniden deploy bütün hesapların oturumunu sıfırlar; deploy edecekseniz hepsinin cURL'ünü birlikte tazeleyin. Acelesi yok: servis çalışan hesapla sürüyor.
Hesaplar: hesap-1 ARIZALI [upstream_unauthorized] · hesap-2 çalışıyor
```

**İstek taşınmadı** (karşı sistem `problem` aldı): başlık
`dış istek cevaplanamadı; karşı sisteme "problem" döndü.`, gerisi aynı düzende.

**Çalışan hesap kalmadı:** `Bundan sonra: çalışan hesap kalmadı; dış isteklere 5 dk
boyunca upstream'e gidilmeden "problem" denir; sonra tek istek denenir.`

**Düzelince:** `✅ ApiWrapper — hesap yeniden çalışıyor ve yeniden iş alıyor.` +
`Hesap:` + süre + arıza boyunca `problem` alan dış istek sayısı. Taşınan istekler
bu sayıya **girmez**.

Telegram mesajın (bot) öbür hesaba taşındıysa da aynı bildirim gelir; bot sana
normal cevabı yazar.

---

## 8. Teşhis

**`GET /v1/admin/queue`** — `accounts` bölümü:

```json
"accounts": {
  "count": 2,
  "failover": true,
  "lanes": [
    {"account": "hesap-1", "slot": 1, "usable": true,  "assigned_total": 37,
     "cooldown_remaining_seconds": 0.0, "queue": {…}, "upstream_health": {…}},
    {"account": "hesap-2", "slot": 2, "usable": false, "assigned_total": 36,
     "cooldown_remaining_seconds": 0.0, "queue": {…},
     "upstream_health": {"failing": true, "code": "upstream_timeout", …}}
  ]
}
```

* `assigned_total` — bölüşümle hesaba verilen iş sayısı (probe ve hesaba
  sabitlenmiş elle sınama istekleri sayılmaz). İkisi birbirine yakın olmalı;
  değilse biri bir süre devre dışı kalmıştır.
* `usable: false` — hesap şu an yeni iş almıyor (arızalı ya da kota dinlenmesinde).
* Eski `queue` / `upstream_health` alanları yerinde: **1. hesabın** sırası ve durumu.
* `alerts.accounts` — hesap başına açık arıza kodu ve `problem` sayısı.

**Tek bir hesabı elle sınamak** — isteği o hesaba sabitle:

```bash
curl -s https://<servis>/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "x-apiwrapper-account: 2" \
  -H "Content-Type: application/json" \
  -d '{"model":"max","messages":[{"role":"user","content":"Tek kelime: merhaba"}]}'
```

Sabitlenen istek hesabın sağlığına bakılmadan gönderilir ve başka hesaba
taşınmaz; başarılı olursa hesap yeniden iş almaya başlar. Başlık yalnız tam
yetkili anahtarla (`API_KEYS`) ve yalnız sohbet uçlarında çalışır; **`/v1/ask`
yok sayar** (karşı sistem hesap seçemez). Olmayan yuva → `400`.

**Devreden çıkmış hesabı beklemeden geri almak:**

```bash
curl -s -X POST https://<servis>/v1/admin/accounts/2/resume -H "Authorization: Bearer $API_KEY"
```

Arıza kaydını ve kota dinlenmesini kaldırır. Hesap gerçekten çalışmıyorsa ilk
istekte yeniden devreden çıkar.

**Loglar:** `account_switched` (istek taşındı: `from_account`, `to_account`,
`reason`), `request_reassigned` (sırada bekleyen taşındı), `upstream_request …
account=…` (hangi istek hangi hesaba gitti), `upstream_keepalive_* … account=…`.

**Metrikler:** `apiwrapper_account_assigned_total{account}`,
`apiwrapper_account_switches_total{reason}` (`switch` = kota yüzünden taşıma,
`auth` = kimlik reddi yüzünden taşıma, `quota` = kota kilidi görüldü),
`apiwrapper_request_queue_waiting{account}` (tek hesapta etiketsiz, eskisi gibi).

---

## 9. Bilinen sınırlar (dürüst liste)

| Konu | Durum |
|---|---|
| **Gerçek upstream** | Denenmedi. İlk gerçek deneme deploy sonrası kontrol listesidir (`ROLLBACK §14`). |
| **İki hesap aynı sunucu IP'sinden çıkar** | Aynı IP, aynı TLS parmak izi. Sağlayıcının iki hesabı ilişkilendirip ilişkilendirmediği **bilinmiyor, ölçülemedi**. Bölüşüm hesap başına istek sayısını yarıya indirir; IP başına sayıyı indirmez. |
| **Restart = iki oturum da ölür** | §2.4. Bir hesabı tazelemek için deploy etmek öbürünü de sıfırlar. |
| **Devre kesici ortak** | Art arda 5 erişilebilirlik hatası (zaman aşımı, `5xx`, düz `429`) bütün hesaplar için 30 sn devreyi açar. Bir hesap çalışırken araya giren başarılar sayacı sıfırlar; pratikte tek hesabın arızası devreyi açmaz. |
| **Dinamik captcha sağlayıcı** (`browser`/`external`) | Token hesaplar arasında paylaşılır (100 sn önbellek). Canlı kurulum `static`: her hesap kendi cURL'ünün token'ını kullanır. |
| **`SESSION_REUSE` açıkken** | Sohbetler tek hesapta kalır (§3); o trafik bölünmez. |
| **Taşınan isteğin süresi** | Öbür hesabın sırasında beklenen süre 180 sn'den düşer; o hesap uzun bir işle meşgulse taşınan istek `problem` alabilir. |
| **Hesap sayısı** | En çok 4 (`MAX_ACCOUNT_SLOTS`). 3–4 hesap birim testlerinde var; uçtan uca testler 2 hesapla. |
| **Keepalive kapalıyken geri dönüş** | 30 dk'da bir gerçek istekle; hesap hâlâ takılıysa o istek `problem` alabilir. Canlı kurulumda keepalive açık. |

---

## 10. Nerede ne var

| Ne | Nerede |
|---|---|
| Bölüşüm, arıza kuralı, sohbet bağlılığı | `app/services/lanes.py` |
| `UPSTREAM_CURL_2` → hesap yuvası, aynı hesap denetimi | `app/upstream/curl_runtime.py` |
| İsteğin hatta girmesi, taşınması | `app/services/completion_service.py` (`_admit`, `_reroute`) |
| Hesap başına probe | `app/services/keepalive.py`, `app/main.py` |
| Hesap başına arıza kaydı | `app/services/alerting.py` (`scope`) |
| Sabitleme başlığı | `app/api/v1/chat.py` (`x-apiwrapper-account`) |
| Testler | `tests/test_lanes.py`, `tests/test_two_accounts.py`, `tests/test_two_accounts_live.py`, `tests/test_upstream_curl_runtime.py` |
