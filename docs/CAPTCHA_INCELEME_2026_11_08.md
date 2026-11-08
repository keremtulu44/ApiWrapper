# CAPTCHA incelemesi — 2026-11-08 dilimi (maskelenmiş cURL ↔ kod karşılaştırması)

**Kapsam.** Operatörün paylaştığı cURL ile koddaki istek üretimini bağımsız
karşılaştırdık; kodda kanıtlanabilen hataları sentetik testlerle ürettik ve
düzelttik. Bu dallanma `ca97c37` üzerinde yapıldı.

**Zorunlu sınırlar (okumadan sonuç çıkarmayın).**

* Paylaşılan cURL'de hedef alan adı **bilerek `arena.ai` ile değiştirilmişti**.
  Bu nedenle hiçbir bulgu "şu domain şu politikayı uyguluyor" iddiası taşımaz;
  yalnızca *isteğin biçimi* (başlık adları, gövde alanları, çerez kurgusu)
  referans alındı.
* Maskelendiği söylenen metinde, maskeleme kapsamında olmayan **gerçek
  görünümlü oturum değerleri** bulundu: imzalı erişim JWT'si, `refresh_token`,
  üçüncü taraf kimlik sağlayıcısının erişim/yenileme anahtarları ve
  `cf_clearance`. Bunlar hiçbir yere **kopyalanmadı** (repo, log, test, rapor)
  ve bu incelemede hiç kullanılmadı. **Aksiyon önerisi:** o oturumu sağlayıcının
  "tüm oturumları kapat" akışıyla iptal edin; bundan sonra cURL paylaşırken
  `-b/--cookie`, `authorization` ve gövdedeki token alanlarını da silin.
* Canlı isteğe/üçüncü taraf doğrulama servisine erişim yok. Dolayısıyla
  "retmin sebebi X'tir" demenin imkânı yok; aşağıda **kanıt** ile **olasılık**
  ayrı bölümlerdir.

> **2. tur (itirazlar sonrası).** İlk sürüm; K3'ün nedenini, `reset_context()`
> adını ve `cookie_only` deneyinin durumunu **olduğundan güçlü** yazmıştı.
> İtirazların hepsi doğruydu: K3'ün neden iddiası çürütüldü, iki yarım düzeltme
> tamamlandı, bir öneri zaten denenmişti. Kanıt zinciri ve neyin değiştiği **§6**.

## 1) cURL ile kodun ürettiği isteğin karşılaştırması

Kontrol, paylaşılan cURL'ün *yapısı birebir* kopyalanıp değerleri sahteleriyle
değiştirilerek yapıldı (`scripts/compare_curl.py` mantığıyla; sentetik
değerlerle). Sonuç: **doğru içe aktarılmış bir cURL'de statik başlıkların
tamamı örtüşüyor.**

| Öge | Tarayıcı cURL'ü | Kod | Durum |
| --- | --- | --- | --- |
| `content-type` | `text/plain;charset=UTF-8` | aynı sabit (`build_stream_headers`) | örtüşüyor |
| `accept`, `accept-language`, `priority`, `dnt`, `pragma`, `cache-control` | var | `PASSTHROUGH_HEADERS` ile korunuyor | örtüşüyor |
| `sec-ch-ua`, `sec-ch-ua-mobile: ?0`, `sec-ch-ua-platform: "Windows"` | masaüstü Chromium/Edge | hem passthrough hem UA'dan hesaplama | örtüşüyor |
| `sec-fetch-*` | `empty/cors/same-origin` | aynı | örtüşüyor |
| `origin` / `referer` | `<site>/text/direct` | `origin=base_url`, `referer_path` cURL'den | örtüşüyor |
| `x-deployment-id` + `x-arena-web-deployment` | aynı değer | passthrough + kanarya tazelemesi | örtüşüyor |
| `cookie` | tek satır, ham | **`Cookie` başlığı olarak değil, httpx jar'ından** gidiyor | bilinçli tasarım; kayıpsızlık testi eklendi |
| `authorization` | **yok** (tarayıcı cookie-only) | importer `UPSTREAM_AUTH_FROM_COOKIE=true` seçip JWT'yi çerezden okuyarak **ek başlık üretiyor** | koda özgü sapma (aşağı §3/olasılıklar) |
| Gövde: `id, mode, modelAId, userMessageId, modelAMessageId, userMessage{content, experimental_attachments, metadata}, modality, recaptchaV3Token, cx` | var | `UpstreamPayload` aynı alanları üretir (`cx` yalnız `UPSTREAM_CX_SEND=true` ise) | örtüşüyor |
| `traceparent` / `tracestate` | oturuma özgü değer | kod her istekte yeni `traceparent` üretir | zararsız |

