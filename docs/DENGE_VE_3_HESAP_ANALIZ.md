# Otopilot dayanıklılığı, prompt dengesi ve 3 hesap rotasyonu — analiz raporu

> **Durum:** Yalnızca inceleme. **Hiçbir kod dosyası değiştirilmedi** (dal:
> `arena/a3b1ec5d-apiwrapper`, taban `fc9ed55`). Bu doküman; ölçümleri, bulunan
> sorunları, satır referanslarını ve uygulanabilir önerileri kaydeder.
>
> **Kapsam:** `app/autopilot/*` (≈4.700 satır), `app/services/{lanes,account,keepalive,request_queue}.py`,
> `app/upstream/curl_runtime.py`, `app/api/v1/autopilot.py`, `app/telegram/bot.py`,
> `app/core/config.py`, `tests/test_autopilot.py`, `docs/{AUTOPILOT,TWO_ACCOUNTS,INDEX,LESSONS,RENDER}.md`.
>
> Ölçümler `prompts.py` modülü AST ile yüklenip fonksiyonlar çağrılarak alındı
> (sandbox'ta `fastapi` kurulu olmadığı için tam test paketi koşulmadı; davranış
> değişikliği olmadığı için gerekmedi).

---

## 0. Yönetici özeti

| Konu | Bugün | Sorun | Öneri |
|---|---|---|---|
| Sistemi tek çağrıda gönderilen metin | 5.174 karakter (1. turda 3.593) | Aynı kural 2–3 kez tekrar ediyor; WAF'ı yoran/kota tüketen şişkinlik | Sistem 1.749→**1.231**, tur guardrail 843→**326**, takip guardrail 193→**88** → çağrı başına **−%12**, 1. turda **−%19** |
| Bekleme süreleri | Aynı sayı 3 farklı yerde (orchestrator + API + CLI + bot); tur başına **2 × 15–30 sn** ek bekleme **+** kuyruğun 15–30 sn aralığı | Çifte yavaşlatma; süreklilik "yavaş" değil "kesik" hissettiriyor | zamanlamayı **tek kaynağa** indir; orchestrator aralığını 0–3 sn'ye çek (tel üstü aralık kuyrukta kalsın) |
| Süreklilik (asıl kesinti nedeni) | `TAKILDI` → koşu **anında ölür**; her istisna → `failed`; restart → koşu **bellekten silinir**; `max_turns_reached` → uzatma yok | Modelin tek kötü cevabı 8 saatlik geceyi bitiriyor | kurtarma turu, süre uzatma, kalıcı koşu kaydı, 401'de hesap rotasyonu |
| 3 hesap | `MAX_ACCOUNT_SLOTS = 4` altyapıda **var**; canlı yuva yönetimi **yalnız 1–2** | Rol kavramı yok; `slot 2` onlarca yerde özel-durum | **rol ↔ yuva** ayrımı + atomik takas + `close_slot_2`'nin genelleştirilmesi |

---

## 1. Prompt tarafı: ölçüm ve denge

### 1.1 Bugünkü boyutlar (ölçüldü)

| Parça | Konum | Karakter | Kelime |
|---|---|---|---|
| `MAX_SYSTEM_PROMPT` | `prompts.py:59` | 1.749 | 222 |
| `AGENT_OPERATING_GUARDRAILS` (1. tur) | `prompts.py:21` | 843 | 107 |
| `AGENT_FOLLOWUP_NO_ASK_GUARDRAIL` (2..N) | `prompts.py:37` | 193 | 23 |
| `AGENT_MORNING_HANDOFF_PROMPT` | `prompts.py:44` | 891 | 115 |
| Kickoff kullanıcı istemi | `prompts.py:207` | ≈1.013 | — |
| Tipik tur istemi (kilitli / serbest) | `prompts.py:278` | 3.232 / 3.066 | — |

**Tek bir `max` çağrısında havaya giden toplam (tipik 5. tur):**
`1.749 + 3.232 + 193 = 5.174 karakter`. 12 turluk bir koşuda ≈62 bin karakter
sistem/kural metni; buna ajan yanıtı ve sandbox kanıtı da ekleniyor.

### 1.2 Bulgu A — aynı kural tek çağrıda 3 kez

Ölçüm (`sys + kickoff + turn + g1 + g2` toplamı):

| İfade | Sistem | Kickoff | Tur | G1 | G2 |
|---|---|---|---|---|---|
| `0.0.0.0` | 1 | 0 | 1 | 1 | 0 |
| `ask_user` | 1 | 0 | 0 | 1 | 1 |
| "imza" (özellik) | 1 | 1 | 1 | 0 | 0 |
| "sunucu" | 5 | 1 | 1 | 4 | 0 |
| `TAMAMLANDI` | 2 | 0 | 1 | 0 | 0 |

Yani "0.0.0.0'a bağla", "ask_user açma" ve "imza özellik ekle" talimatları
**her turda 2–3 kez** gidiyor. Durumsuz (stateless) `max` olduğu için kuralların
her çağrıda bir yerde bulunması doğru; ama **birden fazla yerde** bulunması
sadece token ve dikkat israfı.

### 1.3 Bulgu B — ajan guardrail'i, ajan eforunun en kritik turunda kayboluyor

`build_turn_user_prompt`'ta iki ayrı `[KARAR]` bloğu var:

* **Erken bitirme kilidi aktifken** (`prompts.py:321` civarı): "…sıradaki en
  değerli aşamayı yaz" — doğrulama ölçütü **yok**.
* **Serbestken** (`prompts.py:377` civarı): "uçtan uca testler (exit_code=0) ve
  0.0.0.0 canlı sunucu HTTP doğrulaması kanıtla tamsa TAMAMLANDI ver".

Sonuç: ürünün **en çok derinleştirilmesi gereken** turunda `exit_code` ölçütü
tamamen düşüyor (ölçüldü: kilitli tur isteminde `exit_code` **yok**, serbest
turda var). Bu, "erken bitirme kilidi"nin ters etki yapma riski: model kilit
yüzünden Devam diyor ama neyi kanıtlaması gerektiğini bilmiyor.

### 1.4 Bulgu C — belge/kod tutarsızlıkları (küçük ama pahalı)

