<#
.SYNOPSIS
    Esegue benchmark locali offline per Fast Tier (Ollama) o Heavy MoE Tier (Colibri v2.1.0).
.DESCRIPTION
    Supporta benchmark singolo o batteria completa (-FullSuite a 3 prompt), misurando
    TTFT reale (Stopwatch), Prefill tok/s, Decode tok/s, token in/out, VRAM picco (nvidia-smi)
    e RAM picco processo, stampando una riga Markdown pronta per benchmarks/RESULTS.md.
.PARAMETER Mode
    Tier da testare: 'fast' (Ollama 11434) o 'heavy' (Colibri 11435). Default: 'fast'.
.PARAMETER Model
    Identificativo del modello. Default: 'local-uncensored' in fast, 'heavy-moe-uncensored' (o da .env/ACTIVE_MODEL.json) in heavy.
.PARAMETER NumCtx
    Finestra di contesto da passare (usata in Ollama options.num_ctx; informativa in heavy). Default: 16384.
.PARAMETER FullSuite
    Esegue la batteria standard di 3 prompt (IT tecnico, logica multi-step con vincoli, analisi security difensiva).
.PARAMETER EnableThinking
    Se specificato, attiva il ragionamento interno (think) su entrambi i backend durante il benchmark. Di default e' disattivato per un confronto equo 1:1.
#>

[CmdletBinding()]
param(
    [ValidateSet('fast', 'heavy')]
    [string]$Mode = 'fast',
    [string]$Model = '',
    [int]$NumCtx = 16384,
    [switch]$FullSuite,
    [switch]$EnableThinking
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvFile = Join-Path $ProjectRoot ".env"

if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
        }
    }
}

$ollamaHost = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "127.0.0.1:11434" }
$ollamaUrl  = if ($ollamaHost -match '^https?://') { $ollamaHost } else { "http://$ollamaHost" }
$heavyHost  = if ($env:HEAVY_MOE_HOST) { $env:HEAVY_MOE_HOST } else { "127.0.0.1" }
$heavyPort  = if ($env:HEAVY_MOE_PORT) { $env:HEAVY_MOE_PORT } else { "11435" }
$heavyUrl   = "http://${heavyHost}:${heavyPort}"

if ([string]::IsNullOrWhiteSpace($Model)) {
    if ($Mode -eq 'fast') {
        $Model = 'local-uncensored'
    } else {
        $Model = if ($env:HEAVY_MOE_MODEL_ID) { $env:HEAVY_MOE_MODEL_ID } else { 'heavy-moe-uncensored' }
        $activeFile = Join-Path $ProjectRoot "models\heavy-moe\ACTIVE_MODEL.json"
        if (Test-Path $activeFile) {
            try {
                $am = Get-Content $activeFile -Raw | ConvertFrom-Json
                if ($am.modelId) { $Model = [string]$am.modelId }
            } catch {}
        }
        try {
            $mResp = Invoke-RestMethod -Uri "$heavyUrl/v1/models" -TimeoutSec 3
            if ($mResp.data -and $mResp.data.Count -gt 0 -and $mResp.data[0].id) {
                $Model = [string]$mResp.data[0].id
            }
        } catch {}
    }
}

# Verifica endpoint pronto
$checkUrl = if ($Mode -eq 'fast') { "$ollamaUrl/api/version" } else { "$heavyUrl/health" }
try {
    $r = Invoke-WebRequest -Uri $checkUrl -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -lt 200 -or $r.StatusCode -ge 300) { throw "HTTP $($r.StatusCode)" }
} catch {
    Write-Host "[ERRORE] Endpoint per -Mode $Mode non raggiungibile su $checkUrl." -ForegroundColor Red
    Write-Host "Avvia prima il servizio con: .\scripts\start-llm.ps1 -Mode $Mode" -ForegroundColor Yellow
    exit 1
}

Add-Type -AssemblyName System.Net.Http
$script:HttpClient = [System.Net.Http.HttpClient]::new()
$script:HttpClient.Timeout = [TimeSpan]::FromMinutes(15)

