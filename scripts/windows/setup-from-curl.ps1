<#
.SYNOPSIS
    Tarayıcıdan kopyalanan cURL komutunu yapıştırarak .env dosyasını otomatik doldurur.

.DESCRIPTION
    Elle kopyala-yapıştır sırasında çerez değerinin ortadan kesilmesi, alt satıra
    taşması veya tırnakların yarım kalması gibi hataları tamamen ortadan kaldırır.
    cURL komutunu olduğu gibi yapıştırırsınız; betik alan adını, çerezi,
    accept-language ve user-agent başlıklarını, captcha alan adını ve modelAId
    değerini ayıklayıp .env ile config\models.yaml dosyalarına yazar. Kurulum
    tek hesaplıdır; browser/external CAPTCHA seçimi varsa statik token kaydetmez.

.PARAMETER FromClipboard
    cURL'ü panodan okur (yapıştırmaya gerek kalmaz).

.PARAMETER Path
    cURL komutunu içeren hazır bir dosyadan okur.

.PARAMETER DryRun
    Dosyaları değiştirmeden yalnızca ne yazılacağını gösterir.

.PARAMETER ShowSecrets
    Çerez ve token değerlerini maskelemeden yazdırır.

.EXAMPLE
    .\scripts\windows\setup-from-curl.ps1
    Yapıştırma ekranı açar; cURL'ü yapıştırıp boş satırda Enter'a basın.

.EXAMPLE
    .\scripts\windows\setup-from-curl.ps1 -FromClipboard
#>
[CmdletBinding()]
param(
    [switch]$FromClipboard,
    [string]$Path,
    [switch]$DryRun,
    [switch]$ShowSecrets
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repoRoot

function Find-Python {
    foreach ($candidate in @('.venv\Scripts\python.exe', '.venv/bin/python')) {
        $full = Join-Path $repoRoot $candidate
        if (Test-Path $full) { return $full }
    }
    foreach ($name in @('py', 'python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            if ($name -eq 'py') { return 'py -3' }
            return $cmd.Source
        }
    }
    throw "Python bulunamadi. Once .\scripts\windows\run.ps1 calistirin."
}

# ---------------------------------------------------------------- cURL girdisi
$curlText = $null

if ($Path) {
    if (-not (Test-Path $Path)) { throw "Dosya bulunamadi: $Path" }
    $curlText = Get-Content -Raw -Path $Path
}
elseif ($FromClipboard) {
    $curlText = Get-Clipboard -Raw
    if (-not $curlText) { throw "Pano bos. Once 'Copy as cURL (bash)' yapin." }
    Write-Host "Pano okundu ($($curlText.Length) karakter)." -ForegroundColor DarkGray
}
else {
    Write-Host ''
    Write-Host '  cURL komutunu yapistirin' -ForegroundColor Cyan
    Write-Host '  ------------------------' -ForegroundColor Cyan
    Write-Host '  Tarayicida F12 > Network > istege sag tik >' -ForegroundColor Gray
    Write-Host '  Copy > Copy as cURL (bash)' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  Yapistirdiktan sonra BOS bir satirda Enter e basin.' -ForegroundColor Yellow
    Write-Host ''

    $lines = New-Object System.Collections.Generic.List[string]
    while ($true) {
        $line = Read-Host
        if ([string]::IsNullOrWhiteSpace($line)) {
            if ($lines.Count -gt 0) { break }
            continue
        }
        $lines.Add($line)
    }
    $curlText = $lines -join "`n"
}

if ([string]::IsNullOrWhiteSpace($curlText)) { throw 'cURL girdisi bos.' }

if ($curlText -notmatch 'curl') {
    Write-Host ''
    Write-Host "UYARI: Girdide 'curl' gecmiyor. Yanlis metin yapistirilmis olabilir." -ForegroundColor Yellow
}

# İlk kurulumda güncel Enterprise/browser ayarlarıyla başla. Var olan .env
# otomatik olarak ezilmez; eski dosyayı sıfırlamak kullanıcının açık kararıdır.
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
}

# Gecici dosyaya UTF-8 (BOM'suz) yaz: Python tarafi bozulmadan okusun.
$tempFile = Join-Path ([System.IO.Path]::GetTempPath()) "curl_$([guid]::NewGuid().ToString('N')).txt"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tempFile, $curlText, $utf8NoBom)

try {
    $python = Find-Python
    # Windows kurulumu tek hesaplıdır: her içe aktarma eski 2/3/4/5 yuva
    # kalıntılarını temizler ve güncel kimliği yalnız 1. hesaba yazar.
    $arguments = @('scripts/curl_to_env.py', $tempFile, '--single-account')
    if (-not $DryRun)   { $arguments += '--write' }
    if ($ShowSecrets)   { $arguments += '--show-secrets' }

    Write-Host ''
    if ($python -eq 'py -3') {
        & py -3 @arguments
    }
    else {
        & $python @arguments
    }
    $code = $LASTEXITCODE
}
finally {
    Remove-Item $tempFile -ErrorAction SilentlyContinue
}

if ($code -eq 3) {
    Write-Host ''
    Write-Host 'YANLIS ISTEK: kopyalanan cURL bir telemetri/analitik cagrisina ait.' -ForegroundColor Red
    Write-Host 'Network sekmesinde arama kutusuna su metni yazip filtreleyin:' -ForegroundColor Yellow
    Write-Host '    create-evaluation' -ForegroundColor White
    Write-Host 'Cikan istege sag tik > Copy > Copy as cURL (bash) yapip tekrar deneyin.' -ForegroundColor Yellow
    exit $code
}

if ($code -ne 0) {
    Write-Host ''
    Write-Host 'Ayarlar cikarilamadi. cURL i yeniden kopyalayip deneyin.' -ForegroundColor Red
    exit $code
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'Deneme modu: hicbir dosya degistirilmedi.' -ForegroundColor DarkGray
    exit 0
}

Write-Host ''
Write-Host 'Siradaki adimlar:' -ForegroundColor Cyan
Write-Host '  1) Servisi baslatin :  .\scripts\windows\run.ps1'
Write-Host '  2) Dogrulayin       :  .\scripts\windows\chat.ps1 -Diagnose'
Write-Host '  3) Mesaj atin       :  .\scripts\windows\chat.ps1 "Merhaba"'
Write-Host ''
Write-Host 'Not: Kurulum tek hesaplıdır; browser CAPTCHA tokenı her istekte üretir.' -ForegroundColor DarkGray
Write-Host '     Cookie süresi dolarsa aynı komut yalnız 1. hesabı tazeler.' -ForegroundColor DarkGray
