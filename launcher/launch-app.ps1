<#
.SYNOPSIS
    Launcher Desktop user-friendly per Kryptos Core.
.DESCRIPTION
    Mostra un breve splash nativo durante l'avvio dei servizi locali (100% offline),
    attiva l'icona nella System Tray di Windows e apre la Console nella finestra
    Desktop nativa (pywebview private_mode=True) con fallback opzionale a browser
    App Mode isolato.
#>

[CmdletBinding()]
param(
    [ValidateSet('auto', 'fast', 'heavy', 'ui-only')]
    [string]$Mode = 'auto',
    [switch]$BrowserFallback
)

$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StartScript = Join-Path $ProjectRoot "scripts\start-llm.ps1"
$TrayScript  = Join-Path $PSScriptRoot "tray-app.ps1"
$IconPath    = Join-Path $PSScriptRoot "local-llm.ico"
$LegacyAppProfile = Join-Path $PSScriptRoot ".app-profile"
$AppProfile  = Join-Path ([System.IO.Path]::GetTempPath()) ("KryptosCore-AppProfile-{0}" -f $PID)
$UiUrl       = "http://127.0.0.1:8787"

function Test-LocalUrl([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 1 -UseBasicParsing
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch { return $false }
}

function Show-Splash([string]$StatusText) {
    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.Size = New-Object System.Drawing.Size(420, 150)
    $form.BackColor = [System.Drawing.Color]::FromArgb(15, 21, 32)
    $form.TopMost = $true
    $form.ShowInTaskbar = $false

    $form.Add_Paint({
        param($s, $e)
        $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(63, 185, 80), 2)
        $e.Graphics.DrawRectangle($pen, 1, 1, $s.Width - 2, $s.Height - 2)
        $pen.Dispose()
    })

    if (Test-Path $IconPath) {
        $pic = New-Object System.Windows.Forms.PictureBox
        $pic.Size = New-Object System.Drawing.Size(48, 48)
        $pic.Location = New-Object System.Drawing.Point(24, 30)
        $pic.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom
        $pic.Image = (New-Object System.Drawing.Icon($IconPath, 48, 48)).ToBitmap()
        $form.Controls.Add($pic)
    }

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Kryptos Core"
    $title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
    $title.ForeColor = [System.Drawing.Color]::FromArgb(230, 237, 243)
    $title.AutoSize = $true
    $title.Location = New-Object System.Drawing.Point(86, 26)
    $form.Controls.Add($title)

    $sub = New-Object System.Windows.Forms.Label
    $sub.Text = "100% Offline · Loopback 127.0.0.1 · Zero Telemetria"
    $sub.Font = New-Object System.Drawing.Font("Segoe UI", 8.5)
    $sub.ForeColor = [System.Drawing.Color]::FromArgb(63, 185, 80)
    $sub.AutoSize = $true
    $sub.Location = New-Object System.Drawing.Point(88, 56)
    $form.Controls.Add($sub)

    $status = New-Object System.Windows.Forms.Label
    $status.Name = "StatusLabel"
    $status.Text = $StatusText
    $status.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)
    $status.ForeColor = [System.Drawing.Color]::FromArgb(158, 172, 190)
    $status.AutoSize = $false
    $status.Size = New-Object System.Drawing.Size(370, 36)
    $status.Location = New-Object System.Drawing.Point(26, 98)
    $form.Controls.Add($status)

    $form.Show()
    $form.Refresh()
    [System.Windows.Forms.Application]::DoEvents()
    return $form
}