function Get-CurrentMetricsSnapshot([string]$TargetMode) {
    $vramMiB = 0.0
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        try {
            $raw = (nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | Select-Object -First 1).Trim()
            [void][double]::TryParse($raw, [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$vramMiB)
        } catch {}
    }

    $ramBytes = 0L
    if ($TargetMode -eq 'fast') {
        Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | ForEach-Object { $ramBytes += $_.WorkingSet64 }
    } else {
        $names = @("qwen38", "qwen36", "colibri", "glm53", "deepseek_v41", "kimi_k3", "mimo", "inkling", "olmoe", "deepseek_v4", "laya")
        Get-Process -Name $names -ErrorAction SilentlyContinue | ForEach-Object { $ramBytes += $_.WorkingSet64 }
        Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*openai_server.py*" -or $_.CommandLine -like "*engines*colibri*coli*" } |
            ForEach-Object { $ramBytes += [int64]$_.WorkingSetSize }
    }

    return [PSCustomObject]@{
        VramMiB  = $vramMiB
        RamBytes = $ramBytes
    }
}

function Start-ResourceSampler([string]$TargetMode) {
    $base = Get-CurrentMetricsSnapshot -TargetMode $TargetMode
    $state = [hashtable]::Synchronized(@{
        Stop         = $false
        PeakVramMiB  = [double]$base.VramMiB
        PeakRamBytes = [int64]$base.RamBytes
    })

    $ps = [PowerShell]::Create()
    $null = $ps.AddScript({
        param($SyncState, $ModeArg)
        $hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
        $colibriNames = @("qwen38", "qwen36", "colibri", "glm53", "deepseek_v41", "kimi_k3", "mimo", "inkling", "olmoe", "deepseek_v4", "laya")
        while (-not $SyncState.Stop) {
            if ($hasNvidia) {
                try {
                    $raw = (nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | Select-Object -First 1).Trim()
                    $val = 0.0
                    if ([double]::TryParse($raw, [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$val)) {
                        if ($val -gt $SyncState.PeakVramMiB) { $SyncState.PeakVramMiB = $val }
                    }
                } catch {}
            }
            $ram = 0L
            try {
                if ($ModeArg -eq 'fast') {
                    Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | ForEach-Object { $ram += $_.WorkingSet64 }
                } else {
                    Get-Process -Name $colibriNames -ErrorAction SilentlyContinue | ForEach-Object { $ram += $_.WorkingSet64 }
                    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
                        Where-Object { $_.CommandLine -like "*openai_server.py*" -or $_.CommandLine -like "*engines*colibri*coli*" } |
                        ForEach-Object { $ram += [int64]$_.WorkingSetSize }
                }
                if ($ram -gt $SyncState.PeakRamBytes) { $SyncState.PeakRamBytes = $ram }
            } catch {}
            Start-Sleep -Milliseconds 400
        }
    }).AddArgument($state).AddArgument($TargetMode)

    $handle = $ps.BeginInvoke()
    return [PSCustomObject]@{
        PowerShell = $ps
        Handle     = $handle
        State      = $state
    }
}

function Stop-ResourceSampler($Sampler, [string]$TargetMode) {
    $Sampler.State.Stop = $true
    try { $Sampler.PowerShell.EndInvoke($Sampler.Handle) } catch {}
    try { $Sampler.PowerShell.Dispose() } catch {}

    $final = Get-CurrentMetricsSnapshot -TargetMode $TargetMode
    if ($final.VramMiB -gt $Sampler.State.PeakVramMiB) { $Sampler.State.PeakVramMiB = $final.VramMiB }
    if ($final.RamBytes -gt $Sampler.State.PeakRamBytes) { $Sampler.State.PeakRamBytes = $final.RamBytes }

    return [PSCustomObject]@{
        PeakVramGiB = [math]::Round($Sampler.State.PeakVramMiB / 1024.0, 2)
        PeakVramMiB = [math]::Round($Sampler.State.PeakVramMiB, 0)
        PeakRamGiB  = [math]::Round($Sampler.State.PeakRamBytes / 1GB, 2)
    }
}

function Invoke-FastPrompt([string]$PromptName, [string]$PromptText) {
    $payload = @{
        model    = $Model
        messages = @(@{ role = "user"; content = $PromptText })
        stream   = $true
        think    = [bool]$EnableThinking
        options  = @{
            num_ctx     = $NumCtx
            temperature = 0.2
        }
    } | ConvertTo-Json -Depth 6 -Compress

    $content = [System.Net.Http.StringContent]::new($payload, [System.Text.Encoding]::UTF8, "application/json")
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, "$ollamaUrl/api/chat")
    $request.Content = $content

    $sampler = Start-ResourceSampler -TargetMode 'fast'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $response = $null; $stream = $null; $reader = $null

    $ttftSec = $null
    $promptTokens = 0
    $evalTokens = 0
    $promptNs = 0.0
    $evalNs = 0.0
    $sb = [System.Text.StringBuilder]::new()

    try {
        $response = $script:HttpClient.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            $err = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            throw "Ollama HTTP $([int]$response.StatusCode): $err"
        }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)

        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $obj = $line | ConvertFrom-Json
            if ($obj.message) {
                $c = $obj.message.content
                $t = $obj.message.thinking
                if (($null -eq $ttftSec) -and ((-not [string]::IsNullOrEmpty($c)) -or (-not [string]::IsNullOrEmpty($t)))) {
                    $ttftSec = $sw.Elapsed.TotalSeconds
                }
                if (-not [string]::IsNullOrEmpty($c)) { $null = $sb.Append($c) }
            }
            if ($obj.done -eq $true) {
                if ($null -ne $obj.prompt_eval_count) { $promptTokens = [int]$obj.prompt_eval_count }
                if ($null -ne $obj.eval_count) { $evalTokens = [int]$obj.eval_count }
                if ($null -ne $obj.prompt_eval_duration) { $promptNs = [double]$obj.prompt_eval_duration }
                if ($null -ne $obj.eval_duration) { $evalNs = [double]$obj.eval_duration }
            }
        }
        $sw.Stop()
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Dispose() }
        if ($request) { $request.Dispose() }
        if ($content) { $content.Dispose() }
        $peak = Stop-ResourceSampler -Sampler $sampler -TargetMode 'fast'
    }

    $totalSec = $sw.Elapsed.TotalSeconds
    $ttftVal = if ($null -ne $ttftSec) { $ttftSec } else { $totalSec }
    $prefillTps = if ($promptNs -gt 0) { [math]::Round($promptTokens / ($promptNs / 1e9), 1) } else { 0.0 }
    $decodeTps  = if ($evalNs -gt 0) { [math]::Round($evalTokens / ($evalNs / 1e9), 1) } else { 0.0 }

    return [PSCustomObject]@{
        PromptName  = $PromptName
        TTFT        = [math]::Round($ttftVal, 2)
        TotalSec    = [math]::Round($totalSec, 2)
        PrefillTps  = $prefillTps
        DecodeTps   = $decodeTps
        PromptTok   = $promptTokens
        OutputTok   = $evalTokens
        PeakVramGiB = $peak.PeakVramGiB
        PeakRamGiB  = $peak.PeakRamGiB
        Preview     = $sb.ToString()
    }
}

