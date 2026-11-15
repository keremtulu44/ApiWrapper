# Merge hedefi (tek kural)

> **Tüm çalışmalar `arena/1kasim-test` dalına merge edilir.**
> `main` **dokunulmaz**.

Bu dosya, oturumlar arasında kaybolmasın diye depoda tutulur. Render'daki canlı
servis bu dalı takip eder; `main` yalnızca kullanıcı açıkça isterse güncellenir.

**2026-11-15 — hedef değişti.** Kullanıcı deploy dalını `arena/1kasim-test`
olarak belirledi; önceki hedef `arena/c55e601b-apiwrapper` artık merge hedefi
değil. Karışıklık yaşamamak için: bir oturum merge'e başlamadan önce hedefi
buradan okur, tahmin etmez. `arena/1kasim-test` "1 Kasım sürümü" kodunu taşır
(otomatik tarayıcı girişi / `SessionKeeper`); o dala merge etmek aynı zamanda
`main`'in sonraki iki haftalık evrimini de canlıya götürür — bu yüzden
çakışmalar elle çözülüp tam test paketi çalıştırılmadan merge edilmez.

## Kurallar

| Kural | Ayrıntı |
|---|---|
| **Merge hedefi** | `arena/1kasim-test` — istisnasız |
| **`main`** | Dokunulmaz. Merge/deploy hedefi değildir. |
| **Çalışma dalı** | Oturumun kendi dalı (`arena/<oturum>-apiwrapper`); commit + push yalnız oraya |
| **PR akışı** | Oturum dalı → `arena/1kasim-test` (base değiştirilir, sonra `gh pr merge`) |
| **Deploy** | Render `arena/1kasim-test` dalını otomatik deploy eder |
| **Onay** | **Merge = deploy.** Kullanıcı o mesajda açıkça izin vermedikçe merge **yapılmaz** |
| **Eski hedef** | `arena/c55e601b-apiwrapper` (2026-11-15'e kadar); artık kullanılmıyor |

## Onay kuralı (2026-11 — kullanıcı talebi)

Merge, Render'da yeni bir deploy demek. Kullanıcı "dakka başı deploy oluyor,
benden izin al" dedi. Bu yüzden:

1. **Commit + push + PR açmak serbesttir** (deploy tetiklemez).
2. **Merge yalnızca kullanıcı o turda açıkça onaylarsa yapılır** ("merge et",
   "yayına al", "evet" gibi). Onay yoksa dal açık PR olarak bekler.
3. Bir turda birden fazla PR birleştirilmez; onay tek PR içindir.
4. Acil durum istisnası yok: canlıda bir şey bozuksa bile önce kullanıcıya
   "şu düzeltmeyi merge edeyim mi?" diye sorulur.

## Neden

- Render'daki canlı servis bu daldan besleniyor; merge = 2-3 dakika içinde canlı.
- `main` her zaman "yayınlanmış/kararlı" referans olarak kalır.
- Sorun çıkarsa dal bazında geri dönmek kolaydır (`main` kirletilmez).

## Geçmiş (kayıt)

| PR | Kaynak dal | Hedef | Merge commit |
|---|---|---|---|
| #1 | `arena/6c654978-apiwrapper` | `arena/c55e601b-apiwrapper` | `cef7ea9` |
| #2 | `arena/320534c1-apiwrapper` | `arena/c55e601b-apiwrapper` | `4b03c74` |
| #3 | `arena/b47e14e8-apiwrapper` | `arena/c55e601b-apiwrapper` | `ff83b12` |
| #4 | `arena/d1587a5b-apiwrapper` | `arena/c55e601b-apiwrapper` | `b2ba4d1` |
| #5 | `arena/9fb06017-apiwrapper` | `arena/c55e601b-apiwrapper` | `80ac8a8` |
| #6 | `arena/9fb06017-apiwrapper` | `arena/c55e601b-apiwrapper` | `df5e0e0` |
| #7 | `arena/b0820d11-apiwrapper` | `arena/c55e601b-apiwrapper` | `d9ca3f2` |
| #8 | `arena/d5eb0619-apiwrapper` | `arena/c55e601b-apiwrapper` | `6ec6d2f` |
| #9 | `arena/72e4a3e6-apiwrapper` | `arena/c55e601b-apiwrapper` | `8912185` |
| #10 | `arena/2dd37e06-apiwrapper` | `arena/c55e601b-apiwrapper` | `0f1a817` |
| #11 | `arena/1320a4db-apiwrapper` | `arena/c55e601b-apiwrapper` | `b833d68` |
| #12 | `arena/ca07f3d8-apiwrapper` | `arena/c55e601b-apiwrapper` | `93a37c2` |
| #14 | `arena/14b44d27-apiwrapper` | `arena/c55e601b-apiwrapper` | `1402b49` |
| #15 | `arena/ecf279c9-apiwrapper` | `arena/c55e601b-apiwrapper` | *(bu PR)* |
| #21 | `arena/d6beb89f-apiwrapper` | `arena/c55e601b-apiwrapper` | *(bu PR)* |
| #22 | `arena/af86b04b-apiwrapper` | `arena/c55e601b-apiwrapper` | *(bu PR)* |
| #25 | `arena/55a90bd1-apiwrapper` | `arena/c55e601b-apiwrapper` | *(bu PR)* |
| — | `arena/b297a0bb-apiwrapper` | **`arena/1kasim-test`** | *(2026-11-15, yeni hedefe ilk merge; modlar: sohbet/web/görsel)* |

Yeni bir merge yapıldığında bu tabloya bir satır ekleyin.

## Komutlar

```bash
# PR'ı doğru hedefe aç
gh pr create --base arena/1kasim-test --head <oturum-dalı> \
  --title "..." --body "..."

# Hedefi sonradan değiştirmek gerekirse (gh pr edit proje alanları yüzünden
# hata verebilir; API yolu her zaman çalışır)
gh api -X PATCH "repos/<owner>/<repo>/pulls/<no>" -f base="arena/1kasim-test"

# Merge (komut tek başına çalıştırılmalı; son komut olmalı)
gh pr merge <no> --merge
```

Doğrulama:

```bash
git ls-remote --heads origin | grep -E "1kasim-test|main"
# 1kasim-test = son merge commit'i; main yalnız kullanıcı açıkça isterse güncellenir
```

## Bu dala merge ederken (1 Kasım sürümü)

`arena/1kasim-test` `main`'den 1 Kasım'da ayrıldı; o yüzden bir oturum dalını
buraya bağlamak iki yönün birleşimidir ve **çakışma normaldir**. Bilinen
çakışma noktaları ve doğru çözüm:

| Dosya | Neden çakışır | Çözüm |
|---|---|---|
| `app/main.py` | İki taraf da ayrı arka plan görevi ekliyor (`run_token_refresher` / `SessionKeeper`) | **İkisi de kalır**: `alerter` + `refresh_task` + `keeper_task`; `finally` bloğunda üçü de iptal edilir |
| `Dockerfile` | 1 Kasım Chromium için `libatomic1` ekliyor | Paket listesi birleşir (`libatomic1` kalır) |
| `app/core/config.py` | `upstream_refresh_token` iki bölümde de tanımlı | **Tek tanım**: refresh bölümündeki kalır (mypy `no-redef` bunu yakalar) |
| `docs/ENV_FULL.md`, `env.mobile.example` | Yeni ayarlar eklendi | Elle yazma: `python -m scripts.env_template` (doc) ve `--env --out env.mobile.example`; `tests/test_env_template.py` bayatlığı yakalar |

Merge'den sonra **tam test paketi** çalıştırılmadan push edilmez: iki haftalık
kod farkı birleştiği için derleyici/lint temiz görünse bile davranış bozulabilir.
