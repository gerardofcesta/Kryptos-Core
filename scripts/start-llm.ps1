<#
.SYNOPSIS
    Avvia il centro operativo Local LLM Dual-Tier in modalita 100% locale, privata e offline.
.DESCRIPTION
    Carica le variabili da `.env`, garantisce mutua esclusione rigorosa tra Fast Tier (Ollama 11434)
    e Heavy MoE Tier (Colibri v2.1.0 11435) con controllo anti-swap RAM, e opzionalmente avvia
    la Console Web Offline su http://127.0.0.1:8787.
.PARAMETER Mode
    Modalita di inferenza: 'fast' (Ollama), 'heavy' (Colibri Heavy MoE) o 'ui-only' (solo Console UI :8787). Default: 'fast'.
.PARAMETER WithUI
    Avvia anche la Console Web Offline locale (workspaces/offline-chat/server.py).
.PARAMETER OpenBrowser
    Apre automaticamente il browser sulla Console Web Offline (richiede -WithUI).
.PARAMETER RamLimitGB
    Override del tetto RAM (GB) per Heavy MoE (default da .env HEAVY_MOE_RAM_GB=12).
.PARAMETER Ctx
    Override della finestra di contesto per Heavy MoE (default da .env HEAVY_MOE_CTX=16384).
.PARAMETER Force
    Consente l'avvio di Heavy MoE anche in condizioni di RAM libera critica (< 10 GB).
.PARAMETER Restart
    Arresta l'istanza gia' attiva del motore target prima di avviarlo con i nuovi parametri (-RamLimitGB / -Ctx).
#>

[CmdletBinding()]
param(
    [ValidateSet('fast', 'heavy', 'ui-only')]
    [string]$Mode = 'fast',
    [switch]$WithUI,
    [switch]$OpenBrowser,
    [int]$RamLimitGB = 0,
    [int]$Ctx = 0,
    [switch]$Force,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvFile = Join-Path $ProjectRoot ".env"

function Initialize-ToolPath {
    $candidates = New-Object System.Collections.Generic.List[string]
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath) {
        foreach ($p in ($userPath -split ';')) {
            $trimmed = $p.Trim()
            if ($trimmed) { $candidates.Add($trimmed) }
        }
    }

    $wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
    if (Test-Path $wingetLinks) { $candidates.Add($wingetLinks) }

    $wingetPkgs = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wingetPkgs) {
        Get-ChildItem -Path $wingetPkgs -Directory -Filter "astral-sh.uv*" -ErrorAction SilentlyContinue | ForEach-Object {
            $candidates.Add($_.FullName)
        }
    }

    $current = @($env:Path -split ';' | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') })
    foreach ($dir in $candidates) {
        $norm = $dir.TrimEnd('\')
        if ((Test-Path $dir) -and ($current -notcontains $norm)) {
            $env:Path = "$env:Path;$dir"
            $current += $norm
        }
    }
}

function Resolve-RealPython {
    $candidates = New-Object System.Collections.Generic.List[string]
    $venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $candidates.Add($venvPy) }
    $localPy = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
    if (Test-Path $localPy) { $candidates.Add($localPy) }

    Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Source) { $candidates.Add($_.Source) }
    }

    $winAppsPy = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python.exe"
    if (Test-Path $winAppsPy) { $candidates.Add($winAppsPy) }

    foreach ($cand in ($candidates | Select-Object -Unique)) {
        try {
            $verOut = & $cand --version 2>&1
            if ($LASTEXITCODE -eq 0 -and ($verOut -match 'Python\s+3\.')) {
                return $cand
            }
        } catch {}
    }
    return $null
}

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            $key = $k.Trim()
            $val = $v.Trim()
            if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                if ($val.Length -ge 2) { $val = $val.Substring(1, $val.Length - 2) }
            }
            if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($key, 'Process'))) {
                [Environment]::SetEnvironmentVariable($key, $val, 'Process')
            }
        }
    }
    Write-Host "[OK] Caricate impostazioni privacy/GPU da .env (setdefault)" -ForegroundColor Green
}