function Invoke-HeavyPrompt([string]$PromptName, [string]$PromptText) {
    $payloadObj = [ordered]@{
        model           = $Model
        messages        = @(@{ role = "user"; content = $PromptText })
        stream          = $true
        stream_options  = @{ include_usage = $true }
        temperature     = 0.2
        max_tokens      = 640
        enable_thinking = [bool]$EnableThinking
    }
    if ($EnableThinking) {
        $payloadObj["reasoning_effort"] = "low"
    } else {
        $payloadObj["reasoning_effort"] = "none"
    }
    $payload = $payloadObj | ConvertTo-Json -Depth 6 -Compress

    $content = [System.Net.Http.StringContent]::new($payload, [System.Text.Encoding]::UTF8, "application/json")
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, "$heavyUrl/v1/chat/completions")
    $request.Content = $content

    $sampler = Start-ResourceSampler -TargetMode 'heavy'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $response = $null; $stream = $null; $reader = $null

    $ttftSec = $null
    $promptTokens = 0
    $completionTokens = 0
    $deltaChunks = 0
    $finishReason = ""
    $sbContent = [System.Text.StringBuilder]::new()
    $sbReason  = [System.Text.StringBuilder]::new()

    try {
        $response = $script:HttpClient.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            $err = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            throw "Colibri HTTP $([int]$response.StatusCode): $err"
        }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)

        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ($null -eq $line -or -not $line.StartsWith("data:")) { continue }
            $dataPart = $line.Substring(5).Trim()
            if (-not $dataPart) { continue }
            if ($dataPart -eq "[DONE]") { break }

            $chunk = $null
            try { $chunk = $dataPart | ConvertFrom-Json } catch { continue }

            if ($chunk.usage) {
                if ($null -ne $chunk.usage.prompt_tokens) { $promptTokens = [int]$chunk.usage.prompt_tokens }
                if ($null -ne $chunk.usage.completion_tokens) { $completionTokens = [int]$chunk.usage.completion_tokens }
            }
            if ($chunk.choices -and $chunk.choices.Count -gt 0) {
                if ($chunk.choices[0].finish_reason) {
                    $finishReason = [string]$chunk.choices[0].finish_reason
                }
                $delta = $chunk.choices[0].delta
                if ($delta) {
                    $r = $delta.reasoning_content
                    $c = $delta.content
                    if (($null -eq $ttftSec) -and ((-not [string]::IsNullOrEmpty($r)) -or (-not [string]::IsNullOrEmpty($c)))) {
                        $ttftSec = $sw.Elapsed.TotalSeconds
                    }
                    if (-not [string]::IsNullOrEmpty($r)) { $null = $sbReason.Append($r); $deltaChunks++ }
                    if (-not [string]::IsNullOrEmpty($c)) { $null = $sbContent.Append($c); $deltaChunks++ }
                }
            }
        }
        $sw.Stop()
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Dispose() }
        if ($request) { $request.Dispose() }
        if ($content) { $content.Dispose() }
        $peak = Stop-ResourceSampler -Sampler $sampler -TargetMode 'heavy'
    }

    if ($completionTokens -le 0 -and $deltaChunks -gt 0) {
        $completionTokens = $deltaChunks
    }
    if ($finishReason -and $finishReason -notin @("stop", "length")) {
        Write-Warning "Finish reason anomalo su '$PromptName': $finishReason"
    }

    $totalSec = $sw.Elapsed.TotalSeconds
    $ttftVal = if ($null -ne $ttftSec) { $ttftSec } else { $totalSec }
    $prefillTps = if ($promptTokens -gt 0 -and $ttftVal -gt 0) { [math]::Round($promptTokens / $ttftVal, 1) } else { 0.0 }
    $decodeSec  = [math]::Max(0.001, $totalSec - $ttftVal)
    $decodeTps  = if ($completionTokens -gt 1) {
        [math]::Round(($completionTokens - 1) / $decodeSec, 1)
    } elseif ($completionTokens -gt 0) {
        [math]::Round($completionTokens / $decodeSec, 1)
    } else { 0.0 }

    $previewText = if ($sbContent.Length -gt 0) { $sbContent.ToString() } else { "[Think] " + $sbReason.ToString() }

    return [PSCustomObject]@{
        PromptName  = $PromptName
        TTFT        = [math]::Round($ttftVal, 2)
        TotalSec    = [math]::Round($totalSec, 2)
        PrefillTps  = $prefillTps
        DecodeTps   = $decodeTps
        PromptTok   = $promptTokens
        OutputTok   = $completionTokens
        PeakVramGiB = $peak.PeakVramGiB
        PeakRamGiB  = $peak.PeakRamGiB
        Preview     = $previewText
    }
}

