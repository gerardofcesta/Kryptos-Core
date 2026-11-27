<#
.SYNOPSIS
    Setup Wizard portabile multi-hardware per Local LLM (Configurazione .env e percorsi modelli).
.DESCRIPTION
    1. Rileva automaticamente GPU/VRAM, RAM di sistema e spazio NVMe libero.
    2. Calcola il profilo hardware consigliato (workstation_16gb_vram, laptop_8gb_vram, cpu_only_16gb, ecc.),
       il tier di esecuzione, num_ctx, soglie Heavy MoE anti-swap e memoria WSL2.
    3. Configura in sicurezza LOCAL_LLM_MODELS_DIR all'esterno di %APPDATA%/%LOCALAPPDATA% e del repo,
       aggiornando opzionalmente .env senza alterare le variabili privacy (DO_NOT_TRACK=1, OLLAMA_NO_CLOUD=1, KVSAVE=0).
.PARAMETER ModelsDir
    Percorso esterno per i pesi modelli (LOCAL_LLM_MODELS_DIR).
.PARAMETER CheckOnly
    Mostra il rilevamento hardware e la configurazione consigliata senza scrivere su disco.
.PARAMETER ApplyEnv
    Scrive/aggiorna le chiavi hardware e LOCAL_LLM_MODELS_DIR in .env (preservando le impostazioni privacy).
.PARAMETER NonInteractive
    Esegue senza prompt interattivi.
#>

[CmdletBinding()]
param(
    [string]$ModelsDir = "",
    [switch]$CheckOnly,
    [switch]$ApplyEnv,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvPath     = Join-Path $ProjectRoot ".env"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Utf8NoBom   = New-Object System.Text.UTF8Encoding($false)

function Test-ModelsDirSafe {
    param([Parameter(Mandatory = $true)][string]$CandidatePath)

    if ([string]::IsNullOrWhiteSpace($CandidatePath)) {
        throw "LOCAL_LLM_MODELS_DIR non puo essere vuoto."
    }
    if ($CandidatePath.StartsWith('\\') -or $CandidatePath.StartsWith('//')) {
        throw "Percorsi UNC/rete non consentiti per LOCAL_LLM_MODELS_DIR: $CandidatePath"
    }
    if ($CandidatePath -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Path traversal ('..') non consentito in LOCAL_LLM_MODELS_DIR: $CandidatePath"
    }

    $full = [System.IO.Path]::GetFullPath($CandidatePath)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $root.TrimEnd('\')) {
        throw "La root del disco non puo essere usata come LOCAL_LLM_MODELS_DIR: $full"
    }

    $blockedPrefixes = @(
        $env:APPDATA,
        $env:LOCALAPPDATA,
        $env:WINDIR,
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:ProgramData,
        (Join-Path $ProjectRoot "workspaces")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($bp in $blockedPrefixes) {
        $bpFull = [System.IO.Path]::GetFullPath($bp).TrimEnd('\')
        if ($full.TrimEnd('\').Equals($bpFull, [System.StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($bpFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "LOCAL_LLM_MODELS_DIR '$full' non deve risiedere dentro '$bpFull'."
        }
    }

    return $full
}

function Get-HardwareRecommendation {
    param([string]$TargetModelsDir)

    if (Test-Path $VenvPython) {
        $pyCode = @'
import json, os, sys
from workspaces.local_llm_core.services.hardware_wizard import detect_hardware_specs, recommend_hardware_profile
specs = detect_hardware_specs()
mdir = os.environ.get("WIZARD_MODELS_DIR", "").strip() or None
rec = recommend_hardware_profile(
    vram_gb=specs["vram_gb"],
    ram_gb=specs["ram_gb"],
    gpu_name=specs["gpu_name"],
    free_disk_gb=specs["free_disk_gb"],
    models_dir=mdir,
)
print(json.dumps({"specs": specs, "recommendation": rec}, ensure_ascii=False))
'@
        $prevRoot = $env:PYTHONPATH
        $prevMdir = $env:WIZARD_MODELS_DIR
        try {
            $env:PYTHONPATH = $ProjectRoot
            $env:WIZARD_MODELS_DIR = $TargetModelsDir
            $rawJson = & $VenvPython -c $pyCode 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($rawJson)) {
                return ($rawJson | ConvertFrom-Json)
            }
        } finally {
            $env:PYTHONPATH = $prevRoot
            $env:WIZARD_MODELS_DIR = $prevMdir
        }
    }

    # Pure PowerShell fallback detection
    $gpuName = "CPU / Integrated Graphics"
    $vramGb = 0.0
    $nvsmi = Get-Command "nvidia-smi" -ErrorAction SilentlyContinue
    if ($nvsmi) {
        try {
            $nvOut = (& $nvsmi.Source --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
            if ($nvOut -and $nvOut.Contains(',')) {
                $parts = $nvOut -split ',', 2
                $gpuName = $parts[0].Trim()
                $vramGb = [math]::Round(([double]$parts[1].Trim()) / 1024.0, 2)
            }
        } catch {}
    }

    $ramBytes = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).TotalPhysicalMemory
    $ramGb = if ($ramBytes) { [math]::Round(($ramBytes / 1GB), 2) } else { 16.0 }
    $sysDrive = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(':') } else { "C" }
    $psDrive = Get-PSDrive -Name $sysDrive -ErrorAction SilentlyContinue
    $freeDiskGb = if ($psDrive) { [math]::Round(($psDrive.Free / 1GB), 2) } else { 100.0 }

    $resolvedDir = if (-not [string]::IsNullOrWhiteSpace($TargetModelsDir)) {
        Test-ModelsDirSafe -CandidatePath $TargetModelsDir
    } else {
        "$($sysDrive):\LocalLLM-Models"
    }

    $profileId = "cpu_only_16gb"
    $tier = "cpu_only"
    $numCtx = 8192
    $heavyRam = 4
    $heavyFree = 8
    $fastModel = "qwen2.5:3b-instruct-q4_K_M"
    if ($vramGb -ge 14.0) {
        $profileId = "workstation_16gb_vram"
        $tier = if ($ramGb -ge 28.0) { "dual_tier" } else { "fast_only" }
        $numCtx = 32768
        $heavyRam = [math]::Max(8, [math]::Min(14, [int]($ramGb * 0.375)))
        $heavyFree = [math]::Max(10, [int]($ramGb * 0.4375))
        $fastModel = "local-worker:latest"
    } elseif ($vramGb -ge 6.0) {
        $profileId = "laptop_8gb_vram"
        $tier = "fast_only"
        $numCtx = if ($ramGb -ge 16.0) { 16384 } else { 8192 }
        $fastModel = "qwen2.5:7b-instruct-q5_K_M"
    }

    return [pscustomobject]@{
        specs = [pscustomobject]@{
            gpu_name = $gpuName
            vram_gb = $vramGb
            ram_gb = $ramGb
            free_disk_gb = $freeDiskGb
        }
        recommendation = [pscustomobject]@{
            profile_id = $profileId
            recommended_tier = $tier
            num_ctx = $numCtx
            local_llm_models_dir = $resolvedDir
            env_overrides = [pscustomobject]@{
                LOCAL_LLM_HARDWARE_PROFILE = $profileId
                LOCAL_LLM_MODELS_DIR = $resolvedDir
                LOCAL_FAST_MODEL = $fastModel
                LOCAL_NUM_CTX = "$numCtx"
                HEAVY_MOE_RAM_GB = "$heavyRam"
                HEAVY_MOE_MIN_FREE_RAM_GB = "$heavyFree"
            }
        }
    }
}

function Update-DotEnvSafely {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][hashtable]$Updates
    )

    $lines = if (Test-Path $FilePath) {
        @(Get-Content -Path $FilePath -Encoding UTF8)
    } else {
        @()
    }

    $seen = @{}
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($raw in $lines) {
        $trimmed = $raw.Trim()
        if ($trimmed -and (-not $trimmed.StartsWith('#')) -and $trimmed.Contains('=')) {
            $k = ($trimmed -split '=', 2)[0].Trim()
            if ($Updates.ContainsKey($k)) {
                $out.Add("$k=$($Updates[$k])")
                $seen[$k] = $true
                continue
            }
        }
        $out.Add($raw)
    }

    foreach ($k in $Updates.Keys) {
        if (-not $seen.ContainsKey($k)) {
            $out.Add("$k=$($Updates[$k])")
        }
    }

    [System.IO.File]::WriteAllLines($FilePath, $out.ToArray(), $Utf8NoBom)
}

