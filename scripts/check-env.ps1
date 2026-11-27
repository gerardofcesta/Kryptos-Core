<#
.SYNOPSIS
    Diagnostica completa del centro operativo Local LLM Dual-Tier (solo lettura).
.DESCRIPTION
    Verifica GPU/VRAM, RAM libera + regola anti-swap, spazio disco, presenza CLI (incluso uv
    e Colibri v2.1.0), stato endpoint locali (11434, 11435, 8787, 1234, 3000) e modelli locali.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'SilentlyContinue'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

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
                return [PSCustomObject]@{
                    Path    = $cand
                    Version = ($verOut | Select-Object -First 1).ToString().Trim()
                }
            }
        } catch {}
    }
    return $null
}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Test-HttpEndpoint([string]$Url, [int]$TimeoutSec = 2) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    } catch {
        return $false
    }
}

Initialize-ToolPath
$realPython = Resolve-RealPython
if ($realPython) {
    $pyDir = [System.IO.Path]::GetDirectoryName($realPython.Path)
    if ($env:Path -notlike "$pyDir*") {
        $env:Path = "$pyDir;$env:Path"
    }
}

# Leggi soglia anti-swap da .env se presente
$minFreeRam = 14.0
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*HEAVY_MOE_MIN_FREE_RAM_GB\s*=\s*(.+)$') {
            $parsed = 0.0
            if ([double]::TryParse($Matches[1].Trim(), [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
                $minFreeRam = $parsed
            }
        }
    }
}

Write-Section "GPU & VRAM"
if (Get-Command nvidia-smi) {
    $gpu = nvidia-smi --query-gpu=name,memory.used,memory.free,memory.total,temperature.gpu,power.draw,driver_version --format=csv,noheader
    Write-Host "  $gpu" -ForegroundColor Green
} else {
    Write-Host "  nvidia-smi non trovato" -ForegroundColor Yellow
}

Write-Section "RAM & STORAGE (ANTI-SWAP)"
$os = Get-CimInstance Win32_OperatingSystem
$ramTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$ramFree  = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
Write-Host ("  RAM: {0} GB liberi / {1} GB totali" -f $ramFree, $ramTotal)
if ($ramFree -ge $minFreeRam) {
    Write-Host ("  [OK]       Anti-Swap Heavy MoE: {0} GB >= soglia {1} GB" -f $ramFree, $minFreeRam) -ForegroundColor Green
} elseif ($ramFree -ge 10.0) {
    Write-Host ("  [WARNING]  Anti-Swap Heavy MoE: {0} GB < soglia consigliata {1} GB (chiudi app pesanti prima di -Mode heavy)" -f $ramFree, $minFreeRam) -ForegroundColor Yellow
} else {
    Write-Host ("  [CRITICAL] Anti-Swap Heavy MoE: {0} GB < 10 GB (rischio swap Windows 11!)" -f $ramFree) -ForegroundColor Red
}

foreach ($drive in @('C', 'B', 'K')) {
    $d = Get-PSDrive -Name $drive -PSProvider FileSystem
    if ($d) {
        $freeGb  = [math]::Round($d.Free / 1GB, 1)
        $totalGb = [math]::Round(($d.Used + $d.Free) / 1GB, 1)
        Write-Host ("  Drive {0}:\ -> {1} GB liberi / {2} GB totali" -f $drive, $freeGb, $totalGb)
    }
}

Write-Section "TOOL & RUNTIME CLI"
$tools = @('ollama', 'lms', 'llama-server', 'docker', 'python', 'uv', 'nvcc', 'hf', 'git', 'wsl')
foreach ($t in $tools) {
    if ($t -eq 'python') {
        if ($realPython) {
            Write-Host ("  [OK]   {0,-14} -> {1} ({2})" -f $t, $realPython.Path, $realPython.Version) -ForegroundColor Green
        } else {
            Write-Host ("  [----] {0,-14} -> nessun Python 3 reale funzionante" -f $t) -ForegroundColor Yellow
        }
        continue
    }
    $cmd = Get-Command $t
    if ($cmd) {
        Write-Host ("  [OK]   {0,-14} -> {1}" -f $t, $cmd.Source) -ForegroundColor Green
    } else {
        Write-Host ("  [----] {0,-14} -> non installato" -f $t) -ForegroundColor DarkGray
    }
}