| # | Yer | Sorun |
|---|---|---|
| 1 | `prompts.py:273` | "**4 aşamalı** yol haritasını yaz" — oysa sistem isteminde ve modül başlığında `prompts.py:5,77` **5 aşama** var. `max`, 4 maddelik hafıza yazıp 5. aşamayı (`5_KABUL`) hiç üretmeyebilir |
| 2 | `prompts.py:269` | `HEDEF_TUR` aralığı `geniş sistem=9-…`: `max_turns=8` iken metin **"9-9"** oluyor; yani tavana eşit/sınırda bir plan isteniyor |
| 3 | `prompts.py:99` + `orchestrator.py:346` | Olgunluk tabanı iki yerde: `compute_min_turns_before_completion` (8→6, 12→8, tavan 8) **ve** `_current_min_turns` içinde `auto_turns` için ek "en az 4" tabanı. Aynı kural iki yerde yaşıyor |
| 4 | `prompts.py:44` | `AGENT_MORNING_HANDOFF_PROMPT` sonuna `AGENT_FOLLOWUP_NO_ASK_GUARDRAIL` ekleniyor; teslim raporu isteminde de aynı yasak metni ikinci kez var |

### 1.5 Önerilen dengeli metin (ölçülmüş kazanç)

Sistem istemi — **1.749 → 1.231 karakter (−%29)**, `tests/test_autopilot.py:510`
çıpalarının **hepsi korunuyor** (`müsait değilim`, `yapay zekayım`, `sandbox`,
`asla`, `web'de`, `web_search`, `api'ye`, `len ≤ 3000`):

```
Sen vizyoner bir Ürün Kurucusu ve Baş Yazılım Mimarısın. Karşında tam yetkili sandbox
(dosya, terminal, arka plan sunucusu, uçtan uca test), web'de canlı arama (web_search,
fetch_page) ve dış API'ye gerçek HTTP isteği atabilen bir Kodlama Ajanı var.
1. Doğal, kararlı bir insan proje sahibi gibi Türkçe konuş; ASLA "yapay zekayım",
   "dil modeliyim", "sandbox'um yok" deme. Deneme istenirse: "Şu an müsait değilim, kendi
   ortamında çalıştırıp test ederek sonucu paylaş."
2. Ajana soru/onay penceresi (ask_user, offer_options) açtırma; kararı sen ver, pencere
   olmadan uygulat.
3. CRUD şablonuna razı olma: karakterli arayüz, gerçekçi Türkçe veri, 2-3 yaratıcı imza
   özellik. İlk iskelette erken bitirip kaçma; SANDBOX KANITI'ndaki gerçek test/sunucu
   sonucuna bak.
4. Canlı veri yakışıyorsa gerçek ucu doğrulat, önbellek/yedek kurdur; verilmeyen ücretli
   gizli anahtarı zorunlu tutma.
Aşamalar: 1_VIZYON_VE_CEKIRDEK -> 2_YARATICI_DERINLIK -> 3_DENEYIM_VE_CILA ->
4_DOGRULAMA_VE_SUNUCU -> 5_KABUL.

YALNIZ BU ETİKETLER:
<DURUM>DEVAM|TAMAMLANDI|TAKILDI</DURUM>
<ASAMA>1..5</ASAMA>
<DEGERLENDIRME>en fazla 2 cümle</DEGERLENDIRME>
<GUNCEL_HAFIZA>en fazla 8 kısa madde</GUNCEL_HAFIZA>
<AJANA_MESAJ>net talimat; TAMAMLANDI ise boş</AJANA_MESAJ>
```

1. tur guardrail'i — **843 → 326 karakter (−%61)** (uzun "sunucu nasıl çalıştırılır"
tarifi tek yerde kalır, tekrar edilmez):

```
Not: ask_user/offer_options/add_voice açma; kararı sen ver, uygula. Derleme, test ve canlı
sunucu doğrulamasını kendi ortamında yap: sunucuyu start_process ile 0.0.0.0'a bağla,
önizleme proxy hostlarına izin ver, tarayıcı kodunda localhost sabitleme (göreli URL),
bash'te ön planda takılan sunucu/etkileşimli CLI çalıştırma.
```

2..N tur guardrail'i — **193 → 88 karakter (−%54)**:

```
Not: ask_user/onay penceresi açma; kararı sen ver, kendi ortamında çalıştırıp doğrula.
```

**Net etki:** tipik `max` çağrısı `5.174 → 4.551` (**−%12**, ≈620 karakter/tur);
1. tur `3.593 → ≈2.900` (**−%19**). 12 turluk koşuda ≈8 bin karakter.
`tests/test_autopilot.py:554` (`len(huge_turn) <= 5500`) ve `:519`
(`len(MAX_SYSTEM_PROMPT) <= 3000`) sözleşmeleri korunur.

**Neden bu kadar kısılabiliyor?** `max` durumsuz: her çağrı kendi kendine yeterli
olmalı, bu yüzden sistem istemini **tamamen** kaldırmak yanlış olur. Denge şu:
*sistem* = değişmez kurallar (kısa), *tur istemi* = yalnızca delta (bütçe, sandbox
kanıtı, son talimat, son yanıt). Bugün aynı delta hem sistemde hem turda tekrar var.

---

## 2. Bekleme süreleri ve "abudik gubidik" sayılar envanteri

### 2.1 Sürekliliği doğrudan kesenler

| Süre | Yer | Değer | Etki |
|---|---|---|---|
| Gece penceresi | `orchestrator.py:356`, bot `_handle_night_mode` (22–07, varsayılan **açık**) | 15 saate kadar bekleme | Gündüz başlatılan koşu 22:00'ye kadar **hiç** ilerlemez — "süreklilik" hissinin en büyük kaynağı |
| `max_cooldown_schedule` | `orchestrator.py:159` | `(310, 600, 900)` sn | 429'da toplam 30 dk bekleyip yine ölebilir |
| `RETRY_FAILING_WITHOUT_PROBE_SECONDS` | `lanes.py:52` | 1800 sn | Probe kapalıyken (`UPSTREAM_KEEPALIVE_INTERVAL_SECONDS=0`, `.env.example:134`) arızalı hesap **30 dk** yeni iş almaz |
| `TAKILDI` | `orchestrator.py:1077`, `:1207` | 0 sn | Tek kötü karar → koşu biter |
| `max_turns_reached` | `orchestrator.py:1226` | — | Uzatma/derinleştirme yok, koşu kapanır |
| Restart | `manager.py` (bellek içi `_runs`) | — | Render Free'de olağan; koşu **kaybolur** (`state_file` yalnız CLI'da: `orchestrator.py:160`) |

### 2.2 Tur başına eklenen beklemeler (çifte yavaşlatma)

```
orchestrator._sleep_gap()  → 15–30 sn      (orchestrator.py:157-158, 523; çağrılar: :1106, :1161, :456)
        +
RequestQueue aralığı       → 15–30 sn      (config.py:276-277; autopilot max çağrısı da bu sıradan geçer:
                                            max_bridge.py:258 Admission(label="autopilot"))
```

