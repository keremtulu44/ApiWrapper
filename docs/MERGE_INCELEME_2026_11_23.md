# Merge öncesi inceleme — `arena/a3b1ec5d-apiwrapper` → `arena/1kasim-test`

**Durum: MERGE EDİLDİ (23 Kasım 2026).** Önce "incele ama merge etme asla"
talimatıyla inceleme yapıldı; ardından kullanıcı "Birleştir merge et şimdi hemen"
dedi ve PR #64 `arena/1kasim-test` dalına birleştirildi → merge commit `e2d3f53`.
Aşağıdaki bulgular o merge'ün öncesinde düzeltildi.

Tarih: 23 Kasım 2026 · İnceleme ucu: `70bc7cb` (+ bu belgeyle gelen düzeltmeler)
Hedef dal ucu: `fc9ed55` (`arena/1kasim-test`, PR #63 merge'i)

---

## 1. Birleşebilirlik (salt okuma ölçüm)

| Kontrol | Komut | Sonuç |
|---|---|---|
| Dallanma noktası | `git merge-base` | `fc9ed55` — hedef ilerlememiş |
| Bizim taraf | `git log --oneline fc9ed55..HEAD` | **9 commit** |
| Fark | `git diff --stat` | **26 dosya, +5062 / −233** |
| Çakışma | `git merge-tree --write-tree fc9ed55 HEAD` | **çakışma yok** (temiz ağaç) |
| Altyapı dosyaları | `Dockerfile`, `Dockerfile.telegram`, `render.yaml`, `requirements.lock`, `.github/`, `pyproject.toml` | **değişmedi** |

Yani merge, imajı ve CI'yı değiştirmez: aynı taban imaj özeti, aynı 36 kilitli
paket, aynı `render.yaml`. Yeni kod yalnız `app/` + `tests/` + `docs/` içinde.

## 2. CI kapıları (yerelde aynı sürümlerle koşuldu)

| Kapı | Komut | Sonuç |
|---|---|---|
| Lint | `ruff check app tests scripts` | ✅ temiz |
| Tip | `mypy app` (1.20.2, `python_version=3.10`) | ✅ 79 dosya, 0 hata |
| Test | `pytest -q` | ✅ **909 passed** |
| 3.10 sözdizimi | `ast.parse(feature_version=(3,10))` | ✅ temiz |

> **Merge öncesi bulunan iki CI kırmızısı düzeltildi:** `mypy` 3 hata veriyordu
> (`app/core/config.py:455` `int(object)`, `app/telegram/bot.py:104/107`
> `object` üzerinde `__iter__`). CI'daki `mypy app` adımı bunları görüp işi
> kıracaktı. Davranış değişmedi, yalnız tip daraltıldı.

## 3. Çalışma zamanı doğrulaması (ağa çıkmadan)

| Senaryo | Sonuç |
|---|---|
| Bugünkü canlıya yakın: **tek hesap**, rol/rotasyon **kapalı** | `/healthz` 200 · `/readyz` 200 · `slot_roles.enabled=false` → davranış eskisiyle aynı |
| Hedef: **3 hesap** `1:max,2:max,3:agent` | `multi_lane=true`, aktif yuvalar `[1,2,3]`, yuva 3 `usable=false` (Agent trafiği almıyor) |
| `/rol` ucu (hesap yokken) | 400 + "Önce /upstream_curl ile cURL ekleyin" (500 değil) |
| Anormal env (`ACCOUNT_ROTATION_STRICT_SLOTS="abc,2"`) | `[2]` — bozuk değer rotasyonu öldürmüyor |

> **Deploy öncesi bulunan gerçek hata düzeltildi:** açılışta (`start_initial_keepalives`)
> rol süzgeci uygulanmıyordu; `agent` yuvası için de keepalive başlıyordu
> (3 görev → günde 72 probe). Düzeltmeden sonra **2 görev / 48 probe**; yedek
> (`spare`) yuvada keepalive bilinçli olarak **açık** kalır.

## 4. Deploy süreci (merge sonrası ne olur)

```
push → CI (lint + mypy + pytest × 3.10/3.11/3.12 + imaj derleme + imaj içinde test)
     → PR açılır (base: arena/1kasim-test)        ← deploy TETİKLEMEZ
     → kullanıcı onayı                            ← MERGE = DEPLOY
     → gh pr merge → Render autoDeploy: arena/1kasim-test
     → docker build (kilit doğrulaması Dockerfile'ın son adımı)
     → /readyz healthcheck
```

Kritik özellik: **merge = restart**. Bu sürümde çerez yalnız bellekte yaşar;
restart'ta env'deki `UPSTREAM_CURL`'e dönülür ve o cURL bayattır
(`docs/RENDER.md`, `docs/ROLLBACK_2026_11_16.md §12.1`). Bu yüzden:

- Merge'ten hemen önce cURL'ler **taze** olmalı (1., 2., varsa 3. yuva).
- Merge + deploy tamamlanınca **ilk gerçek mesaj** 200 dönmeli; dönmezse yeni
  cURL + yeniden deploy.
- Deploy'dan sonra `UPSTREAM_KEEPALIVE_INTERVAL_SECONDS` değiştirilecekse **aynı
  deploy içinde** yapılmalı; sonradan env değişikliği yine restart demektir.

## 5. Merge sonrası 3 hesap için gereken env (Render → Environment)

Zorunlu (yeni):
```env
UPSTREAM_CURL_2=<2. Max hesabının create-evaluation cURL'ü>
ACCOUNT_SLOT_ROLES=1:max,2:max,3:agent
```
İsteğe bağlı (varsayılanlar güvenli):

| Değişken | Varsayılan | Ne zaman değiştirilir |
|---|---|---|
| `ACCOUNT_ROTATION_ENABLED` | `false` | Sıra tabanlı hesap dönüşümü istenirse `true` |
| `ACCOUNT_ROTATION_MODE` | `round_robin` | `weighted` / `sticky` / `least_loaded` |
| `ACCOUNT_FAILOVER` | `true` | Katı 50/50 bölüşüm istenirse `false` |
| `TARGET_DOMAIN_2` | cURL'den okunur | Elle vermek gerekmez |
| `AUTOPILOT_STATE_DIR` | boş (bellek) | Kalıcı disk bağlandıysa yol verilir; **yazılamıyorsa `/readyz` 503** |

Üç hesap da **açık kalır**; hiçbir cURL silinmez. Rol yalnız iş bölümünü
değiştirir (`/rol 3 agent` ↔ `/rol 3 max`, anında, deploy'suz).

## 6. Merge öncesi operatör kontrol listesi

- [ ] 1. hesap cURL'ü taze mi? (`/ping` → `✓`)
- [ ] 2. hesap cURL'ü hazır mı? (`python scripts/put_curl2.py curl2.txt`)
- [ ] 3. hesap (Agent) cURL'ü geçerli mi? (`/agent_test`)
- [ ] Render'da `ACCOUNT_SLOT_ROLES` yazılacak mı — yoksa üç yuva da `max` olur
      (Agent projesi ile aynı hesap çakışmasın diye **önerilir**)
- [ ] `UPSTREAM_KEEPALIVE_INTERVAL_SECONDS` değeri kararlaştırıldı mı
      (0 = kapalı; canlı geçmişte 3600 kullanıldı)
- [ ] Merge onayı verildi mi (kullanıcı o turda açıkça "merge et" dedi mi)

## 7. Geri dönüş (merge sonrası sorun çıkarsa)

1. Render → Events → önceki başarılı deploy → **Rollback**.
2. Ya da `git revert -m 1 <merge-commit>` + push (yeni deploy).
3. Her iki durumda da **restart olur**: cURL'ler tazelenmeli.
4. Rollback sonrası kırmızıyı ayırt etme: `deployment_id_changed` logu platform
   restart'ını, `401 refresh_required` bayat çerezi gösterir.

## 8. Bilinen, merge'ten sonra da geçerli sınırlar

| Konu | Durum |
|---|---|
| 2. cURL canlı doğrulanmadı | Araç sentetik cURL ile test edildi; gerçek hesapla ilk deneme merge sonrası |
| Kalıcılık | Render Free'de disk yok; `AUTOPILOT_STATE_DIR` verilmezse koşular restart'ta kaybolur (artık `/readyz` uyarır) |
| 401/409'da otomatik rol takası | Yok; rotasyon arızalı hesabı atlar, rol değişimi operatör komutunda |
| Free plan | 15 dk hareketsizlikte uyku; dış zamanlayıcı (cron-job.org) uyanık tutuyor |