function Open-AppWindow([string]$Url) {
    $edgeCandidates = @(
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    )
    $chromeCandidates = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )

    $browserExe = $null
    foreach ($c in ($edgeCandidates + $chromeCandidates)) {
        if (Test-Path $c) { $browserExe = $c; break }
    }

    if ($browserExe) {
        # Rimuovi eventuale profilo legacy in repo se nessun browser lo sta usando
        if (Test-Path $LegacyAppProfile) {
            $legacyRunning = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*launcher*.app-profile*" }
            if (-not $legacyRunning) {
                Remove-Item -Path $LegacyAppProfile -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        # Pulisci eventuali residui %TEMP%\LocalLLM-AppProfile-* di sessioni chiuse
        Get-ChildItem -Path ([System.IO.Path]::GetTempPath()) -Directory -Filter "LocalLLM-AppProfile-*" -ErrorAction SilentlyContinue | ForEach-Object {
            $dirName = $_.Name
            $inUse = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*$dirName*" }
            if (-not $inUse) {
                Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not (Test-Path $AppProfile)) {
            New-Item -ItemType Directory -Path $AppProfile -Force | Out-Null
        }
        $argsList = @(
            "--app=$Url",
            "--user-data-dir=`"$AppProfile`"",
            "--window-size=1440,920",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--disable-client-side-phishing-detection",
            "--disable-features=Translate,OptimizationHints,msEdgeShoppingAssistant,msEdgeSidebarV2,msImplicitSignIn,msWallet",
            "--no-pings",
            "--disable-crash-reporter",
            "--proxy-server=`"http://127.0.0.1:1`"",
            "--proxy-bypass-list=`"127.0.0.1;localhost;[::1]`"",
            "--disk-cache-size=1",
            "--media-cache-size=1",
            "--aggressive-cache-discard"
        )
        Start-Process -FilePath $browserExe -ArgumentList $argsList
    } else {
        Start-Process $Url
    }
}

function Ensure-TrayRunning {
    $pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
    if (-not $pwshExe) { $pwshExe = "powershell.exe" }

    $trayRunning = Get-CimInstance Win32_Process -Filter "Name LIKE 'pwsh%' OR Name LIKE 'powershell%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*launcher*tray-app.ps1*" }
    if (-not $trayRunning -and (Test-Path $TrayScript)) {
        Start-Process -FilePath $pwshExe -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", "`"$TrayScript`"") -WindowStyle Hidden
    }
}

function Resolve-DesktopPython {
    $venvPy = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $venvPyCli = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPyCli) { return $venvPyCli }
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) { return $pyCmd.Source }
    return $null
}

function Start-NativeDesktopWindow([bool]$GatewayAlreadyUp) {
    $pyExe = Resolve-DesktopPython
    if (-not $pyExe) { return $false }

    try {
        $probePy = $pyExe -replace 'pythonw\.exe$', 'python.exe'
        if (Test-Path $probePy) {
            & $probePy -c "import webview, workspaces.local_llm_core.desktop" 2>$null
            if ($LASTEXITCODE -ne 0) { return $false }
        }
    } catch { return $false }

    $argsList = @("-m", "workspaces.local_llm_core.desktop")
    if ($GatewayAlreadyUp) {
        $argsList += @("--attach-url", $UiUrl, "--no-mutex")
    }
    $env:PYTHONPATH = $ProjectRoot
    Start-Process -FilePath $pyExe -ArgumentList $argsList -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    return $true
}

$uiUp     = Test-LocalUrl -Url "$UiUrl/"
$ollamaUp = Test-LocalUrl -Url "http://127.0.0.1:11434/api/version"
$heavyUp  = Test-LocalUrl -Url "http://127.0.0.1:11435/health"

$splash = $null
$nativePythonAvailable = [bool](Resolve-DesktopPython)
$preferNativeDesktop = (-not $BrowserFallback) -and $nativePythonAvailable

try {
    if (-not $uiUp -or ($Mode -eq 'fast' -and -not $ollamaUp) -or ($Mode -eq 'heavy' -and -not $heavyUp) -or ($Mode -eq 'auto' -and -not $ollamaUp -and -not $heavyUp)) {
        $targetMode = if ($Mode -eq 'auto') {
            if ($heavyUp) { 'heavy' } else { 'fast' }
        } elseif ($Mode -eq 'ui-only') {
            'ui-only'
        } else {
            $Mode
        }

        $msg = if ($targetMode -eq 'heavy') {
            "Avvio Heavy MoE (Colibri) e Console Desktop..."
        } elseif ($targetMode -eq 'ui-only') {
            "Avvio Console Desktop Privata (motori in standby)..."
        } else {
            "Avvio Fast Tier (Ollama) e Console Desktop..."
        }
        $splash = Show-Splash -StatusText $msg

        if (-not ($preferNativeDesktop -and $targetMode -eq 'ui-only' -and -not $uiUp)) {
            $pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
            if (-not $pwshExe) { $pwshExe = "powershell.exe" }

            $startArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$StartScript`"", "-Mode", $targetMode)
            if (-not $preferNativeDesktop -or $uiUp) {
                $startArgs += "-WithUI"
            }
            $proc = Start-Process -FilePath $pwshExe -ArgumentList $startArgs -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
            while (-not $proc.HasExited) {
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 120
            }
        }
    }

    Ensure-TrayRunning

    if (-not $preferNativeDesktop) {
        # Attendi al massimo 5s se UI stava finendo il boot
        for ($i = 0; $i -lt 25; $i++) {
            if (Test-LocalUrl -Url "$UiUrl/") { break }
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 200
        }
    }
} finally {
    if ($splash) {
        $splash.Close()
        $splash.Dispose()
    }
}

$launchedNative = $false
if ($preferNativeDesktop) {
    $gatewayUpNow = Test-LocalUrl -Url "$UiUrl/"
    $launchedNative = Start-NativeDesktopWindow -GatewayAlreadyUp $gatewayUpNow
}

if (-not $launchedNative) {
    if (Test-LocalUrl -Url "$UiUrl/") {
        Open-AppWindow -Url $UiUrl
    } else {
        [System.Windows.Forms.MessageBox]::Show(
            "Impossibile avviare la Console Offline.`nEsegui .\scripts\check-env.ps1 per la diagnostica.",
            "Local LLM — Errore Avvio",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
}