Yani `max`'a giden iki ardışık çağrı arasında **hem** orkestratör bekliyor **hem**
kuyruk aralık koyuyor. Tur başına ~2 gap (ajana gönderme öncesi + değerlendirme
öncesi) ⇒ **~45 sn saf bekleme**, üstüne kuyruk aralığı.

### 2.3 Diğer sabitler (tek kaynak yok)

| Sabit | Yer | Değer |
|---|---|---|
| `_BUSY_POLL_SECONDS` / `_BUSY_MAX_WAIT_SECONDS` | `keepalive.py:64,65` | 5 / 600 sn |
| `_RETRY_AFTER_SKIP_SECONDS` | `keepalive.py:73` | 30 sn |
| `KEEPALIVE_MAX_QUEUE_WAIT_SECONDS` | `request_queue.py:57` | 60 sn |
| `_POLL_SECONDS` | `request_queue.py:47` | 1 sn |
| Agent olay penceresi | `agent_client.py:80` | 120 sn (+30) |
| Agent tur zaman aşımı | `agent_client.py:628` | **yok** (0 = sınırsız) — iyi |
| Boşta uyku (Render Free) | `render.yaml`, `RENDER.md` | 15 dk (dış cron uyandırıyor) |

### 2.4 Varsayılanlar dört yerde ve birbirini tutmuyor

| Alan | `orchestrator.py:151-158` | `api/v1/autopilot.py:105-110` | `manager.py:118-128` | `cli.py` | `telegram/bot.py:152` |
|---|---|---|---|---|---|
| `max_turns` | 8 | 8 | 8 | **12** | **12** |
| `gap_min/max` | 15/30 | 15/30 | 15/30 | 15/30 | — |

`tests/test_autopilot.py:1480-1481` bu değerleri **sözleşme** olarak sabitliyor
(gap `15/30`, soğuma `310/600/900`) — yani süre değişikliği bilinçli bir test
güncellemesi gerektirir. Öneri: sayıları tek bir `AutopilotTiming` / `Settings`
kaynağına taşıyıp testleri o kaynağa bağlamak.

### 2.5 Önerilen değerler (süreklilik odaklı, ölçülü)

| Ayar | Bugün | Öneri | Gerekçe |
|---|---|---|---|
| Orkestratör tur aralığı | 15–30 sn | **0–3 sn** (jitter) | Tel üstü aralık zaten `RequestQueue`'da; çifte beklemeyi kaldır |
| Kuyruk aralığı | 15–30 sn | **10–20 sn** | İnsan desenini korur; asıl "nazik" olan bu katman |
| Soğuma | 310/600/900 | **60/180/420/900** | 429 çoğu kez geçici; 5 dk beklemek yerine 1 dk'da yoklamak daha çok tur kazandırır, son adım yine 15 dk sabır |
| `TAKILDI` | koşu biter | **1 kurtarma turu**, 2. ısrarda durur | Tek kötü karar geceyi bitirmesin |
| Probe aralığı (canlı) | `.env.example` **0** | **3600–10800** | 30 dk ceza yerine probe ile geri dönüş |
| Gece penceresi | varsayılan açık | **opt-in** (`/gece` yazılmadıkça kapalı) | Varsayılan davranış "hemen ilerle" olmalı |

---

## 3. Süreklilik: bulunan somut kesinti noktaları

Sırayla, kod referansıyla:

1. **`TAKILDI` anında ölüm** — `orchestrator.py:1077` ve `:1207`. `max`
   "ilerleme yok" dediğinde `status=failed` ve `return`. Kurtarma yolu yok.
   *Öneri:* `TAKILDI` → `DEVAM` dönüşümü + "ajana farklı taktik/sorunu küçült"
   talimatı; yalnız üst üste 2 kez gelirse dur ve **bildir** (koşuyu öldürmeden
   `stopped`a al ki `/devam` ile sürsün).
2. **Fatal `max` hatası** — `orchestrator.py:564` (`401/409/400/…`) → anında
   `raise`. Oturum ölürse gece biter. *Başka `max` hesabı varken* (bkz. §4)
   önce **hesap rotasyonu** denenmeli, tümü tükendiyse durulmalı.
3. **Herhangi bir istisna → `failed`** (`orchestrator.py:1248`) ve yeniden
   deneme yok. Ajan `fetch_state` bir kez aksarsa koşu biter; oysa
   `wait_for_turn_completion` içinde kopma toleransı **var** (`agent_client.py:660-671`).
   Aynı tolerans tur düzeyinde de olmalı.
4. **Restart = koşu kaybı.** `AutopilotManager._runs` bellek içi
   (`manager.py:48-52`); `state_file` yalnız CLI yolunda dolu
   (`orchestrator.py:160`, `cli.py:191`). API/Telegram koşuları hiç
   kalıcılaşmıyor → Render Free'de uyku/restart sonrası `/devam` bile
   "run bulunamadı" der. *Öneri:* `state_file`'ı API yolunda da yaz
   (sır içermiyor: `AutopilotRunState` yalnız fikir/hafıza/tur kayıtları tutar),
   açılışta `stopped` olarak yeniden yükle, `/devam` taze cURL ile sürsün.
5. **`max_turns_reached` uzatılamaz** — `orchestrator.py:1226`. *Öneri:*
   `POST /v1/autopilot/runs/{id}/extend {extra_turns}`; `max_turns` tavanı 25
   olduğu için rahat.
6. **Gece semaforu = 1** (`manager.py:52`) — tek gecede tek proje. Sıra
   bekleyen proje `waiting_window` görünür ama aslında **başka projeyi**
   bekliyordur; durum etiketi yanıltıcı.
7. **Olgunluk kapısı ikinci `max` çağrısı yapıyor** — `orchestrator.py:760` →
   `_ask_max_for_next_step` tekrar çağrılıyor. Her erken `TAMAMLANDI` = 1 ekstra
   tam çağrı + 1 ekstra aralık. Kilitliyken modelin bunu bilmesi (bkz. §1.3)
   bu maliyeti azaltır.
8. **Oturum tazeleme insanı bekliyor.** 401'de belgelerin kuralı net
   (`docs/INDEX.md` kural 2, `LESSONS §19.2`): süren 401 = oturum ölümü, taze
   cURL gerekir. Bu yüzden "tamamen kesintisiz" bir otopilot **mümkün değil**;
   hedef "insan müdahalesini gece başına 1'e indirmek" olmalı. 3 hesap (§4)
   tam bunu sağlar.

---

## 4. Üç hesap: Max-1 + Max-2 + Agent, değişmeli kullanım

### 4.1 Bugün altyapı ne kadar hazır?