Write-Host "`n=== Local LLM Setup Wizard (Multi-Hardware Portabile) ===" -ForegroundColor Cyan

$chosenModelsDir = $ModelsDir
if (-not $CheckOnly -and -not $NonInteractive -and [string]::IsNullOrWhiteSpace($chosenModelsDir)) {
    $defaultSuggest = if ($env:LOCAL_LLM_MODELS_DIR) { $env:LOCAL_LLM_MODELS_DIR } else { "C:\LocalLLM-Models" }
    $inputDir = Read-Host "Directory esterna pesi modelli LOCAL_LLM_MODELS_DIR [$defaultSuggest]"
    if ([string]::IsNullOrWhiteSpace($inputDir)) { $chosenModelsDir = $defaultSuggest } else { $chosenModelsDir = $inputDir.Trim() }
}

if (-not [string]::IsNullOrWhiteSpace($chosenModelsDir)) {
    $chosenModelsDir = Test-ModelsDirSafe -CandidatePath $chosenModelsDir
}

$wizardData = Get-HardwareRecommendation -TargetModelsDir $chosenModelsDir
$rec = $wizardData.recommendation
$specs = $wizardData.specs

Write-Host ("Hardware rilevato: {0} | VRAM={1} GiB | RAM={2} GiB | Disco libero={3} GiB" -f $specs.gpu_name, $specs.vram_gb, $specs.ram_gb, $specs.free_disk_gb) -ForegroundColor Gray
Write-Host ("Profilo consigliato: {0} | Tier={1} | num_ctx={2}" -f $rec.profile_id, $rec.recommended_tier, $rec.num_ctx) -ForegroundColor Green
Write-Host ("LOCAL_LLM_MODELS_DIR: {0}" -f $rec.local_llm_models_dir) -ForegroundColor Green

if ($CheckOnly) {
    Write-Host "[CheckOnly] Nessuna modifica applicata su disco." -ForegroundColor Yellow
    exit 0
}

if ($ApplyEnv) {
    $safeDir = Test-ModelsDirSafe -CandidatePath $rec.local_llm_models_dir
    if (-not (Test-Path $safeDir)) {
        New-Item -ItemType Directory -Path $safeDir -Force | Out-Null
    }
    $updates = @{}
    $rec.env_overrides.PSObject.Properties | ForEach-Object {
        $updates[$_.Name] = [string]$_.Value
    }
    Update-DotEnvSafely -FilePath $EnvPath -Updates $updates
    Write-Host "[OK] Configurazione hardware e LOCAL_LLM_MODELS_DIR salvate in .env (flag privacy preservati)." -ForegroundColor Green
} else {
    Write-Host "[INFO] Usa -ApplyEnv per salvare le raccomandazioni in .env." -ForegroundColor Gray
}
