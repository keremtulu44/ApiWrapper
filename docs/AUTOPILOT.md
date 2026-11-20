# Otopilot (`max` ↔ Agent Mode) Kullanım Kılavuzu

Bu modül (`app/autopilot/`), canlı `max` sohbet hattına (`app/upstream/*`) hiç dokunmadan, **durumsuz (stateless) `max` modelini** bir Ürün Sahibi / Baş Mimar gibi konumlandırarak hedef sitedeki **sandbox sahibi Agent Mode** oturumuyla çok turlu bir yazılım/web projesi geliştirmesini sağlar.

---

## 1. Altın Kurallar

1. **İki ayrı Google hesabı zorunludur:** `max` için kullanılan cURL ile Agent Mode için alınan cURL **farklı Google hesaplarından** olmalıdır. Sistem JWT `sub` kimliklerini karşılaştırır ve aynı hesap algılanırsa çalışmayı reddeder.
2. **Hangi Agent cURL'ü kopyalanmalı?**
   - Hedef sitede (Agent hesabıyla) yeni bir **Agent Mode** sohbeti açın.
   - Kısa bir başlangıç mesajı gönderin (ör. `"Merhaba, yeni bir proje üzerinde çalışacağız, hazır olduğunda bildir."`) ki sandbox konteyneri ve sohbet kimliği oluşsun.
   - Yanıt geldikten sonra F12 → **Network** sekmesini açıp ikinci bir kısa mesaj (ör. `"Başlayalım"`) gönderin ve **`POST /api/chat/{chat_id}/input`** isteğine sağ tıklayıp **Copy as cURL (cmd)** veya **Copy as cURL (bash)** seçin.
   - Tarayıcı sekmesini kapatabilirsiniz; tüm iş sunucu tarafındaki sandbox ve `/events` akışı üzerinden yürür.
   - *(Not: `/input` isteği hem oturum çerezini, hem sohbet kimliğini, hem de `recaptchaV3Token` ve `cx` alanlarını tek seferde içerir.)*

---

## 2. Komut Satırından (CLI) Kullanım — En Düşük Riskli Yöntem

Agent cURL'ünü bir metin dosyasına (ör. `agent_curl.txt` — git'e eklemeyin) kaydedin.

### Adım 1: cURL'ü ağa çıkmadan doğrulayın
```bash
python -m app.autopilot inspect --agent-curl-file agent_curl.txt
```
İki cURL dosyanız varsa aynı hesap çakışmasını da anında kontrol edebilirsiniz:
```bash
python -m app.autopilot inspect --agent-curl-file agent_curl.txt --max-curl-file max_curl.txt
```

### Adım 2: Agent oturumunun canlı olduğunu test edin (mesaj göndermez)
```bash
python -m app.autopilot check-agent --agent-curl-file agent_curl.txt
```

### Adım 3: Projeyi başlatın (`run`)

**Seçenek A — Canlı Render (veya yerel) ApiWrapper `max` sunucusu üzerinden (Önerilen Hibrit Mod):**
```bash
python -m app.autopilot run \
  --agent-curl-file agent_curl.txt \
  --max-url https://SIZIN-SERVISINIZ.onrender.com \
  --max-api-key sk-SIZIN_ANAHTARINIZ \
  --max-endpoint ask \
  --max-turns 8 \
  --state-file autopilot_state.json \
  --idea "Abonelikleri, yaklaşan ödemeleri ve kategori bazlı aylık nakit akışını takip eden, SQLite tabanlı, karanlık temalı ve interaktif grafikli tam kapsamlı bir kişisel finans web uygulaması geliştir."
```
*(İpucu: `--max-endpoint ask` seçildiğinde Render arkasındaki uzun düşüncelerde 30 saniyede bir boşluk nabzı gönderen `/v1/ask` ucu kullanılır; böylece vekil sunucu zaman aşımı yaşanmaz.)*

**Seçenek B — Doğrudan ikinci bir `max` cURL dosyasıyla (Sunucusuz):**
```bash
python -m app.autopilot run \
  --agent-curl-file agent_curl.txt \
  --max-curl-file max_curl.txt \
  --max-turns 8 \
  --idea "..."
```

### Adım 4: Kaldığı yerden devam ettirme (`resume`)
Tur bütçesi dolduğunda, oturum çerezi yenilendiğinde veya `Ctrl+C` ile durdurduğunuzda (gizli veri içermeyen) `autopilot_state.json` dosyası korunur:
```bash
python -m app.autopilot resume \
  --state-file autopilot_state.json \
  --agent-curl-file yeni_agent_curl.txt \
  --max-url https://SIZIN-SERVISINIZ.onrender.com \
  --max-api-key sk-SIZIN_ANAHTARINIZ \
  --max-turns 12
```

---

## 3. HTTP API Üzerinden Kullanım (`/v1/autopilot/*`)

Aynı FastAPI servisi içinde doğrudan iç `CompletionService` (`InProcessMaxBridge`) veya dış `external_max_url` ile arka planda da çalıştırılabilir (`Authorization: Bearer <API_KEYS>` gerektirir):

- **`POST /v1/autopilot/inspect-curl`** — Agent cURL'ünü ve sunucudaki `max` hesabıyla çakışıp çakışmadığını doğrular.
- **`POST /v1/autopilot/runs`** — Yeni bir arka plan otopilot koşusu başlatır (`project_idea`, `agent_curl`, `max_turns`).
- **`GET /v1/autopilot/runs`** — Koşuları listeler.
- **`GET /v1/autopilot/runs/{run_id}`** — Güncel aşamayı, hafıza defterini ve tur geçmişini döner.
- **`POST /v1/autopilot/runs/{run_id}/stop`** — Koşuyu güvenli biçimde durdurma sinyali gönderir.