function Test-HttpOk([string]$Url, [int]$TimeoutSec = 2) {
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Stop-ColibriEngine {
    $colibriNames = @(
        "qwen38", "qwen36", "colibri", "glm53", "deepseek_v41", "kimi_k3",
        "mimo", "inkling", "olmoe", "deepseek_v4", "laya", "gliner_decide", "qwenimage"
    )
    $found = $false
    $procs = Get-Process -Name $colibriNames -ErrorAction SilentlyContinue
    if ($procs) {
        $found = $true
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    $pyProcs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -like "*openai_server.py*" -or
            $_.CommandLine -like "*engines*colibri*coli*" -or
            $_.CommandLine -match 'coli(\.cmd)?\s+serve\b'
        }
    if ($pyProcs) {
        $found = $true
        $pyProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*engines*colibri*coli.cmd*serve*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    if ($found) {
        Write-Host "[MUTEX] Arrestato engine Heavy MoE Colibri per liberare RAM/VRAM." -ForegroundColor Yellow
        Start-Sleep -Seconds 1
    }
}

function Stop-OllamaEngine([string]$TargetOllamaUrl) {
    $running = Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue
    if ($running -or (Test-HttpOk -Url "$TargetOllamaUrl/api/version" -TimeoutSec 1)) {
        Write-Host "[MUTEX] Scaricamento modelli Ollama e arresto daemon per liberare 16 GB VRAM..." -ForegroundColor Yellow
        if (Get-Command ollama -ErrorAction SilentlyContinue) {
            $psOut = ollama ps 2>$null
            if ($psOut) {
                $psOut | Select-Object -Skip 1 | ForEach-Object {
                    $mName = ($_ -split '\s+')[0]
                    if ($mName) {
                        Write-Host "  Unload Ollama: $mName"
                        ollama stop $mName 2>$null | Out-Null
                    }
                }
            }
        }
        Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        for ($w = 0; $w -lt 10; $w++) {
            if (-not (Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 300
        }
    }
}

Initialize-ToolPath
$RealPython = Resolve-RealPython
if ($RealPython) {
    $pyDir = [System.IO.Path]::GetDirectoryName($RealPython)
    if ($env:Path -notlike "$pyDir*") {
        $env:Path = "$pyDir;$env:Path"
    }
}
Import-DotEnv -Path $EnvFile

$ollamaHost = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "127.0.0.1:11434" }
$ollamaUrl  = if ($ollamaHost -match '^https?://') { $ollamaHost } else { "http://$ollamaHost" }

$heavyHost = if ($env:HEAVY_MOE_HOST) { $env:HEAVY_MOE_HOST } else { "127.0.0.1" }
if ($heavyHost -notin @('127.0.0.1', 'localhost', '::1')) {
    Write-Warning "HEAVY_MOE_HOST ('$heavyHost') non e' loopback; forzato 127.0.0.1 per policy offline."
    $heavyHost = "127.0.0.1"
}
$heavyPort = if ($env:HEAVY_MOE_PORT -as [int]) { [int]$env:HEAVY_MOE_PORT } else { 11435 }
$heavyUrl  = "http://${heavyHost}:${heavyPort}"

if ($Mode -eq 'fast') {
    Stop-ColibriEngine

    if ($Restart) {
        Write-Host "[RESTART] Arresto Fast Tier Ollama per riavvio con nuova configurazione..." -ForegroundColor Yellow
        Stop-OllamaEngine -TargetOllamaUrl $ollamaUrl
    }

    if ($Ctx -gt 0) {
        [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "$Ctx", "Process")
    }

    if (-not (Test-HttpOk -Url "$ollamaUrl/api/version")) {
        if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            Write-Error "Comando 'ollama' non trovato nel PATH."
            exit 1
        }
        Write-Host "Avvio daemon Ollama locale su $ollamaUrl (Flash Attention + KV Cache Q8)..." -ForegroundColor Cyan
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-HttpOk -Url "$ollamaUrl/api/version") { $ready = $true; break }
        }
        if (-not $ready) {
            Write-Error "Ollama non ha risposto entro 15 secondi su $ollamaUrl."
            exit 1
        }
    }

    $ver = (Invoke-RestMethod "$ollamaUrl/api/version").version
    Write-Host "[ONLINE] Fast Tier Ollama v$ver attivo su $ollamaUrl (loopback-only)" -ForegroundColor Green
}
elseif ($Mode -eq 'heavy') {
    Stop-OllamaEngine -TargetOllamaUrl $ollamaUrl

    # Controllo Anti-Swap RAM fisica libera dopo lo scaricamento di Ollama
    $minFreeRam = if ($env:HEAVY_MOE_MIN_FREE_RAM_GB -as [double]) { [double]$env:HEAVY_MOE_MIN_FREE_RAM_GB } else { 14.0 }
    $os = Get-CimInstance Win32_OperatingSystem
    $freeRamGb = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $totalRamGb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)

    if ($freeRamGb -lt $minFreeRam) {
        Write-Host "`n[ANTI-SWAP] RAM fisica libera: $freeRamGb GB / $totalRamGb GB (soglia consigliata >= $minFreeRam GB)" -ForegroundColor Yellow
        Write-Host "Top 5 processi per consumo RAM:" -ForegroundColor Yellow
        Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 | ForEach-Object {
            $wsGb = [math]::Round($_.WorkingSet64 / 1GB, 2)
            Write-Host ("  - {0,-24} PID {1,-7} {2,6} GiB" -f $_.ProcessName, $_.Id, $wsGb) -ForegroundColor Gray
        }
        if ($freeRamGb -lt 10.0 -and -not $Force) {
            Write-Host "`n[ERRORE ANTI-SWAP] RAM libera critica ($freeRamGb GB < 10 GB). Rischio paging severo su Windows 11." -ForegroundColor Red
            Write-Host "Chiudi alcune applicazioni oppure usa -Force per forzare l'avvio." -ForegroundColor Red
            exit 1
        } else {
            Write-Warning "RAM fisica libera ($freeRamGb GB) inferiore a $minFreeRam GB; possibile pressione memoria durante l'inferenza."
        }
    } else {
        Write-Host "[OK] Controllo anti-swap superato: $freeRamGb GB RAM libera (soglia >= $minFreeRam GB)" -ForegroundColor Green
    }

    $coliCmd = Join-Path $ProjectRoot "engines\colibri\coli.cmd"
    if (-not (Test-Path $coliCmd)) {
        Write-Host "[ERRORE] Colibri non trovato in: $coliCmd" -ForegroundColor Red
        Write-Host "Installa i binari Colibri v2.1.0 in 'engines\colibri\' prima di avviare -Mode heavy." -ForegroundColor Yellow
        exit 1
    }

    if (-not $RealPython) {
        Write-Host "[ERRORE] Nessun interprete Python 3 reale funzionante trovato nel sistema." -ForegroundColor Red
        exit 1
    }

    $heavyModelsRoot = Join-Path $ProjectRoot "models\heavy-moe"
    $activeModelFile = Join-Path $heavyModelsRoot "ACTIVE_MODEL.json"
    $activeModel = $null
    $colibriDir = $null
    $modelId = if ($env:HEAVY_MOE_MODEL_ID) { $env:HEAVY_MOE_MODEL_ID } else { "heavy-moe-uncensored" }
    $backend = "auto"

    if (Test-Path $activeModelFile) {
        try {
            $activeModel = Get-Content $activeModelFile -Raw | ConvertFrom-Json
            if ($activeModel.modelId) { $modelId = [string]$activeModel.modelId }
            if ($activeModel.backend) { $backend = [string]$activeModel.backend }
            if ($activeModel.colibriDir) {
                $rawDir = [string]$activeModel.colibriDir
                if ([System.IO.Path]::IsPathRooted($rawDir)) {
                    $colibriDir = $rawDir
                } elseif (Test-Path (Join-Path $ProjectRoot $rawDir)) {
                    $colibriDir = (Resolve-Path (Join-Path $ProjectRoot $rawDir)).Path
                } elseif (Test-Path (Join-Path $heavyModelsRoot $rawDir)) {
                    $colibriDir = (Resolve-Path (Join-Path $heavyModelsRoot $rawDir)).Path
                } else {
                    $colibriDir = Join-Path $ProjectRoot $rawDir
                }
            }
            if ($activeModel.env) {
                foreach ($prop in $activeModel.env.PSObject.Properties) {
                    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($prop.Name, 'Process'))) {
                        [Environment]::SetEnvironmentVariable($prop.Name, [string]$prop.Value, 'Process')
                    }
                }
            }
        } catch {
            Write-Warning "Impossibile leggere $activeModelFile : $_"
        }
    }

    if (-not $colibriDir -or -not (Test-Path $colibriDir)) {
        if (Test-Path $heavyModelsRoot) {
            $fallbackDir = Get-ChildItem -Path $heavyModelsRoot -Filter "*.colibri" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $fallbackDir) {
                $fallbackFile = Get-ChildItem -Path $heavyModelsRoot -Filter "*.colibri" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($fallbackFile) { $colibriDir = $fallbackFile.DirectoryName }
            } else {
                $colibriDir = $fallbackDir.FullName
            }
        }
    }

    if (-not $colibriDir -or -not (Test-Path $colibriDir)) {
        Write-Host "[ERRORE] Nessun modello Heavy MoE valido trovato in '$heavyModelsRoot'." -ForegroundColor Red
        Write-Host "Verifica che esista 'models\heavy-moe\ACTIVE_MODEL.json' oppure una directory '*.colibri' importata con 'coli import --mode link'." -ForegroundColor Yellow
        exit 1
    }

    if (-not $env:KVSAVE) { [Environment]::SetEnvironmentVariable("KVSAVE", "0", "Process") }
    [Environment]::SetEnvironmentVariable("COLI_MODEL", $colibriDir, "Process")

    $ram = if ($RamLimitGB -gt 0) { $RamLimitGB } elseif ($env:HEAVY_MOE_RAM_GB -as [int]) { [int]$env:HEAVY_MOE_RAM_GB } else { 12 }
    $ctxVal = if ($Ctx -gt 0) { $Ctx } elseif ($env:HEAVY_MOE_CTX -as [int]) { [int]$env:HEAVY_MOE_CTX } else { 16384 }
    [Environment]::SetEnvironmentVariable("RAM_GB", "$ram", "Process")

    if ($Restart) {
        Write-Host "[RESTART] Arresto istanza Heavy MoE Colibri corrente per applicare ram=${ram}GB, ctx=$ctxVal..." -ForegroundColor Yellow
        Stop-ColibriEngine
    }

    if (-not (Test-HttpOk -Url "$heavyUrl/health" -TimeoutSec 2)) {
        Write-Host "Avvio Heavy MoE Colibri su $heavyUrl (model='$modelId', backend=$backend, ram=${ram}GB, ctx=$ctxVal)..." -ForegroundColor Cyan
        $serveArgs = @(
            "serve",
            "--model", "`"$colibriDir`"",
            "--host", $heavyHost,
            "--port", "$heavyPort",
            "--ram", "$ram",
            "--ctx", "$ctxVal",
            "--model-id", "`"$modelId`""
        )
        $proc = Start-Process -FilePath $coliCmd -ArgumentList $serveArgs -WorkingDirectory (Split-Path $coliCmd -Parent) -WindowStyle Hidden -PassThru

        $ready = $false
        for ($i = 0; $i -lt 90; $i++) {
            Start-Sleep -Seconds 1
            if (Test-HttpOk -Url "$heavyUrl/health" -TimeoutSec 2) {
                $ready = $true
                break
            }
            if ($proc.HasExited) {
                Write-Host "[ERRORE] Colibri e' terminato prematuramente (exit code $($proc.ExitCode))." -ForegroundColor Red
                exit 1
            }
        }
        if (-not $ready) {
            Write-Error "Heavy MoE Colibri non ha risposto su $heavyUrl/health entro 90 secondi."
            exit 1
        }
    }

    Write-Host "[ONLINE] Heavy MoE Colibri attivo su $heavyUrl (model=$modelId, ram=${ram}GB, ctx=$ctxVal, loopback-only)" -ForegroundColor Green
}
elseif ($Mode -eq 'ui-only') {
    Write-Host "[UI-ONLY] Avvio solo Console Offline Privata (nessun motore LLM avviato)." -ForegroundColor Cyan
    $WithUI = $true
}

if ($WithUI) {
    $uiHost = if ($env:OFFLINE_CHAT_HOST) { $env:OFFLINE_CHAT_HOST } else { "127.0.0.1" }
    $uiPort = if ($env:OFFLINE_CHAT_PORT) { $env:OFFLINE_CHAT_PORT } else { "8787" }
    $uiUrl  = "http://${uiHost}:${uiPort}"
    $serverPy = Join-Path $ProjectRoot "workspaces\offline-chat\server.py"

    $uiUp = Test-HttpOk -Url "$uiUrl/" -TimeoutSec 1
    if (-not $uiUp) {
        if (-not $RealPython) {
            Write-Error "Python 3 reale non trovato per avviare $serverPy."
            exit 1
        }
        Write-Host "Avvio Console Offline Privata su $uiUrl ..." -ForegroundColor Cyan
        Start-Process -FilePath $RealPython -ArgumentList "`"$serverPy`"" -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-HttpOk -Url "$uiUrl/" -TimeoutSec 1) { break }
        }
    }
    Write-Host "[ONLINE] Console Offline Privata attiva su: $uiUrl" -ForegroundColor Green
    if ($OpenBrowser) {
        Start-Process $uiUrl
    }
}
