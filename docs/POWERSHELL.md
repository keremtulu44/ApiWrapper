# PowerShell: Yerel Test Sayfası

Elinde `deploy` ile aynı ağaç var, PowerShell açık. Sırayla aşağıdakileri çalıştır.
Her blok tek başına çalışır; hiçbiri bir şeyi birleştirmez (merge yok).

---

## 0) Nerede olduğunu gör

```powershell
cd C:\path\to\ApiWrapper
git status -sb
git log --oneline -1
git branch -vv
```

Beklenen: `## <dal>` ve son commit `deploy`/`main` ile aynı. Çalışma ağacın
temizse aşağıdaki 1. adım hiçbir şeyi bozmaz.

---

## 1) Bu değişiklikleri kendi kopyana al (ağacı bozmadan)

En güvenlisi ayrı bir çalışma kopyası (`worktree`): mevcut klasörün olduğu gibi
kalır, yeni klasörde duman testi olan dal açılır.

```powershell
git fetch origin
git worktree add ..\ApiWrapper-smoke origin/arena/b6435933-apiwrapper
cd ..\ApiWrapper-smoke
git log --oneline -3
```

Tek klasörde kalacaksan (mevcut ağacı `deploy` ile eşit tutmak istiyorsan bunu
seçme):

```powershell
git fetch origin arena/b6435933-apiwrapper
git switch -c duman-testi FETCH_HEAD      # `-c` yeni dal: deploy'a dokunmaz
```

Geri almak istersen: `git switch -` (worktree'de: klasörden çıkıp
`git worktree remove ..\ApiWrapper-smoke`).

---

## 2) Kurulum (PowerShell)

```powershell
python --version            # 3.10–3.12 olmalı; `python` yoksa `py --version`
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force   # sadece bu pencere
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

> `Activate.ps1` "bu sistemde betik çalıştırma devre dışı" derse: yukarıdaki
> `Set-ExecutionPolicy` satırı yeterli. Kalıcı yapma, sadece bu pencere.

---

## 3) Sunucuyu aç (1. pencere)

`.env` dosyan zaten `deploy` ile aynıysa dokunma; değilse:

```powershell
Copy-Item env.mobile.example .env -Force
notepad .env        # §1'deki 5 satırı doldur
```

Önce ortamı denetle (anahtar adları + eksikler; gizli değer basmaz):

```powershell
python -m scripts.env_audit --env .env
```

Sunucuyu başlat:

```powershell
$env:PYTHONUTF8 = "1"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Bu pencere açık kalacak. Ready logu görününce 4. adıma geç.

---

## 4) Duman testi (2. pencere)

```powershell
cd C:\path\to\ApiWrapper-smoke
.\.venv\Scripts\Activate.ps1
$env:API_KEY = "KeremTulu44"        # .env içindeki API_KEYS ile aynı

python -m scripts.smoke --base http://127.0.0.1:8000 --key $env:API_KEY --ascii
```

`--ascii` Windows PowerShell'de emoji satırlarını `[OK]/[!]/[X]` yapar; Türkçe
karakterler bozulursa pencerede bir kez:

```powershell
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
```

Ek denemeler:

```powershell
python -m scripts.smoke --base http://127.0.0.1:8000 --key $env:API_KEY --stream --ascii
python -m scripts.smoke --base http://127.0.0.1:8000 --key $env:API_KEY --skip-chat --ascii
python -m scripts.smoke --base http://127.0.0.1:8000 --key $env:API_KEY --json | ConvertFrom-Json
```

Canlıya karşı (yerel sunucu gerekmez):

```powershell
python -m scripts.smoke --base https://SERVIS.onrender.com --key $env:API_KEY --ascii
```

---

## 5) PowerShell'de `curl` / `jq` karşılıkları

PowerShell'de `curl` artık `Invoke-WebRequest`'e takma addır; gerçek cURL için
**`curl.exe`** yaz. `jq` genelde kurulu değildir; yerine `ConvertFrom-Json`.

