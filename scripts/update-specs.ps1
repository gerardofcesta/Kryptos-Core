<#
.SYNOPSIS
    Genera uno snapshot aggiornato di hardware, software, Colibri Heavy MoE ed endpoint
    per facilitare l'aggiornamento di specs.md e config/hardware-profile.json.
.PARAMETER OutJson
    Se specificato, salva uno snapshot grezzo in JSON nel percorso indicato.
#>

[CmdletBinding()]
param(
    [string]$OutJson
)

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
                return $cand
            }
        } catch {}
    }
    return $null
}

Initialize-ToolPath
$realPython = Resolve-RealPython
if ($realPython) {
    $pyDir = [System.IO.Path]::GetDirectoryName($realPython)
    if ($env:Path -notlike "$pyDir*") {
        $env:Path = "$pyDir;$env:Path"
    }
}

$os   = Get-CimInstance Win32_OperatingSystem
$cpu  = Get-CimInstance Win32_Processor | Select-Object -First 1
$rams = Get-CimInstance Win32_PhysicalMemory

$gpuRaw = $null
if (Get-Command nvidia-smi) {
    $gpuRaw = (nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version,compute_cap,power.limit,pcie.link.gen.max,pcie.link.width.max --format=csv,noheader) -split ',\s*'
}

$ramFreeGb = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
$minFreeRamGb = 14.0
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*HEAVY_MOE_MIN_FREE_RAM_GB\s*=\s*(.+)$') {
            $parsed = 0.0
            if ([double]::TryParse($Matches[1].Trim(), [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
                $minFreeRamGb = $parsed
            }
        }
    }
}

$storageList = @()
foreach ($drv in @('C', 'B', 'K')) {
    $d = Get-PSDrive -Name $drv -PSProvider FileSystem
    if ($d) {
        $storageList += [ordered]@{
            drive   = "${drv}:\"
            freeGb  = [math]::Round($d.Free / 1GB, 1)
            totalGb = [math]::Round(($d.Used + $d.Free) / 1GB, 1)
        }
    }
}

$coliDir = Join-Path $ProjectRoot "engines\colibri"
$coliCmd = Join-Path $coliDir "coli.cmd"
$coliVer = $null
$verPy = Join-Path $coliDir "version.py"
if (Test-Path $verPy) {
    $m = Select-String -Path $verPy -Pattern '__version__\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
    if ($m) { $coliVer = $m.Matches[0].Groups[1].Value }
}

$heavyDir = Join-Path $ProjectRoot "models\heavy-moe"
$activeModelFile = Join-Path $heavyDir "ACTIVE_MODEL.json"
$activeModel = $null
if (Test-Path $activeModelFile) {
    $activeModel = Get-Content $activeModelFile -Raw | ConvertFrom-Json
}
$ggufFiles = if (Test-Path $heavyDir) { @(Get-ChildItem $heavyDir -Recurse -File -Filter "*.gguf" -ErrorAction SilentlyContinue) } else { @() }
$ggufTotalGb = if ($ggufFiles.Count -gt 0) {
    [math]::Round((($ggufFiles | Measure-Object -Property Length -Sum).Sum) / 1GB, 2)
} else { 0 }

$endpointStatus = [ordered]@{}
$endpointsFile = Join-Path $ProjectRoot "config\endpoints.json"
if (Test-Path $endpointsFile) {
    $cfg = Get-Content $endpointsFile -Raw | ConvertFrom-Json
    foreach ($prop in $cfg.endpoints.PSObject.Properties) {
        $ep = $prop.Value
        $url = "$($ep.baseUrl)$($ep.healthPath)"
        $isUp = $false
        try {
            $resp = Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 2 -UseBasicParsing
            $isUp = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
        } catch {}
        $endpointStatus[$prop.Name] = [ordered]@{
            baseUrl = $ep.baseUrl
            online  = $isUp
        }
    }
}

$snapshot = [ordered]@{
    capturedAt = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    os = [ordered]@{
        caption      = $os.Caption
        version      = $os.Version
        build        = $os.BuildNumber
        architecture = $os.OSArchitecture
        ramTotalGb   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
        ramFreeGb    = $ramFreeGb
    }
    antiSwap = [ordered]@{
        freeRamGb            = $ramFreeGb
        minFreeRamRequiredGb = $minFreeRamGb
        readyForHeavyMoe     = ($ramFreeGb -ge $minFreeRamGb)
    }
    cpu = [ordered]@{
        name        = $cpu.Name.Trim()
        cores       = $cpu.NumberOfCores
        threads     = $cpu.NumberOfLogicalProcessors
        maxClockMhz = $cpu.MaxClockSpeed
    }
    ramModules = @($rams | ForEach-Object {
        [ordered]@{
            manufacturer = $_.Manufacturer
            capacityGb   = [math]::Round($_.Capacity / 1GB, 0)
            speedMt      = $_.ConfiguredClockSpeed
            partNumber   = $_.PartNumber.Trim()
        }
    })
    gpu = if ($gpuRaw) {
        [ordered]@{
            name              = $gpuRaw[0]
            memoryTotal       = $gpuRaw[1]
            memoryFree        = $gpuRaw[2]
            driverVersion     = $gpuRaw[3]
            computeCapability = $gpuRaw[4]
            powerLimit        = $gpuRaw[5]
            pcieMaxGen        = $gpuRaw[6]
            pcieMaxWidth      = $gpuRaw[7]
        }
    } else { $null }
    storage = $storageList
    colibri = [ordered]@{
        installed = (Test-Path $coliCmd)
        coliCmd   = if (Test-Path $coliCmd) { $coliCmd } else { $null }
        version   = $coliVer
        cudaDll   = (Test-Path (Join-Path $coliDir "coli_cuda.dll"))
    }
    heavyMoe = [ordered]@{
        directoryExists = (Test-Path $heavyDir)
        activeModelJson = (Test-Path $activeModelFile)
        modelId         = if ($activeModel) { $activeModel.modelId } else { $null }
        backend         = if ($activeModel) { $activeModel.backend } else { $null }
        colibriDir      = if ($activeModel) { $activeModel.colibriDir } else { $null }
        ggufFilesCount  = $ggufFiles.Count
        ggufTotalGb     = $ggufTotalGb
    }
    endpoints = $endpointStatus
    tools = [ordered]@{}
}

foreach ($tool in @('ollama', 'lms', 'llama-server', 'docker', 'python', 'uv', 'nvcc', 'hf', 'git', 'wsl', 'winget')) {
    if ($tool -eq 'python') {
        $snapshot.tools[$tool] = $realPython
        continue
    }
    $cmd = Get-Command $tool
    $snapshot.tools[$tool] = if ($cmd) { $cmd.Source } else { $null }
}
$snapshot.tools['colibri'] = if (Test-Path $coliCmd) { $coliCmd } else { $null }

if ($OutJson) {
    $snapshot | ConvertTo-Json -Depth 6 | Set-Content -Path $OutJson -Encoding UTF8
    Write-Host "Snapshot salvato in: $OutJson" -ForegroundColor Green
} else {
    $snapshot | ConvertTo-Json -Depth 6
}
