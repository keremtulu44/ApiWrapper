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

## 1.5 Süreklilik ilkesi (2026-11 güncellemesi)

Koşu artık **tek bir kötü turda ölmez**:

| Durum | Eski davranış | Yeni davranış |
|---|---|---|
| `TAKILDI` | koşu `failed` | 1 kurtarma turu, sonra `stopped` (`/devam` ile sürer) |
| Ağ/timeout hatası | koşu `failed` | 30 + 60 sn sonra 2 kez yeniden dene; olmazsa `stopped` |
| 401 / kota | koşu `failed` | yeniden denenmez (kilidi uzatır), `stopped` + Telegram bildirimi |
| Tur tavanı | koşu kapanır | otomatik +3 tur; `/extend` ile istenirse daha fazla |
| Süreç restart | koşu bellekten silinir | `AUTOPILOT_STATE_DIR` ile diskten yüklenir, `/devam`a açık |
| Gece penceresi | varsayılan açık (22:00'e kadar bekleme) | varsayılan kapalı; `/gece` ile opt-in |
| Tur aralığı | 15–30 sn × 2 | 0–3 sn (nazik aralık tel üstünde `REQUEST_GAP_*`) |

> **Sohbet sözleşmesi korunur:** `max` her turda **yeni** (durumsuz) sohbet açar; Agent aynı
> sohbet üzerinde ilerler. Bu davranış `prompts.py` ve testlerle kilitlidir.

---

## 2. Telegram Komut Özeti

### Canlı Hesap & cURL Komutları
- `/upstream_curl` + cURL yapıştır + `/bitir` — 1. ana `max` hesabını canlı güncelle
- `/upstream_curl2` + cURL yapıştır + `/bitir` — 2. `max` hesabını canlı ekle/güncelle
- `/upstream_curl3` + cURL yapıştır + `/bitir` — 3. hesabı (yedek / gece Agent hesabı) canlı ekle
- `/upstream_curl2 kapat` — 2. (veya `3 kapat` ile 3.) `max` hesabını kökten serbest bırak
- `/rotasyon [a b]` — İki yuva arasında **hesapları takas et** (varsayılan `1 2`); üç hesabın
  hiçbiri tamamen tükenmesin diye rolleri dönüşümlü tut (`yuva 1` ana, `yuva 2` ikinci, `yuva 3` yedek/Agent)
- `/hesaplar` — Tüm yuvaların sağlık, sıra, keepalive, bağlantı durumu **ve rol görünümü**
  (`roles.max_primary` / `max_secondary` / `max_spare`)

### Otopilot (Agent Mode) Komutları
- `/agent_curl` (veya `/curl`) + cURL yapıştır + `/bitir` — Parçalı Agent `/input` cURL'ünü birleştir, çerez bütünlüğünü ve canlı oturum sağlığını doğrula
- `/agent_test` — Bellekteki doğrulanmış Agent cURL oturumunun canlı (`200 OK` / `401`) durumunu tekrar yokla
- `/tur oto` | `/tur <1-25>` — Otomatik tur (varsayılan) veya sabit tur tavanı
- `/gece` | `/gece kapat` | `/simdi [run_id]` — Gece penceresi artık **opt-in** (varsayılan:
  gönderildiği an başla). `/gece` 22:00–07:00 penceresini açar; `/gece kapat` geri alır.
- `/devam [run_id] [simdi]` — `stopped` / `failed` / `max_turns_reached` koşuyu aynı Agent
  sohbetinde sürdür (tur geçmişi + hafıza korunur)
- `/projeler` | `/durum [run_id]` — Tüm projelerin anlık durumu, hata özeti ve Agent linkleri
- `/rapor [run_id]` — Akşam başlangıç planı ve Agent sabah teslim raporunu görüntüle
- `/durdur [run_id | zorla <run_id> | hepsi]` — Çalışan/bekleyen projeyi güvenli veya anında (`zorla` / `hepsi`) durdur
- `/temizle [hepsi | bitenler | <run_id>]` — Çalışan/bekleyen/hatalı otopilot projelerini durdurup bellekten sil (1. ana `max` hesabına dokunmaz)
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

- `GET  /v1/autopilot/upstream-slots` — Tüm yuvaların (1..4) durumu + rol görünümü (secret içermez)
- `POST /v1/autopilot/upstream-slots/{slot}` — Yuva 1..4 `max` cURL canlı güncelleme
- `POST /v1/autopilot/upstream-slots/{slot}/close` — Yuva 2..4'ü kökten serbest bırakma
  (`1. yuva bırakılamaz`; eski `/2/close` yolu korunur)
- `POST /v1/autopilot/upstream-slots/swap` — `{"slot_a": 1, "slot_b": 3}` iki yuvanın hesabını
  **atomik** takas eder (aynı hesap iki yuvaya düşerse reddedilir)
- `POST /v1/autopilot/inspect-curl` — Agent cURL doğrulaması, çerez parça sağlığı, `verify_live` canlı oturum testi ve yuva bazlı çakışma kontrolü (`conflict_slots`)
- `POST /v1/autopilot/runs` — Yeni otopilot koşusu başlatma (`auto_turns`, `respect_night_window` destekli)
- `GET  /v1/autopilot/runs` — Koşuları listeleme
- `GET  /v1/autopilot/runs/{run_id}` — Detay, hafıza defteri, akşam planı ve sabah teslim raporu
  (süreç yeniden başladıysa kayıt diskten yüklenir; `restored_from_disk: true`)
- `POST /v1/autopilot/runs/{run_id}/start-now` — Gece sırasındaki koşuyu derhal uyandırma
- `POST /v1/autopilot/runs/{run_id}/extend` — `{"extra_turns": 4}` tur tavanını uzat; `max_turns_reached`
  artık koşunun ölümü değil, kaldığı Agent sohbetinde devam eder (ömür boyu tavan: 48 ek tur)
- `POST /v1/autopilot/runs/{run_id}/stop?force=false|true` — Tek koşuyu güvenli veya anında durdurma
- `POST /v1/autopilot/runs/stop-all` — Tüm aktif/bekleyen koşuları topluca durdurma
- `POST /v1/autopilot/runs/clear` — Aktif/bekleyen/bitmiş otopilot koşularını durdurup bellekten temizleme