Önemli düzeltme: `docs/CAPTCHA_TEKNIK_INCELEME.md` "tarayıcı açıkça
mobil/Android istemci ipuçları gönderiyor" diyordu. **Bu cURL için doğru değil**:
`sec-ch-ua-mobile: ?0`, `sec-ch-ua-platform: "Windows"`, UA masaüstü Edge.
Önceki rapor o hükmü farklı (mobil) bir cURL örneğinden çıkarmış; bu cURL'de
cihaz ipucu uyumsuzluğu yok.

## 2) KANITLANAN sorunlar ve yapılan düzeltmeler

Aşağıdaki her biri kod okunarak + **önce kırık, sonra geçen** testle
doğrulandı. Hiçbiri "CAPTCHA retminin sebebi budur" iddiası taşımaz; hepsi
"gerçek bir tarayıcının asla üretmeyeceği istek/durum üretiyorduk" sınıfında.
(İlk sürümde K3 bu kuralı ihlal ediyordu; §6.)

**K1 — Başlık adı büyük/küçük harf duyarlı biriktirildi → çelişkili çift satır.**
`parse_extra_headers()` adları olduğu gibi bırakıyor, `build_*_headers` ise
küçük harfli hesaplanmış değerlerle `dict.update()` yapıyordu.
`UPSTREAM_EXTRA_HEADERS=Sec-CH-UA=...` gibi bir yazımda istek **aynı başlıktan
iki farklı satırla** gidiyordu (httpx `raw` başlık listesiyle doğrulandı).
Aynı duyarlılık `build_stream_headers` içindeki `if name in headers`
karşılaştırmasına da sıçramıştı: `X-Deployment-Id=dpl_ESKİ` yazıldığında taze
kanarya damgası **eskisinin yerini alamıyordu** — yani bayat sürüm damgasıyla
gitmeye devam ediyordu.
*Düzeltme:* adlar tek kaynakta (`parse_extra_headers`) küçük harfe indirgenir;
damga değişimi `k.lower()` ile yapılır.
*Testler:* `tests/test_request_fingerprint_consistency.py` →
`test_stream_request_never_sends_two_conflicting_client_hint_lines`,
`test_fresh_deployment_stamp_replaces_mixed_case_configured_header`,
`test_page_headers_do_not_duplicate_client_hints_either`.

**K2 — Donmuş client hint'leri farklı bir UA ile gönderilebiliyordu.**
Hesap yuvalarında `UPSTREAM_USER_AGENT_2` hesaba özgü,
`UPSTREAM_EXTRA_HEADERS` (dolayısıyla `sec-ch-ua*`) **tüm hesaplarda paylaşılan**
bir alandı. Sonuç: yuva-2'nin POST'u kendi UA'sı + yuva-1'in cihaz ipuçlarıyla
gider (ör. Windows UA + `sec-ch-ua-mobile: ?1`). Bunu ayrıca `Set-Cookie`
kanıtıyla karıştırmamak için ayırıyoruz: **tek hesaplı kurulumda bu çelişki
oluşmuyor**; çok hesaplı/kısmen güncellenmiş `.env` kurulumlarında oluşuyor.
*Düzeltme:* `client_hint_conflicts()` — gönderilen UA ile çelişen
`sec-ch-ua-mobile` / `sec-ch-ua-platform` / marka sürümü UA'da bulunmayan
`sec-ch-ua` değerleri düşürülür ve UA'dan hesaplanan tutarlı değer kullanılır;
tek seferlik `upstream_client_hint_mismatch` uyarısı bırakılır. Chromium
ailesi dışındaki UA'larda (Firefox/Safari `sec-ch-*` göndermez) hiçbir şeye
dokunulmaz; eşleşen ipuçları **birebir korunur**. Kasıtlı taklit için
`UPSTREAM_CLIENT_HINT_AUTOFIX=false`.
*Testler:* `test_hint_conflict_is_detected_against_ua`,
`test_conflicting_frozen_hints_are_replaced_by_ua_derived_values`,
`test_matching_frozen_hints_are_kept_untouched`,
`test_non_chromium_user_agent_keeps_its_own_hints`,
`test_autofix_can_be_disabled_for_deliberate_spoofing`.

