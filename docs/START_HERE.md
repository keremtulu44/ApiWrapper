# Başlangıç dosyası (START HERE)

Bu dosya, sonraki oturumun **tek başına** işe başlayabilmesi için gereken her şeyi
sıraya koyar: önce oku, sonra çalıştır, sonra tespit et, sonra düzelt.

Kurallar:
- Yorum ve tavsiye yok; komut, dosya yolu, satır numarası var.
- Satır numaraları `arena/cd32810d-apiwrapper` / `a0e49a1` anına aittir.
- Kod değişince satırlar kayar; her yerde fonksiyon/sabit adı da verilmiştir.

---

## 0. Tek komutla başla

```bash
cd /home/user/ApiWrapper
cat docs/START_HERE.md          # bu dosya
cat docs/HANDOFF.md             # dosya/satır haritası (301 satır)
```

Sonra §1'deki kurulumu yap, §2'deki doğrulamayı çalıştır.

---

## 1. Kurulum (her oturumda gerekir)

`.venv` anlık görüntüde **yoktur**, her oturumda yeniden kurulur:

```bash
cd /home/user/ApiWrapper
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Dal hazırlama (yeni oturum dalı hep `main` tabanında açılır):

```bash
git fetch origin arena/c55e601b-apiwrapper:refs/remotes/origin/arena/c55e601b-apiwrapper -f
git reset --hard origin/arena/c55e601b-apiwrapper
```

`/tmp` altındaki dosyalar oturumlar arasında **silinir**; betikleri yeniden yaz.

---

## 2. Doğrulama (değişiklik yapmadan önce ve sonra)

```bash
cd /home/user/ApiWrapper
.venv/bin/pytest -q          # beklenen: 589 passed
.venv/bin/ruff check app tests   # beklenen: All checks passed!
.venv/bin/mypy app           # beklenen: Success: no issues found in 61 source files
git diff --check             # beklenen: çıktı yok
```

Bu üçü yeşil değilse düzeltme yapılmaz; önce mevcut kırık bulunur.

---

## 3. Okuma sırası

| # | Dosya | Satır | Ne için |
|---|---|---|---|
| 1 | `docs/HANDOFF.md` | 301 | Dosya/satır haritası — hangi davranış nerede |
| 2 | `docs/MERGE_TARGET.md` | 82 | Merge/onay kuralı: merge = deploy, onaysız merge yok |
| 3 | `README.md` | 586 | Genel bakış, ayar listesi, kurulum |
| 4 | `docs/BIG_PICTURE.md` | 113 | Üç ömür katmanı (kimlik / türev / telemetri) |
| 5 | `docs/COOKIE_LIFECYCLE.md` | 152 | Çerez ömrü, `refresh_token`, parçalı çerez `.0`/`.1` |
| 6 | `docs/TROUBLESHOOTING.md` | 330 | 401/403/409/429 teşhis sözlüğü |
| 7 | `docs/RENDER.md` | 347 | Render runbook, env, log olayları |
| 8 | `docs/ARCHITECTURE.md` | 224 | Dosya mimarisi, veri akışı (satır 61-105) |
| 9 | `docs/MOBILE_CURL.md` | 202 | Telefondan çerez/cURL alma sınırları |
| 10 | `docs/AUDIT_2026_11.md` | 84 | Önceki denetim kaydı |
| 11 | `docs/CAPTURE_TIMELINE.md` | 190 | Üç cURL'ün ölçülmüş zaman çizelgesi: hangi değer ne zaman döner |

Okuma bittiğinde şu üç sorunun cevabı bilinir: istek hangi sırayla işlenir,
hangi değer nerede saklanır, hangi hata kodu hangi katmana işaret eder.

---

## 4. Kod içinde gezinme — giriş noktaları

| Ne arıyorsun | Dosya:satır |
|---|---|
| Servislerin kurulumu | `app/main.py:119` `lifespan()` |
| Ayarların tamamı | `app/core/config.py:27` `Settings` |
| Sohbet ucu | `app/api/v1/chat.py:23` |
| Teşhis ucu (`/tani` bunu okur) | `app/api/v1/admin.py:552` `auth_diagnostics()` |
| cURL yükleme ucu | `app/api/v1/admin.py:327` |
| İstek döngüsü + retry | `app/services/completion_service.py:349` |
| Hata sınıflandırma | `app/upstream/client.py:1010-1044` |
| Gövde üretimi | `app/schemas/upstream.py:43` `to_wire()` |
| CAPTCHA sağlayıcıları | `app/services/recaptcha/__init__.py:11` `PROVIDERS` |
| Kalıcılık | `app/upstream/state_store.py:67` `PERSISTED_FIELDS` |

Tam liste: `docs/HANDOFF.md` §4-§13.

---

## 5. Genel problem tespiti — sırayla yürütülecek adımlar

Her adım tek bir soruyu yanıtlar. Cevap "hayır" ise o katmanda durulur.

### 5.1 Süreç ayakta mı
```bash
curl -sS "$WRAPPER_URL/healthz"
curl -sS "$WRAPPER_URL/readyz"
```
Beklenen: `200`. `readyz` hata kodu verirse `docs/RENDER.md` §4 (satır 124-141).

### 5.2 Kimlik okunuyor mu
```bash
curl -sS -H "Authorization: Bearer $API_KEY" "$WRAPPER_URL/v1/admin/auth" | python3 -m json.tool
```
Bakılacak alanlar (`app/api/v1/admin.py:552`):
- `token.found` — kullanılan token var mı
- `token.expires_in_seconds` — kalan süre
- `cookie_token.found` / `cookie_token.expired` — çerezdeki oturum
- `refresh_token` — yenileme mümkün mü
- `session_cookie_candidates` — parçalı `.0`/`.1` durumu

Okuma mantığı: `app/upstream/auth.py:527` `extract_access_token()`,
`app/upstream/auth.py:144` `merge_chunked_cookies()`,
`app/upstream/auth.py:442` `extract_refresh_token()`.

### 5.3 CAPTCHA üretimi çalışıyor mu
Aynı cevapta `recaptcha` bloğu (`app/api/v1/admin.py:636-673`):

| Görülen | Katman | Bakılacak yer |
|---|---|---|
| `production.generated == 0`, `last_error` dolu | üretim bozuk | `app/services/recaptcha/browser.py:152` `_produce_token()` |
| `generated > 0`, `rejected > 0` | üretim çalışıyor, token reddediliyor | §5.4 |
| `provider == "noop"` | alan hiç gönderilmiyor | `app/schemas/upstream.py:43` |

Site key kaynağı: `app/upstream/curl_import.py:452-457` (`_RECAPTCHA_SITE_KEY_RE`,
satır 155). Sabit blueprint değeri: `render.yaml:37`.

### 5.4 Hangi hata kodu, hangi katman
```
app/core/diagnostics.py:31  _HINTS   (17 kod)
app/core/diagnostics.py:194 explain_failure()
app/core/diagnostics.py:207 format_failure()
```

| Kod | Dosya:satır | İlk bakılacak |
|---|---|---|
| `upstream_refresh_required` | `diagnostics.py:32` | `client.py:626` `_refresh_marker()` |
| `upstream_quota_reached` | `diagnostics.py:42` | `client.py:613` `_is_quota_body()` |
| `recaptcha_unavailable` | `diagnostics.py:50` | §5.3 |
| `recaptcha_rejected` | `diagnostics.py:59` | `client.py:596` `_is_captcha_body()`, `CAPTCHA_MARKERS` (satır 56) |
| `upstream_unauthorized` | `diagnostics.py:67` | `client.py:668` `_is_auth_body()` |
| `upstream_not_found` | `diagnostics.py:75` | yol: `curl_import.py:474` `looks_like_stream_path()` |
| `upstream_redirect` | `diagnostics.py:83` | `TARGET_DOMAIN` |
| `upstream_unavailable` | `diagnostics.py:91` | `client.py:49` `RETRYABLE_STATUS` |
| `upstream_timeout` | `diagnostics.py:98` | `config.py` timeout ayarları |
| `wrapper_unreachable` | `diagnostics.py:105` | ağ / Render |
| `upstream_failure` | `diagnostics.py:113` | genel |
| `invalid_api_key` / `missing_api_key` | `diagnostics.py:117,125` | `API_KEYS` |
| `rate_limit_exceeded` | `diagnostics.py:130` | kota |
| `model_not_found` | `diagnostics.py:137` | `config/models.yaml` |
| `payload_too_large` | `diagnostics.py:142` | `config.py:318` `max_prompt_chars` |
| `internal_error` | `diagnostics.py:146` | log |

### 5.5 cURL doğru ayrıştırılıyor mu
```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from app.upstream.curl_import import parse_curl, build_settings
env, warns, model_id = build_settings(parse_curl(Path("/tmp/req.txt").read_text()))
for k in sorted(env): print(f"{k:28} = {str(env[k])[:80]}")
print("model_id:", model_id)
for w in warns: print("UYARI:", w)
PY
```
Zincir: `curl_runtime.py:131` → `:70` → `curl_import.py:245` → `:81`.
Alan eşlemesi: `curl_runtime.py:20` `_CURL_FIELDS`.

### 5.6 Log'dan kanıt
```bash
curl -sS -H "Authorization: Bearer $API_KEY" "$WRAPPER_URL/admin/logs" 2>/dev/null | head
```
Olay adları: `upstream_request`, `upstream_http_error`, `upstream_refresh_required`,
`recaptcha_rejected_retrying`, `upstream_auth_refreshed_retrying`,
`deployment_stamp_unchanged`. Sözlük: `docs/RENDER.md` §8 (satır 273).

---

## 6. Düzenleme yapma kuralları

1. **Düzeltmeden önce kırmızı test yaz.** Mevcut hatalı davranışı kodlayan test
   olabilir; o zaman test **güncellenir**, düzeltme geri alınmaz.
   Örnek geçmiş: `tests/test_curl_to_env.py` (boş captcha beklentisi).
2. **Tek katman düzelt.** Düzeltme yapılan katmanı `git diff` ile göster.
3. **Eski test kırılırsa** nedenini yaz; sessizce silme.
4. **Commit mesajı** ne + neden içersin; "ne yapıldı" yetmez.
5. **Dal:** yalnız `arena/cd32810d-apiwrapper`. `main` dokunulmaz.
6. **Push:** `git push origin arena/cd32810d-apiwrapper`.
7. **PR:** `gh pr create --base arena/c55e601b-apiwrapper --head arena/cd32810d-apiwrapper`
8. **Merge:** yalnız kullanıcı o turda açıkça onaylarsa (`docs/MERGE_TARGET.md`).
   Merge = Render deploy.
9. **Sahte doğrulama yok.** "Test geçti" demek için testi çalıştır.
10. **Sırlar:** token/çerez/cURL ham değeri commit edilmez, loglanmaz, rapora yazılmaz.

---

## 7. Sık düşülen tuzaklar

| Tuzak | Doğrusu |
|---|---|
| `.venv` var sanmak | her oturumda `python3 -m venv .venv && pip install -e ".[dev]"` |
| `/tmp` betiğine güvenmek | oturumlar arası silinir, yeniden yaz |
| `/tmp` betiğinde import hatası | `PYTHONPATH=/home/user/ApiWrapper` gerekir |
| `git stash pop` | `git checkout stash@{0} -- .` + `git show "stash@{0}^3:<path>"` |
| `gh pr view --json merged` | geçersiz; `state` / `mergeCommit` / `mergedAt` kullan |
| Yeni dal `main` tabanında | `git reset --hard origin/arena/c55e601b-apiwrapper` |
| `render.yaml`'ı canlı ayar sanmak | sadece blueprint; gerçek değer Render panosunda |
| `_is_captcha_body`'yi "forbidden" ile test etmek | `CAPTCHA_MARKERS` (`client.py:56`) kullan |
| Belgeye güvenip kodu okumamak | `cx` örneği: belge "gönderilmiyor" der, kod `upstream_cx_send` ile koşullu |

---

## 8. Açık maddeler (konumlarıyla)

| # | Konu | Konum | Durum |
|---|---|---|---|
| 1 | ~~`recaptcha_provider` kalıcı alanlar listesinde değil~~ cURL yüklemesi `static`'e döndürüyordu | `app/upstream/state_store.py:67` | **düzeltildi** (PERSISTED_FIELDS + snapshot_fields içine eklendi; restart'ta seçilen sağlayıcı korunuyor) |
| 2 | CAPTCHA token cache TTL 100 sn — **ölçüm: token istek başına tek ve farklı** (`docs/CAPTURE_TIMELINE.md` §5) | `app/core/config.py:324`, `app/services/recaptcha/base.py:51` | bilerek korunuyor; düzeltme kapsam dışı (§5) |
| 3 | ~~`cx` varsayılan gönderilmiyor~~ | `app/core/config.py:89` | **düzeltildi** (cURL gövdesinde `cx` varsa `UPSTREAM_CX_SEND` otomatik `true`; açıkça ayarlanmadıysa eski varsayılan korunur) |
| 4 | `render.yaml` site key blueprint değeri (canlı değer Render panosunda farklı olabilir) | `render.yaml:37` | bilerek korunuyor (render.yaml sadece blueprint) |
| 5 | ~~URL-encoded JSON çerez biçimi (`unquote` yok)~~ | `app/upstream/auth.py:122` | **düzeltildi** (`parse_cookie_header` percent-encoded değerleri tek seviye çözüyor) |
| 6 | ~~Görseller yalnız son kullanıcı mesajından toplanıyor~~ çok turlu sohbette eski görsel kayboluyor | `app/adapters/prompt_builder.py:151` `collect_attachments()` | **düzeltildi** (tüm kullanıcı mesajlarından topluyor, aynı URL'yi deduplike ediyor) |
| 7 | ~~Ek `name` alanı gerçek dosya adı değil (`attachment-N`)~~ | `app/schemas/openai.py:79` `attachments()` | **düzeltildi** (URL son segmenti / data URI content-type'ından gerçek ad türetiliyor) |

---

## 9. Açık PR

| PR | Başlık | Durum |
|---|---|---|
| #28 | Stop reporting generic 403s as CAPTCHA rejections | açık, MERGEABLE, CI 3/3 pass |

Kapsam: `app/upstream/client.py` (`CAPTCHA_MARKERS`, `_is_captcha_body`),
`app/schemas/upstream.py` (boş token alanı), `app/services/recaptcha/static.py`
(noop red saymaz), `app/core/config.py` (`upstream_cx_send`),
`app/adapters/openai_to_upstream.py`, `docs/RENDER.md`, `docs/TROUBLESHOOTING.md`,
testler. Ayrıntı: `docs/HANDOFF.md` §13.

---

## 10. Bitirme kontrol listesi

- [ ] `.venv/bin/pytest -q` → 574 passed
- [ ] `.venv/bin/ruff check app tests` → temiz
- [ ] `.venv/bin/mypy app` → temiz
- [ ] `git diff --check` → çıktı yok
- [ ] `docs/HANDOFF.md` satır numaraları güncel
- [ ] commit + push yapıldı
- [ ] PR hedefi `arena/c55e601b-apiwrapper`
- [ ] merge için kullanıcı onayı alındı
