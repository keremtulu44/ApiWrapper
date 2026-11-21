# Otopilot (`max` ↔ Agent Mode) Kullanım Kılavuzu

Bu modül (`app/autopilot/`), canlı `max` sohbet hattına (`app/upstream/*`) hiç dokunmadan, **durumsuz (stateless) `max` modelini** bir Ürün Kurucusu / Baş Yazılım Mimarı gibi konumlandırarak hedef sitedeki **sandbox sahibi Agent Mode** oturumuyla uçtan uca çalışan, yaratıcı web/yazılım projeleri geliştirmesini sağlar.

---

## 1. Altın Kurallar

1. **İki ayrı Google hesabı zorunludur:** `max` için kullanılan cURL (`UPSTREAM_CURL`..`UPSTREAM_CURL_4`) ile Agent Mode için alınan cURL **farklı Google hesaplarından** olmalıdır. Sistem JWT `sub` kimliklerini karşılaştırır ve aynı hesap algılanırsa çalışmayı reddeder.
2. **Hangi Agent cURL'ü kopyalanmalı?**
   - Hedef sitede (Agent hesabıyla) yeni bir **Agent Mode** sohbeti açın.
   - Kısa bir başlangıç mesajı gönderin (ör. `"Merhaba, yeni bir proje üzerinde çalışacağız, hazır olduğunda bildir."`) ki sandbox konteyneri ve sohbet kimliği oluşsun.
   - Yanıt geldikten sonra F12 → **Network** sekmesini açıp ikinci bir kısa mesaj (ör. `"Hazır"`) gönderin ve **`POST /api/chat/{chat_id}/input`** isteğine sağ tıklayıp **Copy as cURL (cmd)** veya **Copy as cURL (bash)** seçin.
   - Tarayıcı sekmesini kapatabilirsiniz; tüm iş sunucu tarafındaki sandbox ve `/events` akışı üzerinden yürür.

---

## 2. Telegram Üzerinden Kullanım (Model A — Önerilen Üretim Akışı)

Render Web Service içindeki gömülü (veya ayrı) Telegram botu üzerinden sunucuyu yeniden başlatmadan proje başlatabilirsiniz:

1. **`/curl`** yazın ve kopyaladığınız Agent `/input` cURL'ünü yapıştırın *(Telegram 4096 karakter sınırı nedeniyle 3–4 mesaja bölse bile tüm parçalar sırayla toplanır)*.
2. Tüm parçalar gidince **`/curl bitir`** (veya kısaca **`/bitir`**) yazın — cURL birleştirilir, doğrulanır ve hesap ayrılığı kontrol edilir.
3. Doğrudan **yaptırmak istediğiniz proje fikrini** yazıp gönderin.

### Otomatik Gece Döngüsü (`22:00 – 07:00` TR), Akşam Planı ve Sabah Raporu
- **Gece Penceresi (Varsayılan):** Gündüz `max` aktif kullanıldığı için bıraktığınız projeler `22:00`'ye kadar `max`'a tek bir istek bile atmadan sırada bekler; `22:00 – 07:00` (TR) arasında sırayla (biri bitince diğeri) yürütülür.
- **Otomatik Tur Planlaması (`<HEDEF_TUR>`):** Tur sayısını elle girmeniz gerekmez; `max` 1. turda projenin kapsamına göre ideal tur hedefini (ör. 4–5, 6–8 veya 9–12 tur) kendi belirler ve erken bitirip kaçmayı önleyen olgunluk kapısı o projeye göre kurulur.
- **🌆 Akşam Başlangıç Planı:** Saat `22:00` olup geliştirme başlarken `max` ürün vizyonunu, yaratıcı imza özellikleri ve yol haritasını çıkarıp Telegram'dan size gönderir.
- **🌅 Sabah Teslim Raporu:** Geliştirme turları bittiğinde doğrudan **Agent** (*"Neler Yapıldı · Canlı Önizleme · Senin Yapman Gerekenler"*) teslim raporunu hazırlar; gece bittiyse sabah saat **`07:00` (TR)** civarında Telegram'dan size iletilir.

### Telegram Komut Özeti
- `/curl` · `/bitir` — Parçalı Agent cURL toplama ve birleştirme
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

- `POST /v1/autopilot/inspect-curl` — Agent cURL doğrulaması ve hesap ayrılığı kontrolü
- `POST /v1/autopilot/runs` — Yeni otopilot koşusu başlatma (`auto_turns`, `respect_night_window` destekli)
- `GET  /v1/autopilot/runs` — Koşuları listeleme
- `GET  /v1/autopilot/runs/{run_id}` — Detay, hafıza defteri, akşam planı ve sabah teslim raporu
- `POST /v1/autopilot/runs/{run_id}/start-now` — Gece sırasındaki koşuyu derhal uyandırma
- `POST /v1/autopilot/runs/{run_id}/stop` — Güvenli durdurma