**K3 — Kimlik dönüşünde transport ile CAPTCHA üretim bağlamı kopuyordu.**
*(Kod asimetrisi kesin; "bu olayın nedeni" iddiası 2. turda **geri alındı**.)*
Asimetri kodda gerçek: `/upstream/refresh`, `/upstream/cookie`, `/upstream/curl`
gibi **elle** yollar `recaptcha.invalidate()` çağırıyor; **otomatik** yollar — arka
plan yenileyici, `UpstreamAuthRejected` sonrası tek deneme, upstream'in
`Set-Cookie` ile oturumu çevirmesi — yalnız `reset_account_clients()` çağırıyor.
Yani birinde sayfa + token bağlamı tazeleniyor, diğerinde transport.

Bunu **bu 403'ün nedeni olarak sunmak yanlıştı**, iki nedenle:

1. Log `Set-Cookie` geldiğini gösteriyor, **oturum token'ının döndüğünü
   göstermiyor** — hangi çerezin döndüğü logda yok. Orijinal cURL'de
   `__Secure-sb-access-token` 1 saniyelik `Max-Age` ile süresi dolmuş
   görünüyordu, `sb-id-token`/`sb-refresh-token` 400 günlük. "Set-Cookie geldi"
   ve "kimlik döndü" ayrı iddialardı; ikincisi kanıtlanmadı.
2. Kod *ilk* reddedilişte zaten sayfayı yeniliyor (`_events` içinde her
   `recaptcha_rejected` işareti → `invalidate()`); **ikinci deneme de 403** aldı.
   Bayat sayfa tek başına sebep olsaydı ikinci denemenin geçmesi gerekirdi.

*Düzeltme (hijyen olarak korundu):* `UpstreamClient.add_identity_listener()`
**yalnız erişim token'ı gerçekten değiştiğinde** bildirir; bağ `app.state.recaptcha`
üzerinden kurulur, böylece `/settings` sağlayıcıyı değiştirse de kopmaz. Bu hatta
bağlı dört iyileştirme:

* **Ad:** `reset_context()` → **`reset_token_context()`**. Eski ad Playwright
  *browser context*'inin silindiğini söylüyordu; kodun yaptığı ısınma **sayfasını**
  çöpe atıp bekleyen canlı token'ı düşürmekti — browser context, UA, viewport ve
  `cf_*` çerezleri korunuyordu. Yeni ad davranışı anlatıyor; docstring neyin
  **kasıtlı olarak korunduğunu** da yazıyor.
* **Hayalet çerez budama:** yenilenen oturumda enjekte ettiğimiz isimlerden
  düşenler (ör. `__Secure-sb-access-token`, `accessToken`) ertesi oturumlu
  gezinmede **eski değeriyle** taşınıyordu. Playwright'da bağlam çerezleri
  **`clear_cookies()`** ile silinir — `remove_cookies` adında bir yöntem
  **yoktur** (1.42 ve 1.49 wheel'lerinde `BrowserContext` yüzeyi
  `add_cookies` / `clear_cookies` / `cookies` olarak doğrulandı). İlk sürümde
  uydurma ada bakıldığı için budama gerçek tarayıcıda sessiz no-op'tu; taklit
  context aynı uydurma yöntemi tanımladığından test bunu görmüyordu. Şimdi:
  * `clear_cookies(name=...)` destekleniyorsa (1.42'de yok, 1.49'da var;
    sürüm tahmini yapılmaz, imza çalışırken okunur) yalnız hedef ad silinir —
    `stats["browser_cookie_prune_mode"] = "filtered"`;
  * desteklenmiyorsa (1.42) `cookies()` anlık görüntüsü alınıp silinmeyenler
    `add_cookies` ile geri yazılır — `"rewritten"`;
  * yöntem hiç yoksa hata yutulmaz, `stats["browser_cookie_prune"] =
    "unsupported"` yazılır.
  `cf_clearance` / `__cf_bm` / `__cf_turn` / `__cfruid` / `_cfuvid` / `cf_chl_*`
  **hiçbir yolda silinmez** — onları temizlemek her token üretiminde Cloudflare
  yarışını baştan tetiklerdi. Bu koruma **ad üzerinden** çalışır: bu çerezler
  `UPSTREAM_COOKIE` içinde gelebildiği (paylaşılan cURL örneğinde
  `cf_clearance` vardı) ve biz onları jar'a enjekte ettiğimiz için "sayfanın
  kendi kazandığı çerez" varsayımı yeterli değildi — 3. turdaki hâl, korunan ad
  ayardan düşünce *bizim koyduğumuz* kopyayı siliyordu. Korunan ad
  `_injected_cookie_names`te tutulmaya devam edilir ve
  `stats["browser_cookie_prune_skipped_protected"]` listelenir. `add_cookies`
  alan biçimini reddederse URL ile bir kez daha denenir (sessiz kayıp olmaz).