$prompts = @(
    @{
        Name = "1. Italiano Tecnico (Flash Attention + MoE)"
        Text = "Spiega in italiano in modo tecnico e strutturato (circa 180 parole) come funziona la Flash Attention negli LLM e perche i modelli Mixture of Experts (MoE) riducono il costo computazionale per token."
    }
)

if ($FullSuite) {
    $prompts += @(
        @{
            Name = "2. Ragionamento Logico Multi-Step con Vincoli"
            Text = "Risolvi questo problema logico passo dopo passo verificando tutti i vincoli. Quattro server locali (Alfa, Beta, Gamma, Delta) ospitano ciascuno un servizio diverso (Ollama, Colibri, Proxy, Monitor) su porte distinte (11434, 11435, 8787, 9090). Vincoli: 1) Alfa non ospita Proxy e non usa la porta 11435. 2) Il servizio sulla porta 8787 e ospitato da Gamma. 3) Colibri gira su una porta dispari maggiore di 11434 ed e ospitato da Delta. 4) Ollama usa una porta pari diversa da 9090 e non e su Beta. 5) Proxy e esposto sulla porta 8787. Associa in modo univoco Server, Servizio e Porta e verifica ogni vincolo."
        },
        @{
            Name = "3. Analisi Tecnica / Red-Team Difensiva"
            Text = "Dal punto di vista di un security engineer difensivo, analizza in italiano i principali rischi di esfiltrazione dati e prompt injection in un server HTTP locale OpenAI-compatibile esposto per errore su 0.0.0.0 invece di 127.0.0.1, elencando 5 controlli tecnici concreti di hardening (CSP, binding loopback, validazione Host header, sanitizzazione log, isolamento rete)."
        }
    )
}

