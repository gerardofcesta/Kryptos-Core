<#
.SYNOPSIS
    Builds the zero-CDN React/Vite SPA, PyInstaller Windows bundle, and Inno Setup 6 installer
    for Kryptos Core.
#>
param(
    [switch]$SkipWebBuild,
    [switch]$SkipPyInstaller,
    [switch]$SkipInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$WebDir = Join-Path $RepoRoot "workspaces\offline-chat\web"
$WebDistIndex = Join-Path $WebDir "dist\index.html"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DistDir = Join-Path $RepoRoot "dist\KryptosCore"
$InstallerIss = Join-Path $RepoRoot "installer\kryptos_core.iss"
$InstallerOutDir = Join-Path $RepoRoot "installer\out"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Project virtualenv Python not found at $VenvPython. Run 'uv sync' first."
}

$LocalNodeDir = Join-Path $env:LOCALAPPDATA "Programs\nodejs"
if (Test-Path -LiteralPath $LocalNodeDir) {
    $env:PATH = "$LocalNodeDir;$env:PATH"
}

Write-Host "=== Kryptos Core Build Pipeline ($RepoRoot) ===" -ForegroundColor Cyan

# 1. Build zero-CDN React + TypeScript + Vite + Tailwind SPA
if (-not $SkipWebBuild) {
    Write-Host "[1/4] Building zero-CDN React SPA in workspaces/offline-chat/web/..." -ForegroundColor Yellow
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npmCmd) {
        throw "npm not found on PATH (checked $LocalNodeDir)."
    }

    Push-Location $WebDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $WebDir "node_modules"))) {
            & $npmCmd.Source ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        }
        & $npmCmd.Source run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $WebDistIndex)) {
    throw "Compiled SPA entry point missing: $WebDistIndex"
}
Write-Host "[1/4] Verified offline SPA bundle: $WebDistIndex" -ForegroundColor Green

# 2. Build standalone PyInstaller onedir distribution
if (-not $SkipPyInstaller) {
    Write-Host "[2/4] Packaging KryptosCore.exe with PyInstaller..." -ForegroundColor Yellow
    $pyInstallerArgs = @(
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "KryptosCore",
        "--distpath", (Join-Path $RepoRoot "dist"),
        "--workpath", (Join-Path $RepoRoot "build\pyinstaller"),
        "--specpath", (Join-Path $RepoRoot "build"),
        "--paths", $RepoRoot,
        "--add-data", "$(Join-Path $WebDir 'dist');web/dist",
        "--add-data", "$(Join-Path $RepoRoot 'config');config",
        "--add-data", "$(Join-Path $RepoRoot 'prompts');prompts",
        "--hidden-import", "workspaces.local_llm_core.app",
        "--hidden-import", "workspaces.local_llm_core.desktop",
        (Join-Path $RepoRoot "workspaces\local_llm_core\desktop.py")
    )

    $iconPath = Join-Path $RepoRoot "launcher\local-llm.ico"
    if (Test-Path -LiteralPath $iconPath) {
        $pyInstallerArgs = @($pyInstallerArgs[0..($pyInstallerArgs.Length - 2)]) + @(
            "--icon", $iconPath,
            "--add-data", "$iconPath;launcher",
            $pyInstallerArgs[-1]
        )
    }

    $pyInstallerExe = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"
    if (Test-Path -LiteralPath $pyInstallerExe) {
        & $pyInstallerExe @pyInstallerArgs
    } else {
        $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uvCmd) {
            throw "Neither .venv\Scripts\pyinstaller.exe nor uv was found."
        }
        & $uvCmd.Source run --with pyinstaller pyinstaller @pyInstallerArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    Write-Host "[2/4] PyInstaller bundle ready at: $DistDir" -ForegroundColor Green
} else {
    Write-Host "[2/4] Skipping PyInstaller step (-SkipPyInstaller)" -ForegroundColor DarkGray
}

# 3. Regenerate deterministic Inno Setup 6 script via built-in desktop_shell helper
Write-Host "[3/4] Rendering installer/kryptos_core.iss..." -ForegroundColor Yellow
$env:PYTHONPATH = $RepoRoot
& $VenvPython -c "from pathlib import Path; from workspaces.local_llm_core.desktop import write_inno_script; write_inno_script(Path(r'$InstallerIss'))"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to render $InstallerIss"
}
Write-Host "[3/4] Rendered $InstallerIss" -ForegroundColor Green

# 4. Compile Inno Setup 6 installer
if (-not $SkipInstaller) {
    Write-Host "[4/4] Compiling Inno Setup 6 installer..." -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $DistDir)) {
        throw "Distribution directory not found: $DistDir. Run without -SkipPyInstaller first."
    }

    $isccCandidates = @(
        $env:INNO_ISCC_PATH,
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $isccCmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($isccCmd) {
        $isccCandidates = @($isccCmd.Source) + $isccCandidates
    }

    $isccExe = $null
    foreach ($cand in $isccCandidates) {
        if (-not [string]::IsNullOrWhiteSpace($cand) -and (Test-Path -LiteralPath $cand)) {
            $isccExe = $cand
            break
        }
    }
    if (-not $isccExe) {
        throw "Inno Setup 6 compiler (ISCC.exe) not found. Install Inno Setup 6 or set INNO_ISCC_PATH."
    }

    New-Item -ItemType Directory -Force -Path $InstallerOutDir | Out-Null
    & $isccExe "/Q" $InstallerIss
    if ($LASTEXITCODE -ne 0) {
        throw "ISCC.exe failed with exit code $LASTEXITCODE"
    }
    Write-Host "[4/4] Installer compiled in: $InstallerOutDir" -ForegroundColor Green
} else {
    Write-Host "[4/4] Skipping Inno Setup compile step (-SkipInstaller)" -ForegroundColor DarkGray
}

# 5. Create portable ZIP + SHA256SUMS.txt + CycloneDX SBOM when artifacts exist
New-Item -ItemType Directory -Force -Path $InstallerOutDir | Out-Null
$PortableZip = Join-Path $InstallerOutDir "LocalLLM-v3.0.0-windows-x64-portable.zip"
if (Test-Path -LiteralPath $DistDir) {
    if (Test-Path -LiteralPath $PortableZip) {
        Remove-Item -LiteralPath $PortableZip -Force
    }
    Compress-Archive -Path "$DistDir\*" -DestinationPath $PortableZip -CompressionLevel Optimal
    Write-Host "[5/5] Created portable archive: $PortableZip" -ForegroundColor Green
}
& $VenvPython (Join-Path $RepoRoot "scripts\generate_release_manifest.py") --artifacts-dir $InstallerOutDir --repo-root $RepoRoot --version "3.0.0"
if ($LASTEXITCODE -ne 0) {
    throw "generate_release_manifest.py failed with exit code $LASTEXITCODE"
}