* **Boşalan çerez ayarı:** `UPSTREAM_COOKIE` temizlendiğinde fonksiyon
  `if not cookie_header: return` ile budamaya **hiç gelmiyordu** — yani
  enjekte ettiğimiz çerezler tam da "herkes silinsin" denilen anda jar'da
  kalıyordu. Artık boş ayarda da budama çalışır (korunanlar hariç) ve takip
  listesi sıfırlanır.
  *Test koruması:* budama yolu AST üzerinden denetlenir — Playwright'ın
  yayınlamadığı bir ad kodlandığı anda test kırılır (`test_prune_only_uses_cookie_apis_playwright_actually_publishes`),
  taklidin aynı uydurma yöntemle testi yeniden yeşile çekmesi mümkün değil.
* **Bildirim atılmaz, birleştirilir:** ilk sürümde 60 sn'lik eşik *kısıyordu* —
  art arda iki gerçek rotasyonda ikinci bildirim hiç gelmiyordu. Artık aynı
  değerin tekrarı sha256 özetle bastırılır (boş yere sayfa yakmamak için),
  **farklı** değerin bildirimi sakin pencere sonuna ertelenir ama asla
  düşürülmez (`_IDENTITY_NOTIFY_QUIET_SECONDS`, `reset_account_clients()`
  bekleyen bildirimi gecikmeden teslim eder **ve zamanlayıcıyı iptal eder**).
* **Teşhis:** `_ingest_set_cookie`, **`Set-Cookie` başlığı gelen her yanıtta**
  bir kez `upstream_identity_update {slot, reason, changed, access_token_rotated,
  refresh_token_rotated, untracked_cookie_names}` yazar (yalnız **bayrak ve ad**,
  değer yok) ve aynı özet `identity_update` olarak `GET /v1/admin/auth`
  cevabında görünür. Okuma sözleşmesi bilinçli olarak dardır:
  * satır **yoksa** → tek sonuç: *o yanıtta `Set-Cookie` yoktu*. "Kimlik
    değişmedi" sonucu **çıkarılamaz** (erken dönüşler artık kayıt bırakıyor:
    `no_stored_cookie`, `empty_cookie_jar`, `cookie_unchanged`);
  * bayrak `null` → karar değerlendirilemedi; `false` → gerçekten dönmedi;
  * `untracked_cookie_names` doluysa hedef daha önce saklamadığımız bir ad set
    etmiştir: satıra eklenir (`replace_cookie_values`) ama
    `UPSTREAM_TOKEN_COOKIE_NAMES` listesine eklenmedikçe token kıyasına girmez.
  * kayıt `Set-Cookie` **taşayan ve bizim işlediğimiz** yanıtlar içindir. Ret
    yolu (4xx) `_ingest_set_cookie`yi çağırmıyordu — yani olaydaki 403 için
    kayıt hiç yoktu. Artık `_observe_set_cookie` ret yanıtını `reason =
    rejected_not_ingested` ile **yalnız gözlemler**: `status`,
    `set_cookie_count`, `set_cookie_names` yazılır, çerezler **kasıtlı olarak**
    uygulanmaz (hata sayfasının değeri kalıcı oturum satırına sızmaz).
  "`Set-Cookie` geldi" ile "oturum döndü" ayrımı bundan sonra logdan
  okunabilir — ilk sürümde eksik olan tam olarak buydu.

*Testler:* `tests/test_identity_rotation_captcha_context.py` (27 test) —
bildirimin `reset_account_clients`'ta yapılması, erişim token'ı döndüğünde
bildirim, ilgisiz çerezde sıfırlama olmaması, art arda rotasyonların
birleştirilip **hiçbirinin düşürülmemesi**, aynı değerin ikinci kez
bildirilmemesi, `reset_token_context`'in `rejected` sayacını artırmaması,
bekleyen token'ın düşürülmesi, hayalet çerez budaması (challenge çerezleri
kalarak), uygulama bağının kurulması, `/v1/admin/auth` özetinin değer
sızdırmaması.

