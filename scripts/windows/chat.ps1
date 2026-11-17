<#
.SYNOPSIS
    Yerel ApiWrapper servisine sohbet mesaji gonderir (streaming destekli).

.DESCRIPTION
    OpenAI uyumlu /v1/chat/completions ucuna istek atar.
    Varsayilan olarak yaniti canli (streaming) yazdirir.

    NOT: Invoke-RestMethod tum yaniti bekledigi icin streaming'de kullanilamaz;
    bu betik HttpClient ile satir satir okur.

.EXAMPLE
    .\scripts\windows\chat.ps1 "Merhaba, kendini tanit"
    .\scripts\windows\chat.ps1 "Uzun bir hikaye yaz" -Model gpt-4o
    .\scripts\windows\chat.ps1 "Tek seferde cevapla" -NoStream
    .\scripts\windows\chat.ps1 -Interactive
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Message = "Merhaba! Kendini kisaca tanitir misin?",

    [string]$Model = "",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$ApiKey = "sk-local-dev-key",
    [string]$System = "",
    [switch]$NoStream,
    [switch]$Interactive,
    [switch]$Diagnose
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# --- Yardimcilar -------------------------------------------------------------
function Get-DefaultModel {
    try {
        $response = Invoke-RestMethod -Uri "$BaseUrl/v1/models" `
            -Headers @{ Authorization = "Bearer $ApiKey" } -TimeoutSec 15
        if ($response.data.Count -gt 0) { return $response.data[0].id }
    } catch {
        Write-Host "Servise ulasilamadi: $BaseUrl" -ForegroundColor Red
        Write-Host "Once .\scripts\windows\run.ps1 ile servisi baslatin." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "Hic model tanimli degil (config\models.yaml)." -ForegroundColor Red
    exit 1
}

function Build-Body([string]$UserMessage, [array]$History) {
    $messages = @()
    if ($System) { $messages += @{ role = "system"; content = $System } }
    foreach ($item in $History) { $messages += $item }
    $messages += @{ role = "user"; content = $UserMessage }

    $body = @{
        model    = $Model
        messages = $messages
        stream   = (-not $NoStream)
    }
    if (-not $NoStream) { $body.stream_options = @{ include_usage = $true } }
    return ($body | ConvertTo-Json -Depth 10 -Compress)
}

function Invoke-ChatStream([string]$JsonBody) {
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        Add-Type -AssemblyName System.Net.Http
    }

    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromMinutes(10)
    $client.DefaultRequestHeaders.Add("Authorization", "Bearer $ApiKey")
    $client.DefaultRequestHeaders.Add("Accept", "text/event-stream")

    $content = [System.Net.Http.StringContent]::new(
        $JsonBody, [System.Text.Encoding]::UTF8, "application/json")

    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::Post, "$BaseUrl/v1/chat/completions")
    $request.Content = $content

    $full = New-Object System.Text.StringBuilder
    $usage = $null
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $firstTokenMs = $null

    try {
        # ResponseHeadersRead: govde beklenmeden akisa baslanir (streaming icin sart)
        $response = $client.SendAsync($request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()

        if (-not $response.IsSuccessStatusCode) {
            $errorBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            Write-Host "HTTP $([int]$response.StatusCode)" -ForegroundColor Red
            try {
                $parsed = $errorBody | ConvertFrom-Json
                Write-Host $parsed.error.message -ForegroundColor Red
                if ($parsed.error.code) {
                    Write-Host "  kod: $($parsed.error.code)" -ForegroundColor DarkGray
                }
            } catch { Write-Host $errorBody -ForegroundColor Red }
            return $null
        }

        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)

        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if (-not $line.StartsWith("data: ")) { continue }

            $payload = $line.Substring(6).Trim()
            if ($payload -eq "[DONE]") { break }

            try { $chunk = $payload | ConvertFrom-Json } catch { continue }

            if ($chunk.error) {
                Write-Host ""
                Write-Host "Akis hatasi: $($chunk.error.message)" -ForegroundColor Red
                break
            }
            if ($chunk.usage) { $usage = $chunk.usage }
            if ($chunk.choices -and $chunk.choices.Count -gt 0) {
                $piece = $chunk.choices[0].delta.content
                if ($piece) {
                    if (-not $firstTokenMs) { $firstTokenMs = $stopwatch.ElapsedMilliseconds }
                    Write-Host -NoNewline $piece
                    [void]$full.Append($piece)
                }
            }
        }
        $reader.Dispose()
    } finally {
        $client.Dispose()
    }

    $stopwatch.Stop()
    Write-Host ""
    if ($firstTokenMs) {
        $line = "  ilk token: ${firstTokenMs} ms | toplam: $($stopwatch.ElapsedMilliseconds) ms"
        if ($usage) { $line += " | token: $($usage.total_tokens)" }
        Write-Host $line -ForegroundColor DarkGray
    }
    return $full.ToString()
}

function Invoke-ChatBlocking([string]$JsonBody) {
    try {
        $params = @{
            Uri         = "$BaseUrl/v1/chat/completions"
            Method      = "Post"
            Headers     = @{ Authorization = "Bearer $ApiKey" }
            ContentType = "application/json; charset=utf-8"
            Body        = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
            TimeoutSec  = 600
        }
        # PS 5.1 yanit govdesini varsayilan olarak UTF-8 kabul etmez.
        if ($PSVersionTable.PSVersion.Major -lt 6) {
            $raw = Invoke-WebRequest @params
            $bytes = $raw.RawContentStream.ToArray()
            $response = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        } else {
            $response = Invoke-RestMethod @params
        }
    } catch {
        $detail = ""
        if ($_.ErrorDetails.Message) { $detail = $_.ErrorDetails.Message }
        elseif ($_.Exception.Response) {
            $stream = $_.Exception.Response.GetResponseStream()
            $detail = [System.IO.StreamReader]::new($stream).ReadToEnd()
        }
        Write-Host "Istek basarisiz." -ForegroundColor Red
        try {
            $parsed = $detail | ConvertFrom-Json
            Write-Host $parsed.error.message -ForegroundColor Red
            if ($parsed.error.code) {
                Write-Host "  kod: $($parsed.error.code)" -ForegroundColor DarkGray
            }
        } catch { Write-Host $detail -ForegroundColor Red }
        return $null
    }

    $text = $response.choices[0].message.content
    Write-Host $text
    Write-Host ("  token: {0} (giris {1} / cikis {2})" -f `
        $response.usage.total_tokens,
        $response.usage.prompt_tokens,
        $response.usage.completion_tokens) -ForegroundColor DarkGray
    return $text
}

