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

**K3 — Otomatik kimlik dönüşünde CAPTCHA üretim bağlamı yenilenmiyordu.**
Belirtilen belirtiyle (otomatik istek başarılı → hemen ardından kullanıcı
isteği ve tekrar denemesi CAPTCHA 403) doğrudan örtüşen tek kanıtlı asimetri:
`/upstream/refresh`, `/upstream/cookie` ve `/upstream/curl` gibi **elle**
yollar `recaptcha.invalidate()` çağırıyordu; **otomatik** yollar — arka plan
yenileyici (`run_token_refresher`), `UpstreamAuthRejected` sonrası tek deneme
ve upstream'in `Set-Cookie` ile oturumu çevirmesi — yalnız
`reset_account_clients()` yapıyordu. Yani POST taze çerezle giderken, token'ı
üreten tarayıcı sayfası ölü oturumun çerezleriyle ayakta kalıyordu.
Ayrıca `BrowserRecaptchaProvider.invalidate()` yakalanmış ama henüz
tüketilmemiş token'ı **düşürmüyordu**; `invalidate()` ağ yanıtından sonra kilit
dışında çağrıldığı için o token bir sonraki isteğe sızabiliyordu.
*Düzeltme:* sağlayıcıya red sayacını kirletmeyen `reset_context()` kancası
eklendi; `UpstreamClient.add_identity_listener()` ile bağlandı ve
`reset_account_clients()` + erişim token'ının gerçekten döndüğü
`_ingest_set_cookie` yolunda çağrılır (`Set-Cookie` yolu, hedef her yanıtta
çerez tazelerse ilk token süresinin katlanmaması için 60 sn ile kısılır;
`_body_signature`/`upstream_client_hint_mismatch` logları hariç hiçbir yerde
değer yazılmaz). Uygulama yaşam döngüsü bu kanalı `app.state.recaptcha`
üzerinden bağlar, böylece `/settings` sağlayıcıyı değiştirse bile bağlantı
kopmaz.
*Testler:* `tests/test_identity_rotation_captcha_context.py` (12 test) —
bildirimin `reset_account_clients`'ta yapılması, `Set-Cookie` ile erişim
 token'ı döndüğünde bildirim + kısma, ilgisiz çerezde sıfırlama olmaması,
`reset_context`'in `rejected` sayacını artırmaması, tarayıcının bekleyen
token'ını düşürmesi, uygulama bağlantısının kurulmuş olması.

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
   bilinmiyor. Test: `UPSTREAM_AUTH_HEADER_MODE=cookie_only` ile tek istek.
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

* `python -m pytest -q` → **688 geçti** (652 taban + 36 yeni/yeniden
  düzenlenmiş); `ruff check app tests scripts` ve `mypy app` temiz.
* Ağ yok: tüm testler `httpx.MockTransport`/RESPX ve sentetik verilerle
  koştu. Gerçek hedefe, Google reCAPTCHA'ya veya Cloudflare'e istek
  atılmadı; Playwright başlatılmadı.
* Karşılaştırma çıktısı (§1) ve K1–K5'in her biri için **önce kırık test**
  üretildi; düzeltme sonrası testler geçiyor. Testler sahte değer kullanır,
  paylaşılan cURL'ün hiçbir değeri test dosyalarına yazılmadı.

## 5) Sıradaki üç iş (öncelik sırasıyla)

1. Bir sonraki canlı 403'te `upstream_error_unclassified` satırındaki bayrakları
   ve `cf-mitigated` varlığını oku; `captcha_like` true + marker false ise
   `CAPTCHA_MARKERS` listesini **hedefin kendi metnine göre** genişlet
   (varsayılanı değiştirme).
2. Isınma sayfasının oturumlu/oturumsuz açıldığını (`recaptcha_browser_anonymous_context`)
   ve token'ın hangi yoldan üretildiğini (`stats["last_token_source"]`) aynı
   olay penceresinde karşılaştır; yerel/izole test sayfası olmadan canlı-click
   izolasyonunu "çözüm" olarak sunma.
3. `recaptcha_rejected` alındığında `UPSTREAM_CX_SEND=false` ve
   `UPSTREAM_AUTH_HEADER_MODE=cookie_only` ile tek değişkenli deneme yap —
   wrapper'a özgü iki sapma bunlar; sırayla devre dışı bırakmak retmi
   ayırt etmenin en ucuz yolu.
