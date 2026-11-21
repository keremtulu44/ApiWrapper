# Otopilot (`max` ↔ Agent Mode) & Canlı 2-Hesap Yönetimi Kılavuzu

Bu modül (`app/autopilot/`), canlı `max` sohbet hattına (`app/upstream/*`) hiç dokunmadan:
1. Gündüz 2 ayrı Google hesabıyla (`upstream_curl` + `upstream_curl2`) `max` yük paylaşımı yapmanızı,
2. Akşam `/upstream_curl2 kapat` komutuyla 2. hesabın `max` bağlantısını ve keepalive görevini **kökten kesip** aynı 2. hesabı gece **Agent Mode** (`/agent_curl`) için çakışmasız kullanmanızı,
3. **Durumsuz (stateless) `max` modelini** Baş Yazılım Mimarı gibi konumlandırarak sandbox sahibi Agent Mode oturumuyla uçtan uca yazılım projeleri geliştirmenizi sağlar.

---

## 1. Gündüz Çift `max` ↔ Gece `max` + Agent Akışı (2 Google Hesabı)

Render üzerinde yeniden deploy atmadan (1. hesabın çalışan oturumunu sıfırlamadan) tüm cURL geçişleri doğrudan Telegram'dan canlı yapılır:

1. **Gündüz (`09:00 – 21:50` — Çift `max` Yük Paylaşımı):**
   - 1. ana hesap zaten çalışır; arızalanırsa `/upstream_curl` yazıp cURL'ü bölmeden yapıştırın, parçalar gidince `/bitir` yazın.
   - 2. hesabı gündüz `max`'a eklemek için `/upstream_curl2` yazıp `create-evaluation` cURL'ünü bölmeden doğrudan yapıştırın (Telegram otomatik 3–4 mesaja böler), ardından `/bitir` yazın. İstekler iki hesap arasında 50/50 bölünür.
2. **Akşam Geçişi (`22:00` öncesi — 2. Hesabı Agent'a Hazırlama):**
   - **`/upstream_curl2 kapat`** yazın.
   - Bu komut 2. hesabın HTTP/2 bağlantı havuzunu ve çerez kavanozunu (`aclose()`) kapatır, keepalive görevini durdurur ve bellekteki tüm 2. yuva kimliklerini siler. 1. ana hesap kesintisiz çalışmaya devam eder.
   - Kapatmayı unutup `/agent_curl` yapıştırırsanız bot çakışmayı yakalar ve önce `/upstream_curl2 kapat` yazmanızı hatırlatır.
3. **Gece (`22:00 – 07:00` TR — Agent Mode Proje Geliştirme):**
   - 2. Google hesabından aldığınız Agent `/api/chat/{id}/input` cURL'ünü **`/agent_curl`** ile yapıştırıp **`/bitir`** yazın, ardından proje fikrinizi gönderin.
   - Gece Agent projesi aktifken yanlışlıkla aynı hesabı `/upstream_curl2` olarak açmaya çalışırsanız ters çakışma kilidi devreye girerek iki oturumun birbirini düşürmesini engeller.
4. **Ertesi Sabah (`07:00` sonrası):**
   - Sabah teslim raporunuz gelir; gündüz tekrar çift `max` kullanmak istediğinizde 2. hesaptan taze `create-evaluation` cURL'ü alıp `/upstream_curl2` + `/bitir` ile açabilirsiniz.

> **Not:** Telegram üzerinden yüklenen cURL'ler güvenlik gereği diske yazılmaz, çalışan konteyner belleğinde (RAM) tutulur. Render servisi yeniden başlatılırsa başlangıç ortam değişkenlerine (`UPSTREAM_CURL`) döner.

---

## 2. Telegram Komut Özeti

### Canlı Hesap & cURL Komutları
- `/upstream_curl` + cURL yapıştır + `/bitir` — 1. ana `max` hesabını canlı güncelle
- `/upstream_curl2` + cURL yapıştır + `/bitir` — 2. `max` hesabını canlı ekle/güncelle
- `/upstream_curl2 kapat` — 2. `max` hesabının bağlantısını, çerezlerini ve keepalive görevini kökten kapat
- `/hesaplar` — Yuva 1 ve Yuva 2'nin sağlık, sıra, keepalive ve bağlantı durumunu görüntüle

### Otopilot (Agent Mode) Komutları
- `/agent_curl` (veya `/curl`) + cURL yapıştır + `/bitir` — Parçalı Agent `/input` cURL'ünü birleştir ve doğrula
- `/tur oto` | `/tur <1-25>` — Otomatik tur (varsayılan) veya sabit tur tavanı
- `/gece` | `/simdi [run_id]` — `22:00–07:00` gece modu (varsayılan) veya beklemeden hemen çalıştır
- `/projeler` | `/durum [run_id]` — Tüm projelerin anlık durumu ve Agent linkleri
- `/rapor [run_id]` — Akşam başlangıç planı ve Agent sabah teslim raporunu görüntüle
- `/durdur [run_id]` — Çalışan veya gece sırasındaki projeyi güvenli biçimde durdur
- `/iptal` — Açık cURL/fikir girişini temizle

---

## 3. Komut Satırından (CLI) Kullanım

```bash
python -m app.autopilot inspect --agent-curl-file agent_curl.txt --max-curl-file max_curl.txt
python -m app.autopilot check-agent --agent-curl-file agent_curl.txt
python -m app.autopilot run \
  --agent-curl-file agent_curl.txt \
  --max-url https://SIZIN-SERVISINIZ.onrender.com \
  --max-api-key sk-SIZIN_ANAHTARINIZ \
  --max-endpoint ask \
  --auto-turns \
  --state-file autopilot_state.json \
  --idea "..."
```

---

## 4. HTTP API Uçları (`/v1/autopilot/*`, `Authorization: Bearer <API_KEYS>`)

- `GET  /v1/autopilot/upstream-slots` — Canlı Yuva 1 & Yuva 2 durum özeti (secret içermez)
- `POST /v1/autopilot/upstream-slots/{slot}` — Yuva 1 veya 2 `max` cURL canlı güncelleme
- `POST /v1/autopilot/upstream-slots/2/close` — Yuva 2'yi kökten kapatma
- `POST /v1/autopilot/inspect-curl` — Agent cURL doğrulaması ve yuva bazlı çakışma kontrolü (`conflict_slots`)
- `POST /v1/autopilot/runs` — Yeni otopilot koşusu başlatma (`auto_turns`, `respect_night_window` destekli)
- `GET  /v1/autopilot/runs` — Koşuları listeleme
- `GET  /v1/autopilot/runs/{run_id}` — Detay, hafıza defteri, akşam planı ve sabah teslim raporu
- `POST /v1/autopilot/runs/{run_id}/start-now` — Gece sırasındaki koşuyu derhal uyandırma
- `POST /v1/autopilot/runs/{run_id}/stop` — Güvenli durdurma
