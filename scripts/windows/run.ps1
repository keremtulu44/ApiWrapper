<#
.SYNOPSIS
    ApiWrapper servisini Windows/PowerShell üzerinde başlatır.

.DESCRIPTION
    Sanal ortamı (gerekirse) kurar, bağımlılıkları yükler, .env dosyasını
    kontrol eder ve uvicorn'u başlatır.

.EXAMPLE
    .\scripts\windows\run.ps1
    .\scripts\windows\run.ps1 -Port 8080 -Reload
#>

[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

# Depo köküne geç (bu betik scripts\windows\ altında)
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot
Write-Host "Proje dizini: $RepoRoot" -ForegroundColor DarkGray

# --- Python kontrolu ---------------------------------------------------------
$PythonCmd = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    try {
        $parts = $candidate.Split(" ")
        $version = & $parts[0] $parts[1..($parts.Length - 1)] --version 2>&1
        if ($LASTEXITCODE -eq 0) { $PythonCmd = $candidate; break }
    } catch { continue }
}
if (-not $PythonCmd) {
    Write-Host "Python bulunamadi. https://python.org adresinden kurun." -ForegroundColor Red
    exit 1
}
Write-Host "Python: $PythonCmd" -ForegroundColor DarkGray

# --- Sanal ortam -------------------------------------------------------------
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Sanal ortam olusturuluyor (.venv)..." -ForegroundColor Cyan
    $parts = $PythonCmd.Split(" ")
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "venv olusturulamadi." -ForegroundColor Red; exit 1 }
}

if (-not $SkipInstall) {
    Write-Host "Bagimliliklar yukleniyor..." -ForegroundColor Cyan
    & $VenvPython -m pip install --upgrade pip --quiet
    # dev ekstrasi pytest/respx/ruff/mypy, browser ekstrasi Enterprise CAPTCHA
    # icin Playwright icerir. Tek dogru yer .venv oldugu icin birlikte kurulur.
    & $VenvPython -m pip install -e ".[dev,tokens,browser]" --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "Kurulum basarisiz." -ForegroundColor Red; exit 1 }
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Host "Chromium kurulumu basarisiz." -ForegroundColor Red; exit 1 }
}

# --- .env kontrolu -----------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host ".env olusturuldu. Devam etmeden once doldurun:" -ForegroundColor Yellow
    Write-Host "  TARGET_DOMAIN            = hedef domain (semasiz)" -ForegroundColor Yellow
    Write-Host "  UPSTREAM_COOKIE          = tarayicidan aldiginiz ham Cookie" -ForegroundColor Yellow
    Write-Host "  UPSTREAM_RECAPTCHA_FIELD = recaptchaV3Token" -ForegroundColor Yellow
    Write-Host "  RECAPTCHA_PROVIDER       = browser (site key/example hazir)" -ForegroundColor Yellow
    Write-Host "  config\models.yaml       = Max upstream_id hazir" -ForegroundColor Yellow
    Write-Host ""
    notepad .env
    Read-Host "Duzenlemeyi bitirince Enter'a basin"
}

# --- Baslat ------------------------------------------------------------------
$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $BindHost, "--port", "$Port", "--no-access-log")
if ($Reload) { $uvicornArgs += "--reload" }

Write-Host ""
Write-Host "ApiWrapper baslatiliyor -> http://${BindHost}:${Port}" -ForegroundColor Green
Write-Host "  Swagger : http://${BindHost}:${Port}/docs" -ForegroundColor DarkGray
Write-Host "  Saglik  : http://${BindHost}:${Port}/health" -ForegroundColor DarkGray
Write-Host "  Durdur  : Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

& $VenvPython @uvicornArgs