| Katman | Durum | Kanıt |
|---|---|---|
| Ayar yuvaları | **Hazır** — `MAX_ACCOUNT_SLOTS = 4` | `config.py:22`; `*_3`, `*_4` alanları `config.py:185-205` |
| cURL → yuva | **Hazır** — 2..4 sonekleri | `curl_runtime.py:152-155` |
| Hesap havuzu / hatlar | **Hazır** — N hesaba genel | `account.py:309`, `lanes.py:120-148` |
| Keepalive | **Hazır** — hat başına | `upstream_slots.py:103-188` |
| Çakışma tespiti (`sub`) | **Hazır** — tüm yuvalar | `curl_parse.py:465-479` |
| **Canlı yuva yönetimi** | **Yalnız 1–2** | `upstream_slots.py:241` (`for slot in (1,2)`), `:321` (`slot not in (1,2)`), `:500` (`close_slot_2`), `:47` (`_SLOT2_PRESERVE_ATTRS`) |
| HTTP API | **Yalnız 1–2** | `api/v1/autopilot.py:425` |
| Telegram | **Yalnız 1–2** | `bot.py:44-45, 560, 625` |
| **Rol kavramı** | **Yok** | Agent, yuva değil: her koşuya yapıştırılan cURL (`manager.py:145`) |

Yani "3 hesap" için **veri düzlemi hazır**, **kontrol düzlemi eksik**.

### 4.2 Değişmez kural: bir hesap = bir istemci

Bugünkü gece akışının tüm mantığı buna dayanıyor: `close_slot_2`
(`upstream_slots.py:500`) 2. hesabın keepalive'ını, hattını, HTTP havuzunu ve
çerez kavanozunu kapatıyor ki **aynı hesap** Agent'ta çakışmasız kullanılabilsin.
İki istemci aynı oturumu taşırsa token rotasyonu birbirini ezer — belgelerde
`pinned_token_shadowing` olarak kayıtlı (`ROLLBACK_2026_11_16.md`).

**Sonuç:** rotasyon "aynı anda 3 istemci" demek **değil**; "roller arasında
taşıma" demek. Her taşımada önce **kaynak roldeki istemci sökülür**, sonra
hedef role bağlanır.

### 4.3 Önerilen model: slot değil **rol**

```
roles = {
    "max_primary"   -> slot 1        (ortak şekli belirler: mode, stream path, modelAId)
    "max_secondary" -> slot 2        (gündüz yük paylaşımı)
    "max_spare"     -> slot 3        (yedek / rotasyonda devreye girer)
    "agent"         -> koşu bazlı    (gece projesi; hiçbir max trafiği almaz)
}
```

* `agent` rolü zaten koşuya bağlı; **kalıcı** olması için cURL'ün parmak iziyle
  (`_identity_fingerprint`, `upstream_slots.py:63`) bellekte tutulması yeterli.
* Kimlik `sub` üzerinden doğrulanır; **aynı `sub` iki rolde olamaz**
  (`_reject_duplicate_accounts`, `curl_runtime.py:124-140` bunu zaten zorluyor
  → takas **tek `Settings` güncellemesinde atomik** yapılmalı, yoksa ara adımda
  "aynı hesap iki yuvada" hatası alınır).

### 4.4 Rotasyon politikası — "hiçbiri tamamen tükenmesin"

Kullanıcı isteği: *"ikisi birincisinin tamamen bitmez tarzında etkiler görüp
düzenlemeler yapacağız"* + *"birinci rol değişmeli şekilde Ajan modunda
kullanılır"*.

Döngü (her gece sonu / sabah teslimden sonra, **insan onaylı**):

```
Gece N    : max_primary=A  max_secondary=B  agent=C
Sabah     : C rapor verir → boşa çıkar
Gece N+1  : max_primary=B  max_secondary=C  agent=A
Gece N+2  : max_primary=C  max_secondary=A  agent=B
```

Kazanç: (a) hiçbir hesap sürekli yüksek hacimli max trafiği taşımıyor,
(b) bir hesabın oturumu ölürse **rotasyonla** yerine başkası geçiyor,
(c) gece projesi hesabı her gece değiştiği için "bir hesap tamamen biterse"
diğer ikisi projeyi sürdürebiliyor.

**Sınır (dürüst):** rotasyon **büyülü değil** — her rol değişiminde o hesabın
cURL'ü gerekiyor. İki seçenek var:

| Seçenek | Nasıl | Bedel |
|---|---|---|
| **A. Elle cURL** | Rolü boşalt (mevcut `close_slot_2` genelleştirilmiş hâli) → o hesabın taze cURL'ünü `/agent_curl` ile yapıştır | Gece başına 1 yapıştırma (bugünkü akışla aynı) |
| **B. Bellekten taşıma** | Gün içinde alınan 3 cURL RAM'de tutulur; rol değişiminde yalnız "hangi cURL hangi rolde" değişir | cURL yaşlanır: JWT ~20 dk'da döner, oturum saatler sürer → **aktivasyonda `verify_live` şart** (`inspect-curl`, `api/v1/autopilot.py:120-160`) |

Öneri: **A varsayılan**, B opsiyonel (`ROTATE_FROM_MEMORY=true`), her ikisinde de
aktivasyon öncesi canlı doğrulama ve "bir hesap = bir istemci" kontrolü.

### 4.5 Uygulama planı (dosya + satır)

| # | Değişiklik | Dosya |
|---|---|---|
| 1 | `status_snapshot` döngüsü `(1,2)` → yapılandırılmış tüm yuvalar | `upstream_slots.py:241` |
| 2 | `update_slot` sınırı `(1,2)` → `1..MAX_ACCOUNT_SLOTS` | `upstream_slots.py:321`, `api/v1/autopilot.py:425` |
| 3 | `_SLOT2_PRESERVE_ATTRS` → `_slot_preserve_attrs(slot)` | `upstream_slots.py:47,366` |
| 4 | `close_slot_2` → `release_slot(slot)` (+ `close_slot_2` uyumluluk sarmalayıcısı) | `upstream_slots.py:500` |
| 5 | **Yeni:** `async def swap_roles(a, b)` — kilit altında, tek `Settings` güncellemesi, iki istemciyi de sök, kimlikleri yeniden doğrula, keepalive'ları uzlaştır, oturum önbelleğini boşalt (`_evict_account_sessions`, `:216`) | `upstream_slots.py` |
| 6 | **Yeni:** `POST /v1/autopilot/upstream-slots/swap` + `GET /v1/autopilot/roles` (sır içermez, yalnız parmak izi) | `api/v1/autopilot.py` |
| 7 | **Yeni:** Telegram `/rotasyon` (önizleme) ve `/rotasyon onayla` | `bot.py:44-56, 560-680` |
| 8 | Bellekte cURL kasası: `dict[fingerprint, raw_curl]`; aktivasyonda `verify_live` | `upstream_slots.py` |
| 9 | Agent tarafı: rol→kimlik eşlemesi zaten var (`manager.py:58`, `upstream_slots.py:382`) → `agent` rolünü de say | `manager.py:58-70` |
| 10 | Belge: `docs/THREE_ACCOUNTS.md` + `AUTOPILOT.md` güncellemesi | `docs/` |

