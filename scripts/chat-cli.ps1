<#
.SYNOPSIS
    Avvia una sessione di chat 100% locale da terminale (zero cronologia persistente su disco).
.PARAMETER Mode
    Tier di esecuzione: 'fast' (Ollama) o 'heavy' (Colibri Heavy MoE). Default: 'fast'.
.PARAMETER Model
    Nome del modello Ollama (default: local-uncensored) oppure modelId Colibri in -Mode heavy.
.PARAMETER RamLimitGB
    Override del tetto RAM (GB) per Heavy MoE (passato a start-llm.ps1).
.PARAMETER Ctx
    Override della finestra di contesto per Heavy MoE (passato a start-llm.ps1).
#>

[CmdletBinding()]
param(
    [ValidateSet('fast', 'heavy')]
    [string]$Mode = 'fast',
    [string]$Model = "local-uncensored",
    [int]$RamLimitGB = 0,
    [int]$Ctx = 0
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StartScript = Join-Path $PSScriptRoot "start-llm.ps1"

function Load-SystemPrompt([string]$Path) {
    if (-not (Test-Path $Path)) { return "" }
    $raw = (Get-Content -Path $Path -Raw -Encoding UTF8).Trim()
    if ($raw.StartsWith("---")) {
        $parts = $raw -split '---', 3
        if ($parts.Length -eq 3) {
            return $parts[2].Trim()
        }
    }
    return $raw
}

function Import-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
        }
    }
}

Import-EnvFile -Path (Join-Path $ProjectRoot ".env")

if ($Mode -eq 'fast') {
    & pwsh -NoProfile -File $StartScript -Mode fast
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "`nAvvio chat terminale Fast Tier con '$Model' (scrivi /bye per uscire)..." -ForegroundColor Cyan
    & ollama run $Model
    exit $LASTEXITCODE
}

# --- Heavy MoE Mode (Colibri SSE REPL, zero disk writes) ---
$startArgs = @("-NoProfile", "-File", $StartScript, "-Mode", "heavy")
if ($RamLimitGB -gt 0) { $startArgs += @("-RamLimitGB", "$RamLimitGB") }
if ($Ctx -gt 0) { $startArgs += @("-Ctx", "$Ctx") }

& pwsh @startArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$heavyHost = if ($env:HEAVY_MOE_HOST) { $env:HEAVY_MOE_HOST } else { "127.0.0.1" }
$heavyPort = if ($env:HEAVY_MOE_PORT) { $env:HEAVY_MOE_PORT } else { "11435" }
$heavyUrl  = "http://${heavyHost}:${heavyPort}"

$activeModelId = $Model
if (-not $PSBoundParameters.ContainsKey('Model')) {
    $activeModelId = if ($env:HEAVY_MOE_MODEL_ID) { $env:HEAVY_MOE_MODEL_ID } else { "heavy-moe-uncensored" }
    try {
        $modelsResp = Invoke-RestMethod -Uri "$heavyUrl/v1/models" -TimeoutSec 5
        if ($modelsResp.data -and $modelsResp.data.Count -gt 0 -and $modelsResp.data[0].id) {
            $activeModelId = [string]$modelsResp.data[0].id
        }
    } catch {}
}

$systemPrompt = Load-SystemPrompt -Path (Join-Path $ProjectRoot "prompts\uncensored-direct.md")
$history = New-Object 'System.Collections.Generic.List[hashtable]'
$thinkEnabled = $true

Add-Type -AssemblyName System.Net.Http
$httpClient = [System.Net.Http.HttpClient]::new()
$httpClient.Timeout = [TimeSpan]::FromMinutes(15)

Write-Host ""
Write-Host "=== Heavy MoE Chat CLI (Colibri SSE · 100% RAM/Offline) ===" -ForegroundColor Cyan
Write-Host "Endpoint : $heavyUrl/v1/chat/completions" -ForegroundColor Gray
Write-Host "Modello  : $activeModelId" -ForegroundColor Gray
Write-Host "Comandi  : /bye (esci), /clear (azzera RAM), /think on|off (stato attuale: ON)" -ForegroundColor Gray
Write-Host ""