**K4 — `cx` oturumdan oturuma taşınıyordu.** `cx` gövdede görünen, oturuma
bağlı bir bağlam işaretidir. `build_settings()` yalnız yeni cURL'de `cx`
**varsa** alan yazdığı için, `cx` taşımayan yeni bir cURL uygulandığında eski
oturumun `UPSTREAM_CX` + `UPSTREAM_CX_SEND=true` değerleri hayatta kalıyordu —
yarı-taze istemci bağlamı. Kod zaten aynı tehlikeyi `UPSTREAM_ACCESS_TOKEN`
için düşünüyordu (orada açıkça sıfırlanıyor); runtime "yoksa varsayılana dön"
döngüsünde `UPSTREAM_MODE` var, `UPSTREAM_CX` yoktu.
*Düzeltme:* gövde ayrıştırılabilir JSON ama `cx` taşımıyorsa `UPSTREAM_CX=""`
ve `UPSTREAM_CX_SEND=false` yazılır + uyarı; gövde **hiç** ayrıştırılamadıysa
"hüküm yok" sayılır ve alanlara dokunulmaz. `scripts/curl_to_env.py` özeti de
artık `UPSTREAM_CX(_SEND)` satırlarını gösterir.
*Testler:* `test_new_curl_without_cx_clears_the_previous_session_context`,
`test_unparsable_body_does_not_clear_cx_without_evidence`,
`test_runtime_curl_apply_clears_stale_cx`.

**K5 — Sürüm damgası kanaryası hesabın ayarlarını görmüyordu.**
`DeploymentIdResolver`, keşif GET'ini ve `active/configured` kararını **global**
`self.settings` üzerinden kuruyordu; oysa GET aynı hesaba ait client
jar'ıyla yapılıyor, POST ise hesabın UA/dil başlıklarıyla gidiyordu. Sonuç:
aynı oturumun iki isteği iki farklı tarayıcı gibi görünür ve `UPSTREAM_*_2`
hesaplarında kanarya hiç devreye girmez.
*Düzeltme:* `refresh()/ingest()/active_for()` isteği gönderecek `Settings`'i
alır (verilmezse geriye uyumlu biçimde globali kullanır).
*Testler:* `test_canary_probe_uses_the_account_user_agent_not_the_global_one`,
`test_active_for_reports_the_account_own_configuration`.

**K6 — 403 triyajı iz bırakmıyordu (teşhis boşluğu).** `CAPTCHA_MARKERS`
bilinçli olarak özgü ifadeleri arar; hedef "captcha" kelimesini geçen ama o
listede olmayan bir 403 dönerse hata jenerik görünür ve operatör retmi
CAPTCHA'ya mı yoksa oturuma mı bağlayacağını ayıramaz.
*Düzeltme:* hiçbir işaret ailesi eşleşmeyen 4xx'lerde `upstream_error_unclassified`
satırı eklenir; yalnız hangi ailelere benzediği (`captcha_like`, `bot_like`,
`verify_like`, `forbidden_like`) ve gövde uzunluğu yazılır — **gövde metni
loglanmaz**. Sınıflandırma bilinçli olarak değiştirilmedi: kanıt yokken
"CAPTCHA" diye işaretlemek önceki yanlış teşhisin ters yönü olurdu.
*Testler:* `tests/test_captcha_triage_signal.py` (4 test; gizli değerin loga
sızmaması dahil).

**K7 — Yarı uygulanmış kimlik sessiz kalıyordu.** `apply_refresh()` yeni
`access_token`'ı ayarlara yazar, çerezi güncelleyemezse (biçim tanınmadıysa)
`Authorization` taze + `Cookie` bayat çiftiyle gitmeye devam ederdik.
*Düzeltme:* bu durumda `upstream_refresh_cookie_not_rewritten` uyarısı
 üretilir (token değeri yazılmaz) ve operatör `/upstream/cookie` yoluna
yönlendirilir. *Test:* `test_refresh_warning_when_cookie_cannot_be_rewritten`.

## 3) OLASILIKLAR — kanıtlanmadı, canlı ölçüm ister

