<#
.SYNOPSIS
    Build una-tantum dell'immagine Docker offline `local-llm-sandbox:offline`.
.DESCRIPTION
    Verifica lo spazio libero su C:\ (>= 250 GB), verifica la disponibilita del CLI e del daemon
    Docker (con opzione -StartDocker per avviare Docker Desktop e attendere fino a 90s),
    esegue il build di Dockerfile.sandbox e valida l'immagine con uno smoke-test in isolamento
    (--network none --read-only --tmpfs /work).
#>

[CmdletBinding()]
param(
    [switch]$StartDocker,
    [string]$ImageTag = "local-llm-sandbox:offline",
    [int]$MinFreeSpaceGB = 250,
    [int]$DockerWaitSec = 90,
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'

$SandboxDir = $PSScriptRoot
$DockerfilePath = Join-Path $SandboxDir "Dockerfile.sandbox"
$DockerDesktopExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail([string]$Message) {
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Test-DockerDaemon {
    try {
        $null = & docker info --format "{{.ServerVersion}}" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# 1. Preflight: spazio libero su C:\ (>= 250 GB safety rule)
Write-Step "Verifica spazio libero su C:\"
try {
    $cDrive = Get-PSDrive -Name C -ErrorAction Stop
    $freeGB = [math]::Round(($cDrive.Free / 1GB), 2)
} catch {
    Write-Fail "Impossibile leggere lo spazio libero del disco C:\: $($_.Exception.Message)"
    exit 1
}

if ($freeGB -lt $MinFreeSpaceGB) {
    Write-Fail "Spazio libero su C:\ insufficiente: $freeGB GB rilevati (soglia di sicurezza: >= $MinFreeSpaceGB GB)."
    exit 1
}
Write-Ok "Spazio libero su C:\: $freeGB GB (soglia >= $MinFreeSpaceGB GB)"

# 2. Preflight: Dockerfile esistente
if (-not (Test-Path -LiteralPath $DockerfilePath -PathType Leaf)) {
    Write-Fail "Dockerfile non trovato: $DockerfilePath"
    exit 1
}
Write-Ok "Dockerfile rilevato: $DockerfilePath"

# 3. Preflight: Docker CLI
Write-Step "Verifica Docker CLI e daemon"
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Fail "Docker CLI non trovato nel PATH. Installa Docker Desktop per Windows e riprova."
    exit 1
}
Write-Ok "Docker CLI trovato: $($dockerCmd.Source)"

# 4. Verifica Docker daemon (con avvio opzionale -StartDocker)
if (-not (Test-DockerDaemon)) {
    if ($StartDocker) {
        if (-not (Test-Path -LiteralPath $DockerDesktopExe -PathType Leaf)) {
            Write-Fail "Docker Desktop non trovato in: $DockerDesktopExe"
            exit 1
        }
        Write-Warn "Docker daemon spento. Avvio Docker Desktop e attesa fino a ${DockerWaitSec}s..."
        Start-Process -FilePath $DockerDesktopExe | Out-Null

        $deadline = (Get-Date).AddSeconds($DockerWaitSec)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            if (Test-DockerDaemon) {
                $ready = $true
                break
            }
            $remaining = [math]::Max(0, [int]($deadline - (Get-Date)).TotalSeconds)
            Write-Host "  Attesa daemon Docker... (${remaining}s rimanenti)" -ForegroundColor DarkGray
        }

        if (-not $ready) {
            Write-Fail "Timeout: il daemon Docker non ha risposto entro ${DockerWaitSec}s."
            Write-Host "Controlla Docker Desktop nella tray di Windows e riprova quando il motore e attivo." -ForegroundColor Yellow
            exit 1
        }
        Write-Ok "Daemon Docker avviato e raggiungibile."
    } else {
        Write-Fail "Il daemon Docker non e attivo (docker info fallito)."
        Write-Host ""
        Write-Host "Per procedere hai due opzioni:" -ForegroundColor Yellow
        Write-Host "  1) Avvia manualmente Docker Desktop e attendi 'Engine running', poi riesegui questo script."
        Write-Host "  2) Esegui direttamente con avvio automatico:"
        Write-Host "     pwsh -File `"$PSCommandPath`" -StartDocker" -ForegroundColor White
        exit 1
    }
} else {
    $serverVer = (& docker info --format "{{.ServerVersion}}" 2>&1 | Select-Object -First 1).ToString().Trim()
    Write-Ok "Daemon Docker attivo (Server $serverVer)"
}

# 5. Build immagine offline
Write-Step "Build immagine $ImageTag da Dockerfile.sandbox"
$buildArgs = @("build", "-t", $ImageTag, "-f", "Dockerfile.sandbox")
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += "."

Push-Location -LiteralPath $SandboxDir
try {
    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "docker build fallito con exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
Write-Ok "Immagine costruita con successo: $ImageTag"

# 6. Smoke-test offline (--network none --read-only --tmpfs /work)
Write-Step "Smoke-test isolamento offline (--network none --read-only --tmpfs /work)"
$baseRunFlags = @(
    "run", "--rm",
    "--label", "local-llm-orchestrator=true",
    "--network", "none",
    "--read-only",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "128",
    "--memory", "512m",
    "--cpus", "2",
    "--tmpfs", "/work:rw,nosuid,size=64m",
    $ImageTag
)

Write-Host "-> Test import Python (duckdb, pandas, pyarrow, fitz, openpyxl)..." -ForegroundColor DarkCyan
$pyCheckArgs = $baseRunFlags + @("python", "-c", "import duckdb, pandas, pyarrow, fitz, openpyxl; print('OK')")
$pyOut = & docker @pyCheckArgs 2>&1
if ($LASTEXITCODE -ne 0 -or (($pyOut | Out-String) -notmatch '\bOK\b')) {
    Write-Fail "Smoke-test import Python fallito:"
    Write-Host ($pyOut | Out-String)
    exit 1
}
Write-Ok "Import Python: OK"

Write-Host "-> Test CLI sandbox (rg, ruff, bandit, pytest)..." -ForegroundColor DarkCyan
$cliCheckArgs = $baseRunFlags + @("sh", "-c", "rg --version && ruff --version && bandit --version && pytest --version")
$cliOut = & docker @cliCheckArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Smoke-test CLI sandbox fallito:"
    Write-Host ($cliOut | Out-String)
    exit 1
}
$cliOut | ForEach-Object { Write-Host "   $_" -ForegroundColor Gray }
Write-Ok "CLI sandbox verificate (rg, ruff, bandit, pytest)"

# 7. Riepilogo dimensione immagine
try {
    $imgSizeBytes = [double](& docker image inspect $ImageTag --format "{{.Size}}" 2>&1 | Select-Object -First 1)
    $imgSizeMB = [math]::Round($imgSizeBytes / 1MB, 1)
    Write-Ok "Build completato. Immagine pronta: $ImageTag (~${imgSizeMB} MB)"
} catch {
    Write-Ok "Build completato. Immagine pronta: $ImageTag"
}

exit 0
