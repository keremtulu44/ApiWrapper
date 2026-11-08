# CAPTCHA ve ağ bağlamı: teknik inceleme

> **2026-11-08 dilimi:** maskelenmiş cURL ile kodun karşılaştırması, kanıtlanan
K1–K7 hataları ve düzeltmeleri için bkz. `docs/CAPTCHA_INCELEME_2026_11_08.md`. O dilimde
buradaki "tarayıcı mobil/Android client hint gönderiyor" hükmünün paylaşılan cURL için
geçerli olmadığı görüldü (masaüstü Edge, `sec-ch-ua-mobile: ?0`).


Tarih: 2026-11-07. İnceleme, çalışma ağacındaki kodun okunması ve aşağıdaki
resmî Google, Cloudflare, Playwright, HTTPX ve MDN belgelerinin karşılaştırılmasıyla
hazırlandı. Canlı upstream, Google assessment sonucu ve Render çalışma zamanı
ayarları bu ortamda test edilmedi. Dolayısıyla aşağıda **kodda görülen** durumlar
ile yalnızca **olası** canlı etkiler ayrıdır; gerçek ret nedenini kanıtlamazlar.

## Kısa sonuç

Bir token'ın üretilmesi, onun upstream tarafından kabul edildiğini veya yeterli
risk skoruna sahip olduğunu göstermez. Google'ın Enterprise assessment yanıtında
`tokenProperties.valid`, `invalidReason`, `action`, `hostname` ve `riskAnalysis`
alanları ayrıdır. Bu wrapper assessment yanıtını almıyor; upstream yalnızca genel
bir hata döndürürse yanlış key/action, süre, skor veya başka bir politikayı
birbirinden kesin olarak ayıramaz. [1](https://docs.cloud.google.com/recaptcha/docs/interpret-assessment-website)

Google, Enterprise tokenlarının tek kullanımlık olduğunu ve iki dakika içinde
değerlendirilmesi gerektiğini belirtiyor. Dinamik sağlayıcılarda token'ın her
POST denemesi öncesinde üretilmesi bu gerekliliğe uygundur; yine de ağ ve hedef
gecikmesi varsa hedefin iki dakikalık süreyi karşılayacağı garanti edilemez.
[2](https://docs.cloud.google.com/recaptcha/docs/create-assessment-website)

## Kanıtlı kod bulguları ve etkileri

### 1. Tarayıcı ile POST farklı ağ yollarını kullanabilir — yüksek

* `app/upstream/client.py` `_build_client()` HTTPX'e `upstream_proxy` geçiriyor.
  `app/services/recaptcha/browser.py` `startup()` ise Playwright tarayıcısına
  veya context'ine bu proxy'yi geçirmiyor. Proxy ayarlıysa iki süreç **aynı
  proxy yapılandırmasını kullanmıyor**; egress adreslerinin mutlaka farklı
  olacağı her dağıtımda kesin değildir.
* Playwright proxy'nin browser veya context seviyesinde yapılandırıldığını
  belgeliyor. [3](https://playwright.dev/python/docs/network)
* Google assessment girdilerinde IP, user agent ve JA3/JA4 gibi alanlar
  bulunabilir; bunların bu belirli upstream tarafından kullanıldığı veya
  uyumsuzluğun otomatik ret anlamına geldiği bilinmiyor.
  [4](https://docs.cloud.google.com/recaptcha/docs/create-assessment-website)
* Cloudflare `cf_clearance` değerinin cihaz/ziyaretçiye bağlı olduğunu, challenge
  isteği ile çözümün farklı IP'den yapılmasının challenge döngüsüne yol
  açabileceğini bildiriyor. Bunlar reCAPTCHA doğrulamasından ayrı bir katmandır.
  [5](https://developers.cloudflare.com/cloudflare-challenges/concepts/clearance/)
  [6](https://developers.cloudflare.com/cloudflare-challenges/concepts/how-challenges-work/)

### 2. Hesap seçimi ve tarayıcı bağlamı aynı kapsamda değil — yüksek

* `CompletionService._events()` seçilen `account` ile HTTP istemcisinin hesap
  ayarlarını oluşturur; dinamik token için kullanılan `self.recaptcha.get_token`
  ise hesap bilgisi almaz (`app/services/completion_service.py`).
* `BrowserRecaptchaProvider` tek context kullanır ve sayfa/çerez/UA için global
  `self.settings` okur (`browser.py` `startup`, `_set_cookies_from_settings`,
  `_warm_target_url`). Seçili hesap farklı domain, çerez, UA veya referer
  taşıyorsa, token üretim bağlamı ile POST bağlamı farklı olabilir. Tokenın
  salt bu sebeple reddedildiği canlı olarak doğrulanmamıştır.
* Ayrıca `arena-auth` ile başlayan çerezler browser token üretimi için bilinçli
  olarak atlanır. Bu, canlı sayfanın anonim açılmasına veya giriş gerektiren
  formun bulunamamasına yol açabilir. Auth çerezlerinin kullanılmaması tek
  başına bir hata değildir; canlı-click yolunun koşullarını sınırlar.

### 3. Canlı-click istek yakalaması tüm URL'leri güvenceye almıyor — yüksek

* `browser.py` `_stream_url()` şablondaki `{chat_id}` alanını sabit
  `warmup-chat-id` ile değiştiriyor. `_extract_token_from_click()` route'unu bu
  sabit yol ve sonunda `**` ile kuruyor. Gerçek chat ID bu sabitten farklıysa
  yolun orta bölümünde eşleşmez; dolayısıyla istek yakalanmayabilir.
* Route 10 saniye sonra `unroute` ile kaldırılıyor. Sayfa kapatılmadan önce
  daha geç başlayan bir istek bu route ile engellenmez. Gerçek yan etkinin
  yaşanıp yaşanmadığı, sayfanın ne gönderdiğine ve oturumuna bağlıdır; mevcut
  kodun “gerçek mesaj gönderilmez” garantisi bütün durumlarda kanıtlı değildir.
* Playwright, `page.route` ile service worker tarafından ele alınan isteklerin
  yakalanmayabileceğini ayrıca belirtiyor. Context şu anda service worker'ları
  engelleyecek bir ayar vermiyor. [7](https://playwright.dev/python/docs/network)
* Bu yol yetkili bir yerel test sayfası ve ağ mock'u ile; farklı chat ID,
  gecikmiş istek, servis çalışanı ve iptal durumları dahil test edilmelidir.
  Üretim hesabında deneme amaçlı gerçek mesaj gönderilmemelidir.

### 4. Statik token tekrar kullanımı — kısmen giderildi

Önceki istemci, 409/5xx yanıtından sonra aynı statik tokenı aynı istekte
tekrar gönderebiliyordu. Artık HTTP yanıtı alındıktan sonra aynı statik tokenla
retry yapılmaz; kota nedeniyle hesap devrinde farklı bir statik token varsa devir
mümkündür. Auth reddi sonrasında aynı statik tokenla otomatik refresh/retry de
yapılmaz. Ayrı API istekleri ve restart boyunca tüm kullanımlar takip edilmez;
bu nedenle statik token sürekli kullanım için hâlâ güvenilir değildir.
[2](https://docs.cloud.google.com/recaptcha/docs/create-assessment-website)

### 5. POST tekrarları ve belirsiz sonuçlar — kısmen giderildi

Read/write timeout, read/write ve protokol hatalarında POST sonucu belirsizdir:
artık otomatik tekrar yapılmaz. Connect/pool hataları öncesinde istek
ulaşmadığından tekrar edilebilir. Dinamik/noop ile 5xx gibi bazı HTTP yanıtları
ve özel deployment 409 kurtarma yolu hâlâ tekrar denenebilir; bu yanıtlar bile
sunucuda işlemin yapılmadığını garanti etmez. `RETRY_MAX_ATTEMPTS=1` ve 409
retry ayarlarını kapatmak bu otomatik yolları kısıtlar, istemci tarafındaki
manuel tekrarları engellemez. Kesin güvence sunucunun idempotency sözleşmesini
gerektirir. [8](https://www.python-httpx.org/advanced/timeouts/)
[9](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Idempotency-Key)

### 6. Cloudflare tespiti — başlık ve hata sınıfı düzeltildi

`cf-mitigated: challenge` artık gövde veya durum kodundan bağımsız tanınıyor ve
`upstream_challenge` hatasına çevriliyor. Bu yanıt oturum refresh'ini veya hesap
devrini tetiklemez. Başlık yoksa 403/503 gövde işaretleri sezgisel kalır;
`cloudflare` kelimesi tek başına yeterli sayılmaz. Challenge'ın kesin sebebi veya
cookie süresinin dolduğu varsayılmaz.
[10](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)

### 7. Zaman aşımı ayarı tüm browser aşamasının üst sınırı değil — orta

`RECAPTCHA_BROWSER_TIMEOUT`, execute ve bootstrap gezinmesi gibi belirli
adımlarda kullanılıyor. İlk sayfa gezinmesi, grecaptcha bekleme, canlı tıklama
ve hazırlık beklemeleri ayrı sürelerle çalışıyor; canlı tıklama iki kez
denenebilir. Ayrıca JavaScript içindeki 30 saniyelik sabit timer,
`RECAPTCHA_BROWSER_TIMEOUT=45` olsa bile execute promise'ini önce
reddedebilir. Bu yüzden 45 saniye bütün token üretim süresinin garantili üst
sınırı veya execute için gerçek 45 saniyelik süre değildir.

### 8. External sağlayıcı serbest biçimli yanıtları fazla iyimser yorumluyor — orta

`app/services/recaptcha/external.py` `request`, `code`, `result` gibi herhangi
bir boş olmayan string'i token sayıyor. Asenkron görev kimliği veya hata metni
HTTP 200 içinde gelirse token diye gönderilebilir. Mevcut adaptörün sözleşmesi
tek istekte doğrudan token döndürmektir; her üçüncü taraf servisle uyumlu
olduğu varsayılmamalı. Gerçek entegrasyonda açık yanıt şeması ve hata durumları
sağlayıcının kendi belgelerine göre test edilmelidir.

## Teşhis için sınırlar ve güvenli doğrulama

1. `/readyz` yerel yapılandırma kontrolüdür; gerçek token üretiminin veya
   upstream kabulünün kanıtı değildir. `/v1/admin/auth` üretim sayacı da yalnızca
   token üretildiğini gösterir, assessment skorunu göstermez.
2. Canlı arızada status, içerik türü, `cf-mitigated` varlığı ve maskelenmiş hata
   sınıfı ayrılmalı; token, cookie, authorization, tam cURL ve ham assessment
   tokenı loglara veya raporlara alınmamalıdır.
3. Yetkili sistem sahibi erişimi varsa Google assessment tarafında `valid`,
   `invalidReason`, `action`, `hostname`, risk skoru ve gerekçeler kontrol
   edilmelidir. Bunlar yoksa belirli ret sebebi kesin olarak söylenemez.
   [1](https://docs.cloud.google.com/recaptcha/docs/interpret-assessment-website)
4. Sahip olunan bir test sitesi ve Google'ın test key'leri ile yerel/entegrasyon
   testi yapılabilir; test skoru gerçek üretim skorunu temsil etmez ve başka bir
   sitenin anahtarını doğrulamaz. [11](https://cloud.google.com/recaptcha-enterprise/docs/faq)
5. Üçüncü taraf hedefler için sağlayıcının desteklediği resmî API/entegrasyon ve
   erişim koşulları tercih edilmelidir. Testler hedefin korumalarını veya kullanım
   sınırlarını aşmaya yönelik olmamalıdır.

## Öncelikli işler

* Önce canlı-click yakalamasının yan etkisizliği için izolasyon testleri ve
  belirsiz POST sonucu için güvenli yeniden deneme politikası.
* Ardından Cloudflare challenge başlığını gövde sezgisinden ayrı ele alan,
  sır sızdırmayan teşhis ve static token yaşam döngüsünün açıklığa kavuşturulması.
* Çok hesaplı veya proxy'li dağıtımlarda üretim bağlamını açıkça modelleyip test
  etmek; gerçek upstream davranışına dair kanıt yokken “kesin çözüm” dememek.

## Bu incelemede yerel doğrulama

* Ağ bağlantısı olmayan bir HTTPX `MockTransport` denemesinde, static token
  taşıyan POST'a 503 döndürüldüğünde ikinci POST'un aynı gövdeyi kullandığı
  gözlendi. Gerçek servis ve gerçek token kullanılmadı.
* Varsayılan şablon yolda sabit `warmup-chat-id` içeren route örüntüsünün farklı
  bir chat ID ile eşleşmediği Python `fnmatch` ile örneklendi. Bu, kalıptaki
  sabit bölümün uyuşmazlığını gösterir; gerçek Playwright/service-worker
  davranışının yerel browser entegrasyon testi ayrıca gereklidir.
* `tests/test_captcha_single_use.py`, `tests/test_cloudflare_challenge.py`,
  `tests/test_recaptcha.py` ve `tests/test_account_pool.py`: **66 test geçti**.
  Bu testlerin geçmesi yukarıdaki açık bulguların çözüldüğü anlamına gelmez.

## Kullanıcının paylaştığı maskelenmiş cURL üzerinden ek inceleme

Bu bölümde gerçek/maskelenmiş domain, cookie, CAPTCHA tokenı, model ID'si,
`cx` değeri ve deployment değerleri kaydedilmemiştir. Kullanıcının domaini
maskelediği özellikle dikkate alınmıştır; hedefin gerçek alan adı ve mevcut
ayarları hakkında çıkarım yapılmaz. Canlı istek gönderilmemiştir.

### İsteğin gösterdikleri ve göstermedikleri

* Örnek, `create-evaluation` yoluna giden, `text/plain` içerik türüyle JSON
  taşıyan bir POST. Gövdede `mode`, `modelAId`, `userMessage`,
  `recaptchaV3Token` ve `cx` alanları var. Bu alanların o anda gönderilmiş
  olması, tek başına her birinin zorunlu olduğunu veya isteğin başarılı
  olduğunu kanıtlamaz.
* Alan adının `recaptchaV3Token` olması, Google entegrasyonunun kesinlikle
  `v3` ya da `enterprise` olduğunu kanıtlamaz. cURL gövdesinde gönderilen
  response token, ayrı bir site key değildir; örnekte açıkça etiketlenmiş
  site key veya `execute` action bilgisi yoktur. Sayfanın yetkili entegrasyon
  belgeleri veya uygulamanın kendi yapılandırması gerekir.
  [12](https://docs.cloud.google.com/recaptcha/docs/api-ref-checkbox-keys)
* Tarayıcı açıkça mobil/Android istemci ipuçları gönderiyor. Playwright context
  ise kodda 1366×768 viewport ile açılıyor ve `is_mobile` / `has_touch`
  belirtilmediğinden Playwright varsayılanları false. Sadece User-Agent'ı
  değiştirmek diğer cihaz parametrelerini otomatik olarak bu cURL'deki cihazla
  aynı yapmaz. Bunların hedef reddinin nedeni olduğuna dair canlı kanıt yoktur.
  [13](https://playwright.dev/python/docs/api/class-browser)
  [14](https://developer.chrome.com/docs/privacy-security/user-agent-client-hints)

### Yerel, tamamen sentetik tekrar üretimleri

Aşağıdaki denemelerde hiçbir kullanıcı değeri kullanılmadı; yalnızca benzer
alan isimleri ve uydurma cookie/token verileri kullanıldı.

1. **Cookie yüzdesel kodlaması:** `parse_cookie_header()` `%7B...%7D` gibi bir
   cookie değerini çözüyor; eski `UpstreamClient._build_client()` sonucu HTTPX
   jar'a koyduğu için yerel istekte orijinal biçim korunmuyordu. Şimdi taşıma
   değerleri decode edilmeden jar'a yazılıyor; kimlik çıkarımı için eski decode
   davranışı korunuyor. Gerçek hedefte hataya yol açıp açmadığı bilinmiyor.
2. **Tekrarlı cookie isimleri:** Örnekte aynı ada ait iki giriş var;
   `parse_cookie_header()` ilkini tutup sonrakini atıyor. Sentetik denemede
   bu davranış görüldü. HTTP Cookie başlığı Path/Domain niteliklerini içermez;
   yalnız başlıktan iki girdinin hangi cookie kapsamına ait olduğu veya
   hangisinin doğru olduğu kesin olarak anlaşılamaz.
   [15](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cookie)
3. **Authorization farkı:** Paylaşılan örnekte ayrı bir `Authorization` başlığı
   görünmüyor. Projedeki importer, JWT okunabilen cookie-only örneğinde
   `UPSTREAM_AUTH_FROM_COOKIE=true` seçiyor; daha sonra koddan Authorization
   üretiliyor. Bu, kopyalanan isteğe göre ek bir başlık demektir. Bunun
   upstream tarafından reddedileceği gösterilmiş değildir; sentetik iki parçalı
   session cookie ile otomatik seçim yerel olarak doğrulandı.
4. **Token/cx içe aktarımı:** `browser` seçiliyken gönderilmiş CAPTCHA tokenı
   statik değere çevrilmez; `cx` mevcutsa gönderimi açılır. Bu davranış
   sentetik örnekte gözlendi. `cx` alanının sunucu tarafından nasıl kullanıldığı
   veya ne kadar süre geçerli olduğu bu cURL'den anlaşılamaz.
5. **Site key sezgisi:** Eski importer tüm header/gövde içinde `6L...`
   arıyordu; sentetik analitik cookie yanlışlıkla key seçildi. Artık yalnız
   açıkça etiketlenmiş site-key alanları dikkate alınıyor; cookie ve response
   token içindeki eşleşmeler kullanılmıyor. Etiketli bir değerin gerçekten hedef
   için doğru olduğu da yalnız biçiminden kanıtlanamaz.
6. **Cookie öznitelikleri:** Browser sağlayıcısı header'dan cookie eklerken
   `httpOnly=False` ve `sameSite=Lax` gibi değerler atıyor. Request Cookie
   başlığı orijinal cookie özniteliklerini içermediğinden bu özelliklerin özgün
   tarayıcıyla aynı olduğu söylenemez. Bu durum tek başına canlı hata kanıtı
   değildir. [15](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cookie)

### Güvenlik

Kullanıcının paylaştığı metin oturum/yenileme bilgisi ve kişisel veri içerebilecek
uzun kodlu cookie parçaları barındırıyor. Erişim tokenı süresi geçmiş olsa bile
refresh/session değerleri geçerli olabilir. Gerçek değerler paylaşıldıysa,
sağlayıcının oturumları sonlandırma/yenileme mekanizmasıyla iptal edilmeli; yeni
secret'lar sohbet veya Git'e gönderilmemelidir.

İlgili importer/auth testlerinin ayrı çalıştırılması: **125 test geçti**.
Bunlar sentetik birim testleridir; gerçek oturumun veya CAPTCHA'nın geçerliliği
hakkında sonuç üretmez.


## Otomatik refresh sonrası yeniden inceleme

Kullanıcının “refresh öncesi çalışıyordu” bilgisi üzerine yerel olarak iki somut
uyumsuzluk testle tekrar üretildi ve düzeltildi:

* Eski `rewrite_session_cookie`, bütün başlığı decode edip sözlükten kuruyordu:
  ilgili olmayan `%XX` değerleri değişiyor, tekrar eden isimler kayboluyordu.
  Artık sadece seçilen session parçaları (veya düz JWT cookie) değiştiriliyor;
  diğer ham parçalar ve sıraları korunuyor. `Set-Cookie` ile settings'e yansıma
  da ham parçaları koruyor. HTTPX jar'a ilk kurulumda ve reset'te değerler
  decode edilmeden ekleniyor. Önce/sonra, hesap havuzu ve client reset'i ile
  sentetik testlerde gönderilecek header kontrol edildi.
* Eski kod normal Base64 (`+`, `/`, `=`) üretiyordu. Supabase SSR'nin
  [tasarımı](https://github.com/supabase/ssr/blob/main/docs/design.md)
  `base64-` öneki ardından padding'siz Base64URL öngörüyor; kaynak kodundaki
  [decoder](https://github.com/supabase/ssr/blob/main/src/utils/base64url.ts)
  `+` ve `/` karakterlerini geçersiz sayıyor (padding `=` karakterini ise
  yok sayıyor). Unicode metadata içeren sentetik örnekte eski kod `+` üretti.
  Yenileme artık padding'siz Base64URL kullanıyor ve JSON içeriği korunuyor.
  Hedef uygulamanın aynı decoder'ı kullanıp kullanmadığı doğrulanmamıştır.

Sınırlamalar: HTTPX cookie jar, yalnız header'dan başlatıldığında aynı isimli
çerezleri farklı Path/Domain'lerle yeniden oluşturamaz; ayardaki ham tekrarlar
korunsa da bunların tümünün ağ isteğinde korunacağı garanti edilmez. Jar'a gelen
`Set-Cookie` güncellemelerini kullanmak için ham başlığı her isteğe sabitlemek
uygun değildir. Diskteki runtime snapshot, farklı başlangıç seed'inde bile
expiry karşılaştırmasına göre yeni cURL'ün önüne geçebilir; burada bu politika
değiştirilmedi ve canlı ortamda gerçekleştiği kanıtlanmadı. Bu değişiklikler
canlı CAPTCHA veya oturum kabulünü garanti etmez; eski/değersiz refresh token
ya da başka istemci bağlamı sorunları ayrıca mümkün olabilir.

Son tam doğrulama: **598 test geçti**, Ruff ve mypy temiz; ağ çağrısı yerine
sentetik veriler ve mock taşıma kullanıldı.


## Genel düzenleme — ilk çalışma dilimi

* Sentetik testlerle site key yanlış seçimi, `cf-mitigated` başlığının atlanması,
  belirsiz ağ hatası sonrası POST tekrarı ve statik tokenın 409/503'te yeniden
  gönderilmesi tekrar üretildi ve yukarıdaki düzeltmeler yapıldı.
* Upstream hata gövdeleri, kota metni önizlemeleri ve harici sağlayıcının ham
  hata yanıtları log/API hata metnine kopyalanmıyor. Bu, tüm uygulamada başka
  kanallardan secret sızmayacağının kanıtı değildir.
* Canlı-click izolasyonu, browser/proxy ve çoklu hesap bağlamı, external yanıt
  şemasının sıkılaştırılması ve 5xx için idempotency hâlâ açık işlerdir. Bu turda
  canlı tarayıcı/upstream çağrısı yapılmadı.
* Tam test kümesi: **616 geçti**; Ruff ve mypy temiz. Bir Starlette/httpx
  deprecation uyarısı mevcut.