# --- Teshis modu -------------------------------------------------------------
if ($Diagnose) {
    Write-Host "=== Saglik ===" -ForegroundColor Cyan
    Invoke-RestMethod -Uri "$BaseUrl/health" | ConvertTo-Json -Depth 5

    Write-Host "`n=== Upstream kimlik teshisi ===" -ForegroundColor Cyan
    Invoke-RestMethod -Uri "$BaseUrl/v1/admin/auth" `
        -Headers @{ Authorization = "Bearer $ApiKey" } | ConvertTo-Json -Depth 5

    Write-Host "`n=== Etkin yapilandirma ===" -ForegroundColor Cyan
    Invoke-RestMethod -Uri "$BaseUrl/v1/admin/config" `
        -Headers @{ Authorization = "Bearer $ApiKey" } | ConvertTo-Json -Depth 5
    exit 0
}

# --- Model secimi ------------------------------------------------------------
if (-not $Model) { $Model = Get-DefaultModel }

# --- Interaktif mod ----------------------------------------------------------
if ($Interactive) {
    Write-Host "Interaktif sohbet | model: $Model" -ForegroundColor Green
    Write-Host "Cikis: 'exit' veya Ctrl+C | Gecmisi temizle: 'clear'" -ForegroundColor DarkGray
    Write-Host ""

    $history = @()
    while ($true) {
        Write-Host "Siz > " -NoNewline -ForegroundColor Cyan
        $userInput = Read-Host
        if ([string]::IsNullOrWhiteSpace($userInput)) { continue }
        if ($userInput -in @("exit", "quit", "cikis")) { break }
        if ($userInput -eq "clear") {
            $history = @()
            Write-Host "Gecmis temizlendi." -ForegroundColor DarkGray
            continue
        }

        Write-Host "Yanit > " -NoNewline -ForegroundColor Green
        $body = Build-Body $userInput $history
        $reply = if ($NoStream) { Invoke-ChatBlocking $body } else { Invoke-ChatStream $body }

        if ($reply) {
            $history += @{ role = "user"; content = $userInput }
            $history += @{ role = "assistant"; content = $reply }
        }
        Write-Host ""
    }
    exit 0
}

# --- Tek mesaj ---------------------------------------------------------------
Write-Host "Model : $Model" -ForegroundColor DarkGray
Write-Host "Siz   : $Message" -ForegroundColor Cyan
Write-Host "Yanit : " -ForegroundColor Green -NoNewline
Write-Host ""

$body = Build-Body $Message @()
if ($NoStream) { [void](Invoke-ChatBlocking $body) } else { [void](Invoke-ChatStream $body) }
