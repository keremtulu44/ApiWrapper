# Merge hedefi (tek kural)

> **Tüm çalışmalar `arena/c55e601b-apiwrapper` dalına merge edilir.**
> `main` **dokunulmaz**.

Bu dosya, oturumlar arasında kaybolmasın diye depoda tutulur. Render'daki canlı
servis bu dalı takip eder; `main` yalnızca kullanıcı açıkça isterse güncellenir.

## Kurallar

| Kural | Ayrıntı |
|---|---|
| **Merge hedefi** | `arena/c55e601b-apiwrapper` — istisnasız |
| **`main`** | Dokunulmaz. Merge/deploy hedefi değildir. |
| **Çalışma dalı** | Oturumun kendi dalı (`arena/<oturum>-apiwrapper`); commit + push yalnız oraya |
| **PR akışı** | Oturum dalı → `arena/c55e601b-apiwrapper` (base değiştirilir, sonra `gh pr merge`) |
| **Deploy** | Render `arena/c55e601b-apiwrapper` dalını otomatik deploy eder |

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

Yeni bir merge yapıldığında bu tabloya bir satır ekleyin.

## Komutlar

```bash
# PR'ı doğru hedefe aç
gh pr create --base arena/c55e601b-apiwrapper --head <oturum-dalı> \
  --title "..." --body "..."

# Hedefi sonradan değiştirmek gerekirse (gh pr edit proje alanları yüzünden
# hata verebilir; API yolu her zaman çalışır)
gh api -X PATCH "repos/<owner>/<repo>/pulls/<no>" -f base="arena/c55e601b-apiwrapper"

# Merge (komut tek başına çalıştırılmalı; son komut olmalı)
gh pr merge <no> --merge
```

Doğrulama:

```bash
git ls-remote --heads origin | grep -E "c55e601b|main"
# c55e601b = son merge commit'i, main = değişmemiş olmalı
```