1. **Retmin kendisi hâlâ kanıtsız.** `upstream_recaptcha_rejected` log satırı
   yoksa 403 CAPTCHA değildir; `cf-mitigated: challenge` varsa CAPTCHA değil
   Cloudflare katmanıdır. Yanıt gövdesindeki tek tipleştirilebilir alanlar
   (durum, `content-type`, `cf-mitigated`, K6'nın aile bayrakları) olmadan
   sebep ayrıştırılamaz.
2. **Tek kullanımlık token'ın iki kez harcanması** mümkün ama bu incelemede
   görülen bir kod yoluyla kanıtlanamadı: `token_factory` her POST denemesinden
   hemen önce üretim yapıyor, retry'da yeni token alıyor. Kalan belirsizlik,
   tarayıcının **kendi** sayfasının da (route kaldırıldıktan sonra) aynı token'la
   tekrar POST atabilmesi; bu, yan-etkisizlik testi gerektiren eski bir açık iş.
3. **`Authorization` başlığının fazlalığı.** Tarayıcı cookie-only gidiyor;
   wrapper çerezden JWT okuyup ek başlık üretiyor. Bu başlık bayat kaldığında
   (K7) veya çerez kuşağından farklılaştığında ret tetikleyip tetiklemeyeceği
   bilinmiyor. **Bu ayrım deneylendi ve elendi:**
   `UPSTREAM_AUTH_HEADER_MODE=cookie_only` etkinken de hem kullanıcı isteği hem
   tekrar denemesi 403 aldı (§6). Yani bu olayın ayırt edicisi
   `Authorization`/çerez yığılması değil.
4. **Damga/oturum kovaları.** cURL'de yapışık oturum çerezindeki `dpl_…`
   değeri ile `x-deployment-id` başlığı **farklıydı**; maskelenmiş değerlerle
   bundan sonuç çıkarılmaz. Kanarya kapalıysa (`deployment_id_refresh_enabled`
   veya damga hiç gönderilmiyor) bayat damga 409/403 üretebilir.
5. **Anonim açılan ısınma sayfası.** `browser` sağlayıcısı oturum çerezlerini
   bilinçli olarak enjekte etmiyor; canlı sayfa girişsiz açılırsa token
   sentetik bootstrap sayfasından üretilir ve risk sinyali zayıflayabilir.
   Artık `stats["browser_session_cookies_skipped"]` ve
   `recaptcha_browser_anonymous_context` uyarısıyla bu durum gözlenebilir —
   davranış değişmedi, yalnız ölçülebilir hâle geldi.
6. **Çoklu hesapta paylaşılan parmak izi.** `UPSTREAM_EXTRA_HEADERS` hesap
   başına değil; K2 çelişkiyi otomatik gideriyor ama asıl çözüm hesap-başına
   başlık katmanıdır (açık iş).

## 4) Doğrulama

* `python -m pytest -q` → **704 geçti, 1 atlandı** (atlanan: Playwright kurulu
  olmadığı için `importorskip` ile geçen API yüzeyi testi); `ruff check app tests
  scripts` ve `mypy app` temiz (61 kaynak dosyası).
* 3. turda Playwright iddiaları **yayınlanan wheel üzerinden** doğrulandı:
  `pip download playwright==1.42.0` ve `playwright-1.49.1` wheel'lerindeki
  `async_api/_generated.py` içinde `BrowserContext` çerez yöntemleri
  `add_cookies` / `clear_cookies` / `cookies`; `remove_cookies` **hiçbir
  sürümde yok**, filtreli `clear_cookies(name=...)` yalnız yenilerde. Aynı
  denetim `browser.py`nin kullandığı tüm Playwright adlarına uygulandı:
  yayınlarda bulunmayan ad çıkmadı.
* 2. turda eklenen beş davranışın (`ingest` kapağı, bildirim birleştirme,
  hayalet çerez budama, `reset_token_context` adı, `identity_update` özeti)
  her biri için de aynı yöntem izlendi: test **önce kırıldı**, düzeltmeden
  sonra geçti.
* Ağ yok: tüm testler `httpx.MockTransport`/RESPX ve sentetik verilerle
  koştu. Gerçek hedefe, Google reCAPTCHA'ya veya Cloudflare'e istek
  atılmadı; Playwright başlatılmadı.
* Karşılaştırma çıktısı (§1) ve K1–K5'in her biri için **önce kırık test**
  üretildi; düzeltme sonrası testler geçiyor. Testler sahte değer kullanır,
  paylaşılan cURL'ün hiçbir değeri test dosyalarına yazılmadı.

## 5) Sıradaki işler (öncelik sırasıyla)

1. Bir sonraki canlı 403'te `upstream_error_unclassified` satırındaki bayrakları
   ve `cf-mitigated` varlığını oku; `captcha_like` true + marker false ise
   `CAPTCHA_MARKERS` listesini **hedefin kendi metnine göre** genişlet
   (varsayılanı değiştirme).
2. Isınma sayfasının oturumlu/oturumsuz açıldığını (`recaptcha_browser_anonymous_context`)
   ve token'ın hangi yoldan üretildiğini (`stats["last_token_source"]`) aynı
   olay penceresinde karşılaştır; yerel/izole test sayfası olmadan canlı-click
   izolasyonunu "çözüm" olarak sunma.
3. `recaptcha_rejected` alındığında `UPSTREAM_CX_SEND=false` ile tek değişkenli
   deneme yap. **`UPSTREAM_AUTH_HEADER_MODE=cookie_only` denemesi yapıldı ve
   negatife döndü** (§6); tekrar önerilmiyor.
4. Bir sonraki olayda kimlik dönüşü iddiasını **`upstream_identity_update`
   satırını okumadan kurma**: `access_token_rotated: false` ise K3 hattı hiç
   gündeme gelmez. (Bu cümlenin kendisi ilk sürümün eksikliğidir.)

## 6) İtirazlar ve düzeltmeler (2. tur)

İlk sürüm operatörün itirazlarıyla sınandı; **itirazların hepsi doğruydu**.
Tablo iddia → kod/log kanıtı → son hal biçimindedir.

| İtiraz | Kanıt | Sonuç |
| --- | --- | --- |
| "`Set-Cookie` tek başına oturumun döndüğünü kanıtlamaz; sayfa zaten yenileniyor olabilir" | `_events`: her `recaptcha_rejected` işareti `invalidate()` çağırıyor; `reset_account_clients()` `Set-Cookie` yolunda **koşulsuz** çağrılıyor. Logda hangi çerezin döndüğü yok; cURL'de `__Secure-sb-access-token` `Max-Age=1`, `sb-id-token`/`sb-refresh-token` 400 gün | **Kabul.** K3'ün neden iddiası çürütüldü, "kod asimetrisi"ne indirgendi. Kanıtsızlığın kendisi düzeltilebilir olan kısmı: `upstream_identity_update` bayrakları eklendi |
| "`cookie_only` deneyini zaten yaptık, ikisi de 403 aldı" | canlı gözlem (rapor dışında) | **Kabul.** Deney §5'ten çıkarıldı, §3'te "elendi" olarak işaretlendi; negatif sonuç ayırt edici bilgi olarak kayda geçti |
| "`reset_context()` yanlış ad; ikisi arasındaki fark ne?" | Eski gövde yalnız `_reset_warm_page = True` + bekleyen token'ı sıfırlıyordu; browser context/UA/viewport/`cf_*` çerezlerine dokunmuyordu | **Kabul.** `reset_token_context()`; dokümantasyonu neyi **kasıtlı koruduğunu** yazıyor. Ek: enjekte edilen hayalet çerezlerin budanması |
| "60 sn'lik kısma gerçek bir rotasyonu düşürebilir" | `if now - self._last_identity_notify < 60.0: return` — bildirim ertelenmiyor, **atılıyordu** | **Kabul.** Birleştirme + değer kıyası: aynı değer bildirilmez, farklı değer hiç kaybolmaz |
| "`ingest` düzeltmesi yarım kaldı" | `ingest()` imzası `settings` alıyordu, gövde `if not self.active:` ile global okuyordu; `/v1/upstream/deployment/refresh` bu kapağa çarpıp `ingested: False` dönüyordu | **Kabul ve tamamlandı.** `ingest()` → `active_for(settings)`; keşif GET'i de hesabın `deployment_probe_path`'ini kullanıyor. `tests/test_request_fingerprint_consistency.py` içindeki iki yeni test (biri pozitif kontrol) kilitliyor |

Ayrıca küçültülen iddia: "yakalanıp tüketilmemiş token bir **sonraki** isteğe
sızabiliyordu" **kanıtlanamadı** — `_captured_live_token` ataması ile
`_present_live_token` arasında `await` yok; sızıntı akışı kodda kurulamadı.
Taşıma savunma amaçlı düşürülüyor, dil "kanıtlanamadı"ya çekildi.

**Kod dışı ders.** Üç itirazın ortak kökü tek hataydı: *kodda görülen asimetri*
ile *olayın nedeni* ayrımının rapor dilinde kayması. K1/K2/K4/K5 ve `ingest`
kapağı gibi bulgular kod okunarak kanıtlanabilir; "bu 403'ü X üretmedi" ya da
"X üretti" ise ancak bayraklı log/ölçümle söylenebilir. İkincisi artık kodun
içinde ölçülebilir (`upstream_identity_update`, `browser_cookie_prune`,
`last_token_source`, `upstream_error_unclassified`).

## 7) 3. tur itirazları (kod hataları; neden oldukları kanıtlanmadı)

Operatör `dfbeff3` üzerinde üç nokta daha gösterdi. Üçü de **kodda doğruydu**;
üçü de **canlı CAPTCHA retminin nedeni olarak kanıtlanmış değildir** — bu
ayrım §2/§3'teki gibi korunuyor.

| İtiraz | Kanıt | Sonuç |
| --- | --- | --- |
| Çerez budama gerçek Playwright'ta çalışmıyor; test uydurma yöntemi tanımladığı için geçiyor | 1.42 + 1.49 wheel'lerinde `BrowserContext.remove_cookies` yok, `clear_cookies()` var; kod `getattr(ctx, "remove_cookies")` arıyordu → gerçek bağlamda **sessiz no-op** | **Kabul.** `clear_cookies()` kullanılıyor; filtre destekli/filtresiz/desteklenmeyen üç yol + `stats` sayaçları. Testler artık gerçek API yüzeyini modelliyor; AST koruma testi uydurma ada dönüşü kırıyor |
| Bekleyen bildirim kaydı kaybolabiliyor; `reset_account_clients()` zamanlayıcıyı iptal etmeden referansı siliyor | `_deliver_identity_rotated()` yalnız `self._identity_flush_handle = None` yapıyordu — `call_later` geri çağırması yaşıyor ve pencere sonunda **bir kez daha** çalıyordu | **Kabul.** `_cancel_identity_flush()` (iptal + referans + bekleyen değer özeti) hem teslimde hem kapanışta kullanılıyor; bekleyen özet teslim edilince dedupe durumu da ilerliyor. `test_reset_account_clients_cancels_the_pending_flush` fazladan bildirimi kilitliyor |
| "`Set-Cookie` gelen her yanıt loglanır" doğru değil; kayıt yokluğu kimliğin değişmediğini kanıtlamaz | Dört erken dönüş (`set-cookie` yok / `old_cookie` yok / jar boş / değer aynı) logdan **önce** `return` ediyordu | **Kabul.** Erken dönüşler artık `reason` ile kayıt bırakıyor, değerlendirilemeyen bayrak `null` kalıyor; sözleşme (satır yok = `Set-Cookie` yok) docstring + §2'de yazılı |

Beklenmedik bir bulgu da yola çıktı: `replace_cookie_values` yeni bir çerez
adını kayıtlı satıra **ekliyor**, yani "hedef yeni ad set etti" durumu sessizce
kaybolmuyor. Buna rağmen o ad token kıyasına girmediği için `untracked_cookie_names`
alanı kayda eklendi; ilk sürümdeki "kıyaslama yeni adı görmez" endişesi bu
nedenle **yarı doğruydu** ve budama dalı (ulaşılamaz `untracked_only`) kaldırıldı.

## 8) 4. tur itirazları (budama güvenliği ve teşhis kapsamı)

| İtiraz | Kanıt | Sonuç |
| --- | --- | --- |
| "`cf_clearance`/`__cf_bm` hiçbir yolda silinmez" doğru değil: wrapper enjekte ettiyse silinebiliyordu | Enjekte filtresi yalnız `arena-auth*` adlarını atlıyordu; dolayısıyla `UPSTREAM_COOKIE`deki `cf_clearance` jar'a **bizim tarafımızdan** girip `_injected_cookie_names`e yazılıyordu. Ad ayardan düşünce `stale` kümesine girdi → silinme adayı | **Kabul.** Koruma artık **ad tabanlı** ve iki katmanda: budama kümesi `_is_protected_browser_cookie()` ile filtrelenir, `_prune_injected_cookies()` de aynı filtreyi uygular (iç çağrı hatalarına karşı). Atlananlar `stats["browser_cookie_prune_skipped_protected"]`de görünür. Test: `test_protected_challenge_cookies_survive_even_when_we_injected_them`, `test_prune_never_targets_protected_names_even_if_asked` |
| Yeni çerez ayarı tamamen boşsa fonksiyon budamaya gelmeden dönüyordu | `if not cookie_header: return` satırı `stale` hesabından **önceydi** | **Kabul.** Erken dönüş kaldırıldı; boş ayarda da budama çalışır, korunanlar kalır, takip listesi sıfırlanır. Test: `test_empty_cookie_setting_still_prunes_what_we_injected` |
| Teşhis kaydı yalnız `_ingest_set_cookie()` çağrılan yanıtlarda var; 403 gibi hata yanıtlarında çağrılmıyor | Akış yolunda kayıt `if status < 400:` bloğunun içindeydi; challenge/`RecaptchaRejected` dalları ondan **önce** raise ediyor | **Kabul.** `status >= 400` için `_observe_set_cookie()` eklendi: iz bırakır, **uygulamaz**. Test: `test_rejected_response_is_recorded_but_not_applied` (ayar dizesinin değişmediği de doğrulanıyor). Kaydın yokluğunun anlamı §2'de üç nedene indirildi; "kimlik değişmedi" yine kanıtlanamaz |

Bu üçü de **kod hatasıdır**; hiçbiri canlı CAPTCHA reddinin nedeni olarak
kanıtlanmış değildir. İlk ikisi düzeltmenin kendi güvenliğini (yanlış çerez
silme, hiç silmeme), üçüncüsü gözlemin kapsamını ilgilendirir — yani
bir dahaki olayda elimizdeki kanıtın kalitesini.