Write-Host "`nAvvio benchmark [-Mode $Mode] su modello '$Model' (Ctx=$NumCtx, FullSuite=$($FullSuite.IsPresent))..." -ForegroundColor Cyan
$results = New-Object System.Collections.Generic.List[object]

try {
    foreach ($p in $prompts) {
        Write-Host "`n--- Esecuzione: $($p.Name) ---" -ForegroundColor Yellow
        $res = if ($Mode -eq 'fast') {
            Invoke-FastPrompt -PromptName $p.Name -PromptText $p.Text
        } else {
            Invoke-HeavyPrompt -PromptName $p.Name -PromptText $p.Text
        }
        $results.Add($res)

        Write-Host ("  TTFT          : {0} s (Total: {1} s)" -f $res.TTFT, $res.TotalSec)
        Write-Host ("  Prefill Speed : {0} tok/s ({1} prompt tokens)" -f $res.PrefillTps, $res.PromptTok)
        Write-Host ("  Decode Speed  : {0} tok/s ({1} output tokens)" -f $res.DecodeTps, $res.OutputTok)
        Write-Host ("  VRAM Picco    : {0} GiB" -f $res.PeakVramGiB)
        Write-Host ("  RAM Picco     : {0} GiB" -f $res.PeakRamGiB)
        $snipLen = [math]::Min(360, $res.Preview.Length)
        Write-Host ("  Estratto      : {0}" -f ($res.Preview.Substring(0, $snipLen) -replace '\r?\n', ' ')) -ForegroundColor DarkGray
    }
} finally {
    $script:HttpClient.Dispose()
}

$avgPrefill = [math]::Round(($results | Measure-Object -Property PrefillTps -Average).Average, 1)
$avgDecode  = [math]::Round(($results | Measure-Object -Property DecodeTps -Average).Average, 1)
$avgTtft    = [math]::Round(($results | Measure-Object -Property TTFT -Average).Average, 2)
$maxVram    = [math]::Round(($results | Measure-Object -Property PeakVramGiB -Maximum).Maximum, 2)
$maxRam     = [math]::Round(($results | Measure-Object -Property PeakRamGiB -Maximum).Maximum, 2)
$today      = Get-Date -Format "yyyy-MM-dd"
$runtimeStr = if ($Mode -eq 'fast') { "Ollama (FA + KV q8_0)" } else { "Colibri v2.1.0" }
$tierStr    = if ($Mode -eq 'fast') { "Fast Tier" } else { "Heavy MoE Tier" }

if ($FullSuite) {
    Write-Host "`n=== RIEPILOGO FULL SUITE (3 PROMPT) ===" -ForegroundColor Green
    Write-Host "Modello        : $Model ($tierStr)"
    Write-Host "TTFT Medio     : $avgTtft s"
    Write-Host "Prefill Medio  : $avgPrefill tok/s"
    Write-Host "Decode Medio   : $avgDecode tok/s"
    Write-Host "VRAM Picco     : $maxVram GiB"
    Write-Host "RAM Picco      : $maxRam GiB"
}

Write-Host "`n=== RIGA PRONTA PER benchmarks/RESULTS.md ===" -ForegroundColor Cyan
Write-Host "| $today | ``$Model`` | — | $runtimeStr | $NumCtx | $avgPrefill | **$avgDecode** | $maxVram | $maxRam | $tierStr | TTFT medio ${avgTtft}s, suite $($results.Count) prompt OK |"
Write-Host ""
