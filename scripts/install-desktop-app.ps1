<#
.SYNOPSIS
    Installa (o rimuove) il collegamento App Desktop e Menu Start per "Local LLM".
.DESCRIPTION
    Genera l'icona personalizzata `launcher\local-llm.ico` e crea:
      - Collegamento sul Desktop:      <Desktop>\Local LLM.lnk
      - Collegamento nel Menu Start:   <StartMenu>\Programs\Local LLM.lnk
    Entrambi avviano l'app silenziosamente (zero finestra console) in App Mode offline.
.PARAMETER Uninstall
    Rimuove i collegamenti dal Desktop e dal Menu Start.
.PARAMETER ForceIcon
    Rigenera `launcher\local-llm.ico` anche se gia' presente.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$ForceIcon
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LauncherDir = Join-Path $ProjectRoot "launcher"
$IconScript  = Join-Path $LauncherDir "generate-icon.ps1"
$IconPath    = Join-Path $LauncherDir "local-llm.ico"
$VbsPath     = Join-Path $LauncherDir "launch-app.vbs"

$DesktopDir   = [Environment]::GetFolderPath('Desktop')
$StartMenuDir = [Environment]::GetFolderPath('Programs')

$DesktopLnk   = Join-Path $DesktopDir "Local LLM.lnk"
$StartMenuLnk = Join-Path $StartMenuDir "Local LLM.lnk"

if ($Uninstall) {
    foreach ($lnk in @($DesktopLnk, $StartMenuLnk)) {
        if (Test-Path $lnk) {
            Remove-Item -Path $lnk -Force
            Write-Host "[OK] Rimosso: $lnk" -ForegroundColor Yellow
        }
    }
    Write-Host "[OK] Collegamenti Local LLM rimossi." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $VbsPath)) {
    Write-Error "Launcher non trovato: $VbsPath"
    exit 1
}

if ($ForceIcon -or -not (Test-Path $IconPath)) {
    & $IconScript -OutPath $IconPath
}

$wscriptExe = Join-Path $env:WINDIR "System32\wscript.exe"
$wsh = New-Object -ComObject WScript.Shell

foreach ($lnkPath in @($DesktopLnk, $StartMenuLnk)) {
    $sc = $wsh.CreateShortcut($lnkPath)
    $sc.TargetPath       = $wscriptExe
    $sc.Arguments        = "`"$VbsPath`""
    $sc.WorkingDirectory = $ProjectRoot
    $sc.IconLocation     = "$IconPath,0"
    $sc.Description      = "Local LLM — Console Offline Dual-Tier (Fast 27B + Heavy MoE 177B)"
    $sc.WindowStyle      = 7
    $sc.Save()
    Write-Host "[OK] Collegamento creato: $lnkPath" -ForegroundColor Green
}

Write-Host "`nPronto! Puoi avviare 'Local LLM' con doppio clic dal Desktop oppure cercandolo nel Menu Start (e fissarlo alla barra delle applicazioni)." -ForegroundColor Cyan
