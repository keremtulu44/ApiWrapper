# Sahte cURL ile yerel uçtan uca inceleme

Bütün değerler uydurmadır. `upstream.test`, `auth.test` ve `solver.test` gerçek
servis olarak kullanılmadı; HTTPX çağrıları RESPX ile mock'landı. FastAPI
TestClient uygulama yaşam döngüsünü ve API uçlarını çalıştırdı. Aşağıdaki JWT
imzalı/doğrulanmış gerçek oturum değildir; hiçbir değer gerçek kimlik veya CAPTCHA
belirteci değildir. Bu örnek gerçek bir servise gönderilmek için değildir.

## Örnek sahte cURL

```sh
curl 'https://upstream.test/nextjs-api/stream/create-evaluation' -H 'cookie: prefs=%7B%22lang%22%3A%22tr%22%7D; arena-auth-test.0=base64-eyJhY2Nlc3NfdG9rZW4iOiAiZXlKaGJHY2lPaUFpYm05dVpTSjkuZXlKbGVIQWlPaUF4TnprME1EVTBPVFk0ZlEuc2lnbmF; arena-auth-test.1=0dXJlIiwgInJlZnJlc2hfdG9rZW4iOiAiZmFrZS1yZWZyZXNoLWJlZm9yZSIsICJ1c2VyIjogeyJuYW1lIjogIlRlc3Qg8J-miiJ9fQ; __cf_bm=fake-bot-cookie' -H 'referer: https://upstream.test/text/direct' -H 'user-agent: SyntheticBrowser/1.0' -H 'sec-ch-ua-mobile: ?1' -H 'sec-ch-ua-platform: "Android"' --data-raw '{"id": "old-chat-id", "modelAId": "fake-model-123", "mode": "direct-battle", "cx": "fake-context", "userMessage": "hello", "recaptchaV3Token": "old-fake-captcha"}'
```

`tests/test_synthetic_curl_e2e.py` sahte JWT sürelerini her çalıştırmada yeniden
üretir; yukarıdaki örnek yalnız biçimi göstermek içindir.

## Çalıştırılan senaryolar

1. `UPSTREAM_CURL` içe alma; sentetik iki parçalı oturum, kodlu cookie, mobil
   client hint, `mode`, `cx`, model ve kullanıcı aracısı kontrolü.
2. Sahte external sağlayıcıdan her POST için farklı token alma ve non-stream
   yanıtı OpenAI uyumlu JSON'a dönüştürme.
3. Mock `Set-Cookie` sonrasında yönetici refresh ucu, yenilenen cookie, refresh
   token ve Authorization'ın hesap/client tarafına yansıması; streaming SSE ve
   `[DONE]` kontrolü.
4. Yerel state dosyasından yeniden başlatma; eski refresh tokenın canlanmaması.
5. Süresi dolmuş sentetik oturumda otomatik yenileme ve ilk POST'un yeni
   kimlikle gönderilmesi.
6. Çalışan servise yeni cURL uygulanması, aynı access token süresi, cURL'de model
   olmaması ve yerel restart kontrolü.
7. Render-env benzeri ayarlarla (gerçek Render çağrısı olmadan) snapshot'ın
   model, cookie ve `cx` değerlerinin seçimi.
8. CAPTCHA reddi, 503, Cloudflare challenge ve read timeout için hata/retry;
   tekrar edilen dinamik denemelerde farklı sahte tokenlar.

## Bulunup düzeltilen tutarsızlıklar

* Yönetici cURL güncellemesi model kimliğini raporluyor ancak canlı model
  registry'sine uygulamıyordu. Model artık POST'a yansıtılıyor; cURL model
  içermiyorsa yapılandırılmış varsayılan kullanılıyor.
* Yerel state geri yüklemesinden sonra registry eski başlangıç cURL modelini
  kullanabiliyordu. Artık geri yüklemede kazanan model kullanılıyor.
* Yönetici cURL güncellemesinde aynı access token `exp` değeri eski runtime
  snapshot'ını seçip yeni cookie/modeli ezebiliyordu. Açık hot-swap'ta verilen
  cURL kazanır; başlangıç/restartta yenilenmiş snapshot'ı koruyan uzlaştırma
  devam eder. Açıkça eski/geçersiz cURL verilirse bu da uygulanabilir.
* Render-env benzeri restart'ta yeni snapshot seçilse bile `cx` veya site key
  eski cURL ile karışabiliyordu. Snapshot geri yüklemesinde bu alanlar korunur;
  `cx` senaryosu test edildi.

## Sonuç ve sınırlar

* Sahte cURL test dosyası: **10 test geçti** (parametreli durumlar dahil).
* Tam test kümesi: **626 test geçti**, Ruff ve mypy temiz; Starlette/httpx için
  bir deprecation uyarısı mevcut.
* Gerçek Playwright/Chromium, Google reCAPTCHA, Cloudflare, gerçek hedef, proxy
  egress veya Render API'si test edilmedi. Sahte sağlayıcının kabul ettiği
  istekler gerçek servisin de kabul edeceğini kanıtlamaz.
* Tekrarlı cookie isimlerinin özgün Path/Domain'leri request başlığından geri
  kazanılamaz. Dinamik tokenlarla bazı HTTP hata yanıtlarında retry devam eder;
  sunucu idempotency desteği olmadan işlem tekrarı riski tamamen kalkmaz.