$coliCmd = Join-Path $ProjectRoot "engines\colibri\coli.cmd"
$coliDir = Join-Path $ProjectRoot "engines\colibri"
if (Test-Path $coliCmd) {
    $coliVer = "presente"
    $verPy = Join-Path $coliDir "version.py"
    if (Test-Path $verPy) {
        $verMatch = Select-String -Path $verPy -Pattern '__version__\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
        if ($verMatch) { $coliVer = "v$($verMatch.Matches[0].Groups[1].Value)" }
    }
    $cudaDll = Test-Path (Join-Path $coliDir "coli_cuda.dll")
    $cudaTag = if ($cudaDll) { "CUDA+Vulkan" } else { "Vulkan/CPU" }
    Write-Host ("  [OK]   {0,-14} -> {1} ({2}, {3})" -f "colibri", $coliCmd, $coliVer, $cudaTag) -ForegroundColor Green
} else {
    Write-Host ("  [----] {0,-14} -> non installato in engines\colibri\" -f "colibri") -ForegroundColor DarkGray
}

Write-Section "STATO SERVIZI & ENDPOINT LOCALI"
$endpointsFile = Join-Path $ProjectRoot "config\endpoints.json"
if (Test-Path $endpointsFile) {
    $cfg = Get-Content $endpointsFile -Raw | ConvertFrom-Json
    foreach ($prop in $cfg.endpoints.PSObject.Properties) {
        $name = $prop.Name
        $ep = $prop.Value
        $healthUrl = "$($ep.baseUrl)$($ep.healthPath)"
        $online = Test-HttpEndpoint -Url $healthUrl
        if ($online) {
            Write-Host ("  [ONLINE]  {0,-16} ({1})" -f $name, $ep.baseUrl) -ForegroundColor Green
        } else {
            Write-Host ("  [OFFLINE] {0,-16} ({1})" -f $name, $ep.baseUrl) -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  File config/endpoints.json non trovato." -ForegroundColor Red
}

Write-Section "DOCKER ENGINE"
if (Get-Command docker) {
    $null = docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [ONLINE]  Docker daemon attivo" -ForegroundColor Green
    } else {
        Write-Host "  [OFFLINE] Docker Desktop non avviato" -ForegroundColor Yellow
    }
}

Write-Section "MODELLI PRESENTI (SOLO LETTURA)"
if (Test-Path "$env:USERPROFILE\.ollama\models\manifests") {
    $olCount = (Get-ChildItem "$env:USERPROFILE\.ollama\models\manifests" -Recurse -File | Measure-Object).Count
    Write-Host "  Ollama manifests locali : $olCount"
} else {
    Write-Host "  Ollama manifests locali : 0"
}
if (Test-Path "$env:USERPROFILE\.lmstudio\models") {
    $lmFiles = Get-ChildItem "$env:USERPROFILE\.lmstudio\models" -Recurse -File | Where-Object { $_.Extension -in '.gguf','.safetensors','.bin' }
    Write-Host ("  LM Studio model files   : {0}" -f ($lmFiles | Measure-Object).Count)
} else {
    Write-Host "  LM Studio model files   : 0"
}

$heavyDir = Join-Path $ProjectRoot "models\heavy-moe"
$activeModelFile = Join-Path $heavyDir "ACTIVE_MODEL.json"
if (Test-Path $heavyDir) {
    $ggufFiles = @(Get-ChildItem $heavyDir -Recurse -File -Filter "*.gguf" -ErrorAction SilentlyContinue)
    $ggufBytes = ($ggufFiles | Measure-Object -Property Length -Sum).Sum
    $ggufGb = if ($ggufBytes) { [math]::Round($ggufBytes / 1GB, 2) } else { 0 }
    $colibriDirs = @(Get-ChildItem $heavyDir -Recurse -Directory -Filter "*.colibri" -ErrorAction SilentlyContinue)
    Write-Host ("  Heavy MoE (.gguf)       : {0} file ({1} GiB) | dirs .colibri: {2}" -f $ggufFiles.Count, $ggufGb, $colibriDirs.Count)
    if (Test-Path $activeModelFile) {
        try {
            $am = Get-Content $activeModelFile -Raw | ConvertFrom-Json
            Write-Host ("  ACTIVE_MODEL.json       : id={0}, backend={1}, dir={2}" -f $am.modelId, $am.backend, $am.colibriDir) -ForegroundColor Green
        } catch {
            Write-Host "  ACTIVE_MODEL.json       : presente (errore parsing JSON)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  ACTIVE_MODEL.json       : assente" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  Heavy MoE (heavy-moe/)  : cartella non presente" -ForegroundColor DarkGray
}

Write-Host ""