```powershell
$BASE = "http://127.0.0.1:8000"
$KEY  = $env:API_KEY
$H    = @{ Authorization = "Bearer $KEY" }

# sağlık
Invoke-RestMethod "$BASE/healthz" | ConvertTo-Json -Depth 5
Invoke-RestMethod "$BASE/readyz"  | ConvertTo-Json -Depth 5

# katman teşhisi
(Invoke-RestMethod "$BASE/v1/admin/auth" -Headers $H).verdict | ConvertTo-Json -Depth 8

# ayarlar (gizli değerler maskeli döner)
Invoke-RestMethod "$BASE/v1/admin/config" -Headers $H | ConvertTo-Json -Depth 8

# gerçek sohbet isteği — Türkçe karakter için gövdeyi UTF-8'e çevir
$body = @{ model = "max"; messages = @(@{ role = "user"; content = "ping" }); max_tokens = 8 } | ConvertTo-Json -Depth 5
Invoke-RestMethod "$BASE/v1/chat/completions" -Method Post -Headers $H `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 8
```

Hata gövdesini görmek (4xx/5xx'te `Invoke-RestMethod` istisna atar):

```powershell
function Show-ApiError {
  param([scriptblock]$Call)
  try { & $Call | ConvertTo-Json -Depth 8 }
  catch {
    $msg = $_.ErrorDetails.Message
    if (-not $msg -and $_.Exception.Response) {
      $stream = $_.Exception.Response.GetResponseStream()
      $msg = (New-Object IO.StreamReader($stream)).ReadToEnd()
    }
    if ($msg) { $msg } else { $_.Exception.Message }
  }
}

Show-ApiError { Invoke-RestMethod "$BASE/v1/chat/completions" -Method Post -Headers $H `
  -ContentType "application/json" -Body $body }
```

Gerçek cURL isteyenler için (Windows 10+ içinde gelir):

```powershell
curl.exe -s "$BASE/readyz"
curl.exe -s -H "Authorization: Bearer $KEY" "$BASE/v1/admin/auth"
curl.exe -s -N -H "Authorization: Bearer $KEY" -H "content-type: application/json" `
  -d '{\"model\":\"max\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":true}' `
  "$BASE/v1/chat/completions"
```

> `curl.exe` ile JSON gövdeyi tırnak içinde verirken PowerShell kaçış kuralı
> yüzünden `\"` yazmak gerekir; bu yüzden `Invoke-RestMethod` daha az tuzaklı.

---

## 6) Testler ve kalite kapısı

```powershell
pytest -q
pytest -q tests/test_smoke.py
ruff check app tests scripts
mypy app
```

---

## 7) Sık takılınanlar

| Belirti | Sebep / çözüm |
|---|---|
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` |
| `curl : parametre ... bulunamadı` | `curl` takma adı; `curl.exe` yaz |
| `jq bulunamadı` | `ConvertFrom-Json` kullan |
| Türkçe karakter `?` / bozuk | `chcp 65001`, `$env:PYTHONUTF8="1"`, `--ascii` |
| Emoji satırı `UnicodeEncodeError` | `--ascii` (bu sürümde kod sayfası hatası artık çökmüyor) |
| `[WinError 10048]` port dolu | `--port 8001` ver, smoke'ta da `--base http://127.0.0.1:8001` |
| `401 Incorrect API key` | `.env`'deki `API_KEYS` ile `$env:API_KEY` aynı değil |
| `/v1/admin/*` 401 ama anahtar doğru | `ADMIN_API_KEYS`'teki anahtar `API_KEYS`'te de olmalı |
| `readyz` 503 `target_domain_not_configured` | `.env`'de `TARGET_DOMAIN` boş |
| İlk istek 30–60 sn | Render ücretsiz plan uykusu; smoke `timeout` 60 sn |