**Test edilecekler** (mevcut test dosyalarına eklenir):
`tests/test_upstream_slots.py` (3. yuva güncelleme, takas atomikliği, aynı kimlik
reddi), yeni `tests/test_role_rotation.py` (döngü politikası, sökme sırası,
"bir hesap bir istemci" değişmezi), `tests/test_autopilot.py` (401'de rotasyon
sonrası devam).

### 4.6 Riskler (belgelerdeki ölçümlerle)

| Risk | Kaynak | Not |
|---|---|---|
| Aynı IP + aynı TLS parmak izi | `TWO_ACCOUNTS.md §9` | Üç hesap, ilişkilendirme yüzeyini büyütür; ölçüm yok |
| Restart üç oturumu da öldürür | `TWO_ACCOUNTS.md §9` | Rotasyon bunu çözmez |
| İki istemci = token yarışı | `ROLLBACK §` (`pinned_token_shadowing`) | §4.2 değişmezi zorunlu |
| Free plan 750 saat/ay, 15 dk uyku | `RENDER.md:48`, `render.yaml` | 3 hesap daha çok probe ⇒ kota/uptime baskısı |
| cURL yaşlanması | `LESSONS §13` (token ~1219 sn'de döner) | Bellekten taşımada `verify_live` şart |

---

## 5. Öneri sırası (etki / efor)

| Sıra | İş | Efor | Etki |
|---|---|---|---|
| 1 | Prompt dengesi + `4 aşamalı`→`5 aşamalı` + `HEDEF_TUR` taşma düzeltmesi + kilitli turda `exit_code` ölçütü | S | Yüksek (her turda tasarruf, daha isabetli karar) |
| 2 | Çifte beklemenin kaldırılması + sürelerin tek kaynağa taşınması | S–M | Yüksek (tur başına ~40 sn) |
| 3 | `TAKILDI` kurtarma turu + `extend` ucu + `stopped`'a düşürme | M | Yüksek (gece yarıda ölmez) |
| 4 | API koşularının kalıcılaşması (`state_file`) + açılışta yükleme | M | Yüksek (restart dayanıklılığı) |
| 5 | 401/409'da hesap rotasyonu (3 hesap veri düzlemi zaten var) | M | Orta–Yüksek |
| 6 | `release_slot` genelleştirmesi + `swap_roles` + API/Telegram komutları | L | Yüksek (istediğin 3'lü düzen) |
| 7 | Probe aralığını canlıda açma (`UPSTREAM_KEEPALIVE_INTERVAL_SECONDS`) | S | Orta (30 dk ceza kalkar) |
| 8 | Gece penceresini opt-in yapma | S | Orta |

> **Not:** 2, 3 ve 5 numaralı maddeler `tests/test_autopilot.py:1480-1481`
> içindeki "süre sözleşmesi" testini bilinçli olarak güncellemeyi gerektirir;
> bu testler zaten bir karar kaydı olduğu için birlikte değiştirilmeli.

---

## 6. Doküman tutarlılığı (bu analizde görülenler)

* `docs/INDEX.md` başındaki tablo pek çok dokümanı "kısmen geçerli/arşiv"
  işaretliyor; `docs/ENV_FULL.md` **bayat** (kendi notu böyle diyor). Bu yüzden
  bu raporda yalnız koda ve `LESSONS §19.1/§20` kurallarına dayanıldı.
* `docs/AUTOPILOT.md` "22:00'de 2. hesabı kapat" akışını anlatıyor; kodda bu
  akış `close_slot_2` ile birebir var, ama **3. hesap** hiçbir yerde yok.
* `docs/TWO_ACCOUNTS.md` "en çok 4 hesap, uçtan uca testler 2 hesapla" diyor —
  3 hesap için ölçüm/doğrulama henüz yok; §4.6'daki riskler bu yüzden açık.

---

## 10. Uygulama sonucu (bu değişiklikle yürürlükte)

Aşağıdaki tablo **artık kodun gerçek davranışıdır**; yukarıdaki bölümler analiz
anındaki (değişiklik öncesi) durumu anlatır.

| Konu | Kod | Yeni davranış |
|---|---|---|
| Tur aralığı (orkestratör) | `orchestrator.py` `TURN_GAP_MIN/MAX_SECONDS` | **0–3 sn**; nazik aralık tel üstünde (`REQUEST_GAP_*`) |
| 429 soğuması | `orchestrator.py` `MAX_COOLDOWN_SCHEDULE` | **(60, 180, 420, 900)** sn — tek kaynak |
| `TAKILDI` | `_recover_from_stuck` | 1 kurtarma turu; yine olmazsa `stopped` (ölmez, `/devam`) |
| Tur tavanı | `_auto_extend_on_turn_limit`, `POST /v1/autopilot/runs/{id}/extend` | tavan dolsa da koşu kapanmaz; otomatik +3, API ile uzatma |
| Geçici Agent hatası | `_send_turn_with_retries` (`MAX_TRANSIENT_TURN_RETRIES=2`) | ağ/timeout/protokol → 30/60 sn sonra yeniden dene; olmazsa `stopped` |
| Geçici **olmayan** hata (401/kota) | aynı yardımcı | yeniden denenmez (kilidi uzatır), `stopped` + Telegram |
| Kalıcılık | `AUTOPILOT_STATE_DIR`, `AutopilotManager._archived` | restart sonrası yarıda kalanlar `stopped` olarak yüklenir, `/devam`a açık |
| Gece penceresi | `respect_night_window` varsayılan `False` | **opt-in**; `/gece` ile açılır, `/gece kapat` ile kapanır |
| 3 hesap | `MAX_ACCOUNT_SLOTS`, `update_slot(1..4)`, `release_slot(N)` | 1./2./3. yuva ayrı ayrı canlı yönetilir |
| Rol rotasyonu | `swap_roles(a, b)`, `POST /v1/autopilot/upstream-slots/swap`, Telegram `/rotasyon` | hesaplar yer değiştirir; aynı hesap iki yuvaya düşmez; takas atomik |
| Prompt boyutu | `prompts.py` | `MAX_SYSTEM_PROMPT` 1672 kr, tipik tur istemi 3265 kr → **çağrı toplamı 5085** (önce 5174) |

### Rol modeli (3 hesap)

```
yuva 1 (max_primary)   → gündüz `max` trafiğinin ana hesabı   (bırakılamaz)
yuva 2 (max_secondary) → ikinci `max` hesabı                  (/upstream_curl2, /rotasyon)
yuva 3 (max_spare)     → yedek + gece Agent Mode hesabı       (/upstream_curl3, /rotasyon, release_slot(3))
```

Rotasyon döngüsü: gün sonunda tükenmeye en yakın hesap yedek yuvaya alınır
(`/rotasyon 1 3`), yedekteki taze hesap ana role geçer; gece Agent projesi
`release_slot(3)` ile Max rollerinden tamamen çıkarılıp Agent'a verilir. Böylece
**hiçbir hesap tamamen tükenmez** ve etkisi `/hesaplar` çıktısındaki
`roles.configured_count` ile görülür.

### Testler

* `tests/test_autopilot.py` → 29 (yeni: geçici hata yeniden denemesi, geçici
  olmayan hatada duraklama, uzatma tavanı, disk gidiş-dönüşü).
* `tests/test_account_rotation.py` → 6 (takas, atomiklik/geri alınabilirlik,
  boş yuva reddi, `release_slot(1)` yasağı, API sözleşmesi, 3. yuva alanları).
* Tam paket: **852 passed**.

---

## 11. "Uzun plan + anlaşılmaz tur" düzeltmesi

Kullanıcı geri bildirimi (23 Kasım): *plan çok uzun çıkıyor, uygulaması ~2 saat
sürüyor; tur akışı dengesiz, ne okuduğu anlaşılmıyor; prompt yapılan işi tekrar
yaptırıyor gibi.*

Ölçüm: kickoff istemi 967 kr, sistem istemi 1672 kr — yani **şişme promptta
değildi**; şişme (a) Max'in ürettiği serbest metin plan/hafızada, (b) turun
kapsamında, (c) görünürlük eksikliğindeydi. Yapılanlar:

| Sorun | Çözüm | Yer |
|---|---|---|
| Plan serbest metin, 6-8 satır | **5 etiketli satır zorunlu**: `AMAC / OZELLIK / MIMARI / ILK_TUR / TUR_SAYISI`; hafıza **en fazla 4 madde**, `- [ ] aşama · kabul ölçütü` | `prompts.build_kickoff_user_prompt` |
| Plan süresi teoride saatler | **tur bütçesi** istemde somut: "bu tur ~60-90 dk; büyükse böl" (`TURN_BUDGET_MINUTES=90`) | `prompts.build_turn_budget_line` |
| Plan mesajı Telegram'da çok uzun | `_compact_plan`: en fazla 6 satır / 620 kr | `orchestrator._compact_plan` |
| "Yapılan işi tekrar yaptırıyor" | Yeni iki bölüm: **[ŞİMDİYE KADAR YAPILDI — TEKRAR İSTEME]** ve **[SIRADAKİ (yalnız bunu iste)]**; hafızadaki `[x]`/`[ ]` işaretlerinden türetilir | `prompts.build_turn_user_prompt`, `orchestrator._next_step_line` |
| Tur sürerken hiçbir şey görünmüyor | Tur başında 3 satır (`▶️ Tur 3 başladı`, `🎯 Bu turun işi`, `⏱️ Hedef`) + **10 dk'da bir** `⏳ Tur 3: 25 dk geçti · ~65 dk kaldı · bash çalıştırıyor` | `orchestrator._turn_start_line`, `_turn_status_line`, `TURN_HEARTBEAT_SECONDS` |
| Tur süresi okunmuyor ("1650 sn") | `/durum` artık **dk** yazar, tur başına "↳ istendi: …" ve "🔬 Son tur kanıtı" ekler | `telegram/bot._show_project_detail` |

Testler: `test_autopilot.py` +5 (defter/sıradaki, bütçe, 5 satırlı plan,
`_compact_plan`, tur başlangıç+heartbeat satırları). Tam paket **857 passed**.

### 11.1 Düzeltme: "sıradaki" rehberdir, süre **sınır değildir**

Kullanıcı itirazı (haklı): *"sıradaki diye plandan gidersek yeni bir şeyi
sınırlamış olmaz mıyız? 65 dk kaldı dediğin süre sınırı değil, değil mi? sistem
sürekli olmalı."*

| Endişe | Durum | Kanıt |
|---|---|---|
| "65 dk kaldı" bir sınır mı? | **Hayır.** Kodda turu kesen hiçbir süre yok | `AgentSessionClient.turn_timeout` varsayılanı `None`; `TURN_BUDGET_*` yalnız metin/rapor |
| Süre aşılırsa ne olur? | Tur **devam eder**; satır "tur uzuyor — sistem bekler, kesmez" der | `orchestrator._turn_status_line` |
| "Sıradaki" planı kilitler mi? | **Hayır** (varsayılan). Rehberdir; gerekçeli sapma serbest | `[SIRADAKİ (rehber — daha iyisi varsa gerekçesiyle değiştir)]` |
| Katı plan istenirse? | `strict_plan=True` ile opt-in; varsayılan kapalı | `prompts.build_turn_user_prompt` |

Yeni fikir koruması: Max, ajan kanıtı ya da projenin gidişatı daha değerli bir
adımı gerektiriyorsa rehberin dışına çıkabilir — yalnız nedenini
`<GUNCEL_HAFIZA>`'ya tek satır yazmak zorunda ("gerekçesiz sapma yasak").

Testler: `test_turn_budget_is_advisory_never_a_timeout`,
`test_next_step_block_is_guidance_not_a_lock`, `test_strict_plan_mode_is_opt_in_only`
eklendi → tam paket **860 passed**.

---

## 12. Hesap rotasyon sistemi (isteğe bağlı, format-nötr)

Kullanıcı isteği: *"2 hesap Max + 1 Agent modu aynı anda aktif olsun; dışarıdan
istek gelince biri siteye biri ajana çalışsın; 40 dk önce ajana verilen metni
1. Max yazdıysa şimdi 2. yazsın — ama **max'ın ve agent'in istek gönderme
formatına asla dokunma**."*

### Ne yapar, ne yapmaz

| | |
|---|---|
| **Değişen** | Yalnız *hangi hesap* sorusu: sıra (`round_robin`), ağırlık (`weighted`) veya eski yük dengesi (`least_loaded`) |
| **DEĞİŞMEYEN** | Upstream istek gövdesi, başlıkları, `create-evaluation` biçimi, SSE akışı, Agent `/input` + `/events` protokolü, yeniden deneme/arıza mantığı |

Format garantisi testle kilitli: `test_rotation_does_not_change_upstream_request_shape`
aynı isteği rotasyon kapalı/açık gönderip başlık+gövdeyi (sohbet kimliği
maskelenerek) **birebir** karşılaştırır.

### Kurallar (öncelik sırası)

1. **pinned** — `x-apiwrapper-account`, keepalive probe'u, elle sınama: rotasyon ezilir.
2. **agent** — Agent işi kendi yuvasını Max tarafında kullanmaz (`ACCOUNT_ROTATION_AGENT_SLOT`).
3. **sticky** — aynı `conversation_id` daha önce bir hesaba düştüyse yine oraya (sohbet ikiye bölünmesin).
4. **strict** — yalnız `ACCOUNT_ROTATION_STRICT_SLOTS` (ör. `1,2`): yedek yuva gündüz trafiğine girmez → *"birinci hesap tamamen bitmesin"*.
5. **round_robin** — **tek global sıra**: 1 → 2 → 1 → 2 (anahtar başına sayaç olsaydı her yeni mesaj hep 1. yuvadan başlar, rotasyon hiç dönmezdi).
6. **aynı mesaj kuralı** — içerik parmak izi (`logical_message_key`, yalnız hash) aynıysa ve sıradaki yuva öncekiyle aynıysa **öbür** yuva seçilir: *"1. Max yazdıysa şimdi 2. yazsın"*.
7. **sağlık/kota** — rotasyon arızalı veya dinlenen hesabı zorlamaz; uygun değilse yük dengesine döner.

### Açma

```env
ACCOUNT_ROTATION_ENABLED=true
ACCOUNT_ROTATION_MODE=round_robin
ACCOUNT_ROTATION_STRICT_SLOTS=1,2      # gündüz Max tarafı
ACCOUNT_ROTATION_EXCLUDED_SLOTS=3      # gece Agent hesabı Max isteği almasın
ACCOUNT_ROTATION_AGENT_SLOT=3
# ACCOUNT_ROTATION_WEIGHTS=1:2,2:1    # mode=weighted ise
```

Görünürlük: `GET /v1/admin/rotation` (kural + **son karar**), `/v1/admin/accounts`
içinde `rotation`, Telegram `/hesaplar` çıkışında `🔁 Rotasyon: açık · sırayla (1→2→1) · son karar: yuva 2 (round_robin)`.

Testler: `tests/test_rotation_engine.py` → 21 test (sıra, aynı-metin kuralı,
strict/excluded/agent yuvası, ağırlık, sticky, pinned, sağlık düşüşü, üç rolün
aynı gün çakışmadan çalışması, format-nötrlüğü). Tam paket **879 passed**.

### 12.1 Düzeltme: hesap **verilmiyor**, rol değişiyor (üç hesap da açık)

Kullanıcı itirazı (haklı): *"nasıl yani hesabı verme artık olmayacak ki, 3 farklı
hesap kullanılacak"*. Önceki anlatımda "serbest bırak / kapat" geçiyordu; bu
yanlıştı. Doğrusu:

| Rol | Gündüz `max` trafiği | Keepalive | Hesap açık mı? | cURL silinir mi? |
|---|---|---|---|---|
| `max` | **alır** (rotasyon üyesi) | açık | ✅ evet | ❌ hayır |
| `spare` | almaz (sıcak yedek) | açık | ✅ evet | ❌ hayır |
| `agent` | **almaz** | **yapılmaz** (o pencere Agent'ın) | ✅ evet | ❌ hayır |

Uygulama: `app/services/slot_roles.py` (`SlotRoles`), `LaneSet.usable()` rolü
`max` olmayan yuvayı gündüz trafiğinden çıkarır ama **hattı/hesabı korur**;
keepalive yalnız `agent` yuvasında durur. Canlı değişim: `/rol <yuva> <rol>`
veya `POST /v1/autopilot/upstream-slots/{slot}/role`. Roller
`ACCOUNT_SLOT_ROLES="1:max,2:max,3:agent"` ile de verilebilir.

Üç hesap senaryosu (hepsi açık):
```
yuva 1  max    → gündüz trafiği (1/2 sırayla)
yuva 2  max    → gündüz trafiği
yuva 3  agent  → gece Agent projesi; max isteği almaz, cURL yerinde
```
Yedek istersen: `/rol 3 yedek` (trafik almaz, sıcak bekler) →
`/rol 3 max` ile tekrar gündüz trafiğine alınır.

Testler: `tests/test_slot_roles.py` → 12 test (rol ayrıştırma, Türkçe eş adlar,
agent yuvasının **açık kalması** ama trafik almaması, yedek yuvanın sıcak
beklemesi, kapanmadan rol takası, boş yuvaya rol verilememesi, uç sözleşmesi).
Tam paket **893 passed**.

---

## 13. Tam denetim (23 Kasım 2026, kapanış)

### 13.1 Geçmişe göre ne yapıldı (5 commit, 4.081 satır)

| Commit | Konu | Kapsam |
|---|---|---|
| `8406c34` | Süreklilik + 3 hesaplı rotasyon + prompt dengeleme | `orchestrator`, `prompts`, `manager`, `upstream_slots` |
| `8a74075` | Turu okunur yap: tekrar yasağı, 90 dk ölçek, canlı ilerleme | `prompts`, `orchestrator`, `bot`, `test_autopilot` |
| `a4af977` | Tur ölçeği **sınır değil**, sıradaki adım **kilit değil** | `prompts`, `orchestrator` + 3 sözleşme testi |
| `9f95b0b` | Hesap rotasyon motoru (format-nötr) | `account_rotation`, `lanes`, `completion_service`, `admin` |
| `30cef34` | Rol sistemi: **üç hesap da açık kalır** | `slot_roles`, `lanes`, `upstream_slots`, `bot`, `api` |

### 13.2 Ölçülen durum (denetim çıktısı)

| Ölçüm | Önce | Şimdi | Not |
|---|---|---|---|
| Test | 842 passed | **896 passed** | +54 yeni test, 0 kırık |
| ruff | temiz | temiz | `app`, `tests`, `scripts` |
| Prompt (kickoff) | 967 kr | 1421 kr | 5 etiketli satır zorunlu → net |
| Tur istemi | 3265 kr | 1677 kr | dengeli dilimler |
| Turu kesen süre | yok | **yok** (`turn_timeout=None`) | testle kilitli |
| Tur aralığı | 15–30 sn ×2 | 0–3 sn | çifte bekleme bitti |
| Gece penceresi | varsayılan açık | opt-in | `/gece`, `/gece kapat` |

### 13.3 Bilinen açık noktalar (dürüst liste)

1. **401/409'da otomatik hesap takası yok.** Rotasyon sıradaki hesabı seçer ve
   arızalı hesabı atlar; ama `swap_roles` çağrısı operatör/telefon komutundadır.
   İstenirse `_is_fatal_max_error` yakalandığında otomatik `/rotasyon` eklenebilir.
2. **Rotasyon 409'u "arıza" sayar.** `failover` açıkken hesap devre dışı kalır ve
   keepalive düzeltene kadar trafik öbür hesaba gider — davranış doğru ama
   operatöre bildirim gitmesi için Telegram uyarısı ayrıca ayarlanmalı.
3. **`state_dir` kalıcı değil.** Render Free'de disk yok; `AUTOPILOT_STATE_DIR`
   yalnız kalıcı disk bağlandıysa anlamlı (belgeye yazıldı).
4. **`ACCOUNT_ROTATION_*` varsayılan kapalı.** Açmadan davranış eskisiyle aynı;
   açmak için `.env`/Render'da 4 satır gerekiyor (bölüm 12).
5. **Agent ile aynı hesabı kullanma** hâlâ `SameAccountError` ile sert durur —
   bilinçli: iki mod aynı oturumu düşürür.

### 13.4 2. Max cURL'ü nasıl konur

Yeni araç: `scripts/put_curl2.py` (tarayıcıdan kopyalanan cURL'ü doğrular,
`.env`'de **yalnız** 2. yuva satırlarını yazar, yedek alır, tam değeri ekrana
basmaz).

```bash
# 1) Tarayıcıda 2. hesapla giriş yap → sohbetten bir mesaj gönder →
#    DevTools → Network → create-evaluation isteği → sağ tık →
#    Copy → Copy as cURL (bash)  (PowerShell/fetch DEĞİL)
# 2) curl2.txt dosyasına yapıştır, sonra:
python scripts/put_curl2.py curl2.txt            # yalnız kontrol
python scripts/put_curl2.py curl2.txt --write    # .env'e yaz (yedek: .env.bak)
```

Araç şunları reddeder: Agent cURL'ü, `modelAId` eksik gövde, çerezi kesilmiş
(`.0`/`.1` parçası boş) oturum, 1. hesapla **aynı** Google hesabı, boş hedef alan
adı. `cf_clearance` yoksa uyarır (403 riski).

Render'da: Environment → `UPSTREAM_CURL_2` alanına cURL'ün tamamını yapıştırın
(tek satır olması gerekmez). Doğrulama: deploy sonrası Telegram'da `/hesaplar` →
`Yuva 2: 🟢 Sağlıklı` ve `🎭 Roller` satırı.


---

## 14. Sistematiğin kapanışı (23 Kasım 2026, ikinci geçiş)

§13'teki "bilinen açık noktalar" listesinin tamamı bu geçişte kapatıldı. Artık
sistematiğin her parçası **tek bir doğru kaynaktan** yönetiliyor: yuva rolü.

### 14.1 Kapatılan açıklar

| Açık ( §13.3 ) | Kapatılış | Kanıt |
|---|---|---|
| Yedek yuva hiç devreye girmiyordu | `promoted_spares()`: iki `max` yuvası da düşerse `spare` **otomatik** trafiğe girer, biri toparlanınca çekilir | `test_spare_takes_over_when_no_max_lane_is_healthy` |
| Rotasyon `spare`/`agent` yuvasını döndürebiliyordu | `choose_slot(..., allowed_slots=...)` — rol tablosu **canlı** süzgeç; `/rol 2 yedek` anında rotasyona yansır | `test_rotation_skips_non_max_roles_live` |
| Sessizce takılan tur | `_heartbeat_tick()` + `SILENT_TURN_NOTICE_SECONDS=1200`: 20 dk olay yoksa "bekliyorum, kesmiyorum" notu (10 dk kalp atışıyla birlikte, spam'siz) | `test_silent_turn_notice_never_cuts_the_turn`, `test_agent_signal_resets_silence_clock` |
| Operatör satırı rolü göstermiyordu | `LaneSet.describe()` → `[yedek·bekliyor]`, `[yedek·ÖNE ALINDI]`, `[agent]`; arıza notuna "bu hesap gündüz trafiğine girmiyordu" satırı | `test_describe_marks_roles_without_leaking_secrets` |
| `/rol` Telegram testi yoktu | 3 test: rol değişimi + hesabın açık kalması, kullanım/wrapper hatası, `/hesaplar` rol+rotasyon çıktısı | `test_rol_command_*`, `test_hesaplar_shows_roles_*` |
| `AUTOPILOT_STATE_DIR` yazılamazsa sessiz kayıp | `/readyz` → `autopilot_state_dir_not_writable` (503, sır içermez) | `test_unwritable_state_dir_is_reported...` |

### 14.2 Tur satırı artık dört ölçülebilir bilgi taşıyor

```
⏳ Tur 3: 42 dk geçti (2/6 bitti · tavan 8) · dosya düzenliyor · Sıradaki: API testlerini yaz
```

Bitmiş iş tekrar istenmez (`_next_step_line` yalnız `[ ]` maddesini seçer), tur
süreyle kesilmez (`turn_timeout=None`), süre aşımı yalnız **bilgi** olarak yazılır.

### 14.3 Üç hesabın varsayılan düzeni

```env
ACCOUNT_SLOT_ROLES=1:max,2:max,3:agent
# iki Max + bir Agent; üç hesap da açık, hiçbiri silinmez
# yedek gerekiyorsa: ACCOUNT_SLOT_ROLES=1:max,2:max,3:spare
# ikisini birlikte kullanmak için 4. yuvayı boş bırakın (MAX_ACCOUNT_SLOTS)
```

| Durum | 1. yuva | 2. yuva | 3. yuva |
|---|---|---|---|
| Gündüz normal | max (trafik) | max (trafik) | agent (Agent projesi) |
| Birincil tükeniyor | max | max | **spare** → öne alınır |
| Agent gecesi | max | max | agent (keepalive yok) |
| Hepsi tükenirse | — | — | istek hata dönmez; sıra bekler, sonra tekrar dener |

### 14.4 Ölçüm

| Ölçüm | §13 sonu | §14 sonu |
|---|---|---|
| Test | 896 passed | **907 passed** |
| ruff (`app`+`tests`+`scripts`) | temiz | temiz |
| Kalan açık nokta | 5 | **0** (davranışsal), 1 operasyonel (2. cURL canlı doğrulaması) |