try {
    while ($true) {
        Write-Host -NoNewline "Tu> " -ForegroundColor Green
        $userInput = [Console]::ReadLine()
        if ($null -eq $userInput) { break }
        $trimmed = $userInput.Trim()
        if (-not $trimmed) { continue }

        if ($trimmed -eq '/bye' -or $trimmed -eq '/exit' -or $trimmed -eq '/quit') {
            break
        }
        if ($trimmed -eq '/clear') {
            $history.Clear()
            Write-Host "[OK] Cronologia cancellata dalla RAM." -ForegroundColor Yellow
            continue
        }
        if ($trimmed -match '^/think\s+(on|off)$') {
            $thinkEnabled = ($Matches[1] -eq 'on')
            $stateLabel = if ($thinkEnabled) { "ON" } else { "OFF" }
            Write-Host "[OK] Thinking impostato su: $stateLabel" -ForegroundColor Yellow
            continue
        }
        if ($trimmed -eq '/help') {
            Write-Host "Comandi disponibili: /bye, /clear, /think on, /think off" -ForegroundColor Gray
            continue
        }

        $history.Add(@{ role = "user"; content = $trimmed })

        $payloadMessages = New-Object 'System.Collections.Generic.List[hashtable]'
        if ($systemPrompt) {
            $payloadMessages.Add(@{ role = "system"; content = $systemPrompt })
        }
        foreach ($m in $history) {
            $payloadMessages.Add($m)
        }

        $bodyObj = @{
            model           = $activeModelId
            messages        = $payloadMessages
            stream          = $true
            stream_options  = @{ include_usage = $true }
            enable_thinking = $thinkEnabled
            temperature     = 0.7
        }
        if (-not $thinkEnabled) {
            $bodyObj["reasoning_effort"] = "none"
        }

        $jsonBody = $bodyObj | ConvertTo-Json -Depth 8 -Compress
        $content = [System.Net.Http.StringContent]::new($jsonBody, [System.Text.Encoding]::UTF8, "application/json")
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, "$heavyUrl/v1/chat/completions")
        $request.Content = $content

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $response = $null
        $stream = $null
        $reader = $null

        $replyContent = [System.Text.StringBuilder]::new()
        $replyReasoning = [System.Text.StringBuilder]::new()
        $inThinking = $false
        $printedAnswerHeader = $false
        $ttftSec = $null
        $promptTokens = 0
        $completionTokens = 0

        try {
            $response = $httpClient.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                $errBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                Write-Host "`n[ERRORE HTTP $([int]$response.StatusCode)] $errBody" -ForegroundColor Red
                $history.RemoveAt($history.Count - 1)
                continue
            }

            $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)

            while (-not $reader.EndOfStream) {
                $line = $reader.ReadLine()
                if ($null -eq $line) { break }
                if (-not $line.StartsWith("data:")) { continue }

                $dataPart = $line.Substring(5).Trim()
                if (-not $dataPart) { continue }
                if ($dataPart -eq "[DONE]") { break }

                $chunk = $null
                try {
                    $chunk = $dataPart | ConvertFrom-Json
                } catch {
                    continue
                }

                if ($chunk.usage) {
                    if ($null -ne $chunk.usage.prompt_tokens) { $promptTokens = [int]$chunk.usage.prompt_tokens }
                    if ($null -ne $chunk.usage.completion_tokens) { $completionTokens = [int]$chunk.usage.completion_tokens }
                }

                if ($chunk.choices -and $chunk.choices.Count -gt 0) {
                    $delta = $chunk.choices[0].delta
                    if ($delta) {
                        $rText = $delta.reasoning_content
                        $cText = $delta.content

                        if (-not [string]::IsNullOrEmpty($rText)) {
                            if ($null -eq $ttftSec) { $ttftSec = $sw.Elapsed.TotalSeconds }
                            if (-not $inThinking) {
                                $inThinking = $true
                                Write-Host "`n[Think] " -NoNewline -ForegroundColor DarkCyan
                            }
                            [Console]::ForegroundColor = [ConsoleColor]::DarkGray
                            [Console]::Write($rText)
                            [Console]::ResetColor()
                            $null = $replyReasoning.Append($rText)
                        }

                        if (-not [string]::IsNullOrEmpty($cText)) {
                            if ($null -eq $ttftSec) { $ttftSec = $sw.Elapsed.TotalSeconds }
                            if ($inThinking -and -not $printedAnswerHeader) {
                                Write-Host "`n"
                            }
                            if (-not $printedAnswerHeader) {
                                $printedAnswerHeader = $true
                                Write-Host "`nAssistente> " -NoNewline -ForegroundColor Cyan
                            }
                            [Console]::Write($cText)
                            $null = $replyContent.Append($cText)
                        }
                    }
                }
            }

            $sw.Stop()
            Write-Host ""
            $totalSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
            $ttftStr = if ($null -ne $ttftSec) { "{0:N2}s" -f $ttftSec } else { "N/D" }
            if ($completionTokens -gt 0 -and $null -ne $ttftSec) {
                $decodeSec = [math]::Max(0.001, $sw.Elapsed.TotalSeconds - $ttftSec)
                $tps = [math]::Round($completionTokens / $decodeSec, 1)
                Write-Host ("[Stats: TTFT={0} | Total={1}s | Tokens={2} in / {3} out | Decode~{4} tok/s]`n" -f $ttftStr, $totalSec, $promptTokens, $completionTokens, $tps) -ForegroundColor DarkGray
            } else {
                Write-Host ("[Stats: TTFT={0} | Total={1}s]`n" -f $ttftStr, $totalSec) -ForegroundColor DarkGray
            }

            $assistantMsg = @{ role = "assistant"; content = $replyContent.ToString() }
            if ($replyReasoning.Length -gt 0) {
                $assistantMsg["reasoning_content"] = $replyReasoning.ToString()
            }
            $history.Add($assistantMsg)
        } finally {
            if ($reader) { $reader.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($response) { $response.Dispose() }
            if ($request) { $request.Dispose() }
            if ($content) { $content.Dispose() }
        }
    }
} finally {
    $httpClient.Dispose()
}
