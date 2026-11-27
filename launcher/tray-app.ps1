<#
.SYNOPSIS
    Icona System Tray di Windows per il controllo rapido di Kryptos Core (100% offline).
.DESCRIPTION
    Single-instance tramite Mutex nominato. Permette con tasto destro o doppio clic:
      - Aprire la finestra App Kryptos Core
      - Switchare tra Fast Tier (Ollama) e Heavy MoE (Colibri)
      - Liberare la VRAM arrestando i motori
      - Controllare l'Orchestrator Autonomo (Pausa/Riprendi e Wipe Report RAM)
      - Consultare lo stato GPU/RAM/Backend/Orchestrator
      - Chiudere tutto (Orchestrator + App + Motori + Tray)
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

# Garantisce istanza unica nella System Tray / Desktop Shell
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\KryptosCore_SingleInstance_v1", [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

$ProjectRoot  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$IconPath     = Join-Path $PSScriptRoot "local-llm.ico"
$LaunchScript = Join-Path $PSScriptRoot "launch-app.ps1"
$StartScript  = Join-Path $ProjectRoot "scripts\start-llm.ps1"
$StopScript   = Join-Path $ProjectRoot "scripts\stop-llm.ps1"
$PwshExe      = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $PwshExe) { $PwshExe = "powershell.exe" }

$tray = New-Object System.Windows.Forms.NotifyIcon
if (Test-Path $IconPath) {
    $tray.Icon = New-Object System.Drawing.Icon($IconPath)
} else {
    $tray.Icon = [System.Drawing.SystemIcons]::Application
}
$tray.Text = "Kryptos Core — 100% Offline (Fast / Heavy MoE)"
$tray.Visible = $true

function Show-Notice([string]$Title, [string]$Body, [System.Windows.Forms.ToolTipIcon]$Icon = [System.Windows.Forms.ToolTipIcon]::Info) {
    $tray.BalloonTipTitle = $Title
    $tray.BalloonTipText  = $Body
    $tray.BalloonTipIcon  = $Icon
    $tray.ShowBalloonTip(3500)
}

function Get-GatewayToken {
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "KryptosCore\gateway.token")
        $candidates += (Join-Path $env:LOCALAPPDATA "LocalLLM\gateway.token")
    }
    foreach ($tokFile in $candidates) {
        if ($tokFile -and (Test-Path $tokFile)) {
            try {
                return (Get-Content -Path $tokFile -Raw -ErrorAction Stop).Trim()
            } catch {}
        }
    }
    return $null
}

function Invoke-LocalApi {
    param(
        [string]$Path,
        [string]$Method = "GET",
        [hashtable]$Body = $null,
        [int]$TimeoutSec = 3
    )
    $uri = "http://127.0.0.1:8787$Path"
    try {
        $headers = @{}
        $tok = Get-GatewayToken
        if ($tok) {
            $headers["Authorization"] = "Bearer $tok"
            $headers["X-Kryptos-Token"] = $tok
        }
        $params = @{
            Uri         = $uri
            Method      = $Method
            Headers     = $headers
            TimeoutSec  = $TimeoutSec
            ErrorAction = 'Stop'
        }
        if ($Method -eq "POST") {
            $jsonBody = if ($null -ne $Body) { ($Body | ConvertTo-Json -Compress) } else { "{}" }
            $params["ContentType"] = "application/json; charset=utf-8"
            $params["Body"] = $jsonBody
        }
        $data = Invoke-RestMethod @params
        return @{ Ok = $true; StatusCode = 200; Data = $data; Error = $null }
    } catch {
        $errMsg = $_.Exception.Message
        $statusCode = 0
        if ($_.Exception.Response) {
            try { $statusCode = [int]$_.Exception.Response.StatusCode } catch {}
        }
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try {
                $parsedErr = $_.ErrorDetails.Message | ConvertFrom-Json
                if ($parsedErr.error) { $errMsg = [string]$parsedErr.error }
            } catch {
                $errMsg = $_.ErrorDetails.Message
            }
        }
        return @{ Ok = $false; StatusCode = $statusCode; Data = $null; Error = $errMsg }
    }
}

function Invoke-BgScript([string[]]$ArgList, [string]$StartMsg, [string]$DoneMsg) {
    if ($StartMsg) { Show-Notice -Title "Local LLM" -Body $StartMsg }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PwshExe
    $psi.Arguments = ($ArgList -join " ")
    $psi.WorkingDirectory = $ProjectRoot
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    if ($DoneMsg -and $p) {
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 500
        $timer.Tag = @{ Proc = $p; Msg = $DoneMsg }
        $timer.Add_Tick({
            param($s, $e)
            $info = $s.Tag
            if ($info.Proc.HasExited) {
                $s.Stop()
                $s.Dispose()
                if ($info.Proc.ExitCode -eq 0) {
                    Show-Notice -Title "Local LLM" -Body $info.Msg
                } elseif ($info.Proc.ExitCode -eq 49) {
                    Show-Notice -Title "Local LLM — Orchestrator Attivo" -Body "Operazione bloccata (409): una run Orchestrator è in corso. Metti in pausa o attendi il completamento." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
                } else {
                    Show-Notice -Title "Local LLM — Attenzione" -Body "Operazione terminata con avviso (codice $($info.Proc.ExitCode)). Apri la Console per i dettagli." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
                }
            }
        })
        $timer.Start()
    }
}

function Invoke-EngineControl([string]$Action, [string[]]$FallbackArgs, [string]$StartMsg, [string]$DoneMsg) {
    $quotedArgs = ($FallbackArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ", "
    $tokPath = if ($env:LOCALAPPDATA) { (Join-Path $env:LOCALAPPDATA "LocalLLM\gateway.token") } else { "" }
    $psCmd = @"
`$tokFile = '$($tokPath -replace "'", "''")'
`$headers = @{}
if (`$tokFile -and (Test-Path `$tokFile)) {
    `$tok = (Get-Content -Path `$tokFile -Raw -ErrorAction SilentlyContinue).Trim()
    if (`$tok) { `$headers['Authorization'] = "Bearer `$tok" }
}
try {
    `$resp = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/control/engine' -Method POST -Headers `$headers -ContentType 'application/json' -Body '{"action":"$Action"}' -TimeoutSec 115 -ErrorAction Stop
    if (`$resp.ok -eq `$false) { exit 1 }
    exit 0
} catch {
    `$sc = 0
    if (`$_.Exception.Response) { try { `$sc = [int]`$_.Exception.Response.StatusCode } catch {} }
    if (`$sc -eq 409) { exit 49 }
    if (`$sc -eq 0) {
        `$fbArgs = @($quotedArgs)
        & '$($PwshExe -replace "'", "''")' @fbArgs
        exit `$LASTEXITCODE
    }
    exit 1
}
"@
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($psCmd))
    Invoke-BgScript -ArgList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded) -StartMsg $StartMsg -DoneMsg $DoneMsg
}

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$miOpen = New-Object System.Windows.Forms.ToolStripMenuItem("Apri Local LLM")
$miOpen.Font = New-Object System.Drawing.Font($miOpen.Font, [System.Drawing.FontStyle]::Bold)
$miOpen.Add_Click({
    Invoke-BgScript -ArgList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$LaunchScript`"", "-Mode", "auto") -StartMsg "" -DoneMsg ""
})

$miFast = New-Object System.Windows.Forms.ToolStripMenuItem("Attiva Fast Tier (Ollama)")
$miFast.Add_Click({
    Invoke-EngineControl -Action "start_fast" `
        -FallbackArgs @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StartScript, "-Mode", "fast", "-WithUI") `
        -StartMsg "Attivazione Fast Tier (Ollama)..." `
        -DoneMsg "Fast Tier (Ollama) ONLINE su 127.0.0.1:11434."
})

$miHeavy = New-Object System.Windows.Forms.ToolStripMenuItem("Attiva Heavy MoE (Colibri)")
$miHeavy.Add_Click({
    Invoke-EngineControl -Action "start_heavy" `
        -FallbackArgs @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StartScript, "-Mode", "heavy", "-WithUI") `
        -StartMsg "Scaricamento Ollama e caricamento Heavy MoE (20-40s)..." `
        -DoneMsg "Heavy MoE (Colibri) ONLINE su 127.0.0.1:11435."
})

$miFree = New-Object System.Windows.Forms.ToolStripMenuItem("Libera VRAM (Arresta Motori)")
$miFree.Add_Click({
    Invoke-EngineControl -Action "stop_all" `
        -FallbackArgs @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StopScript, "-KeepUI") `
        -StartMsg "Arresto motori LLM e liberazione VRAM..." `
        -DoneMsg "VRAM e RAM liberate (Console Web pronta)."
})

$miOrchPauseResume = New-Object System.Windows.Forms.ToolStripMenuItem("Orchestrator: Pausa / Riprendi")
$miOrchPauseResume.Add_Click({
    $stRes = Invoke-LocalApi -Path "/api/orchestrator/status" -Method "GET" -TimeoutSec 2
    if (-not $stRes.Ok) {
        if ($stRes.StatusCode -eq 0) {
            Show-Notice -Title "Orchestrator" -Body "Console Web non attiva su 127.0.0.1:8787. Avvia Local LLM prima." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        } else {
            Show-Notice -Title "Orchestrator" -Body "Stato non disponibile: $($stRes.Error)" -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        }
        return
    }

    $st = $stRes.Data
    $orchState = if ($st.state) { ([string]$st.state).ToUpper() } elseif ($st.phase) { ([string]$st.phase).ToUpper() } else { "IDLE" }
    $retention = if ($st.retention_mode) { ([string]$st.retention_mode).ToLower() } elseif ($st.retentionMode) { ([string]$st.retentionMode).ToLower() } else { "" }

    if ($retention -eq "ephemeral") {
        Show-Notice -Title "Orchestrator — Ephemeral" -Body "Modalita Ephemeral (Zero Disco): Pausa/Ripresa disabilitata per garantire zero scritture su disco." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        return
    }

    if ($orchState -eq "PAUSED") {
        $res = Invoke-LocalApi -Path "/api/orchestrator/resume" -Method "POST" -Body @{} -TimeoutSec 4
        if ($res.Ok -and ($res.Data.ok -ne $false)) {
            Show-Notice -Title "Orchestrator" -Body "Run Persistent ripresa dal checkpoint."
        } else {
            $msg = if ($res.Data -and $res.Data.error) { [string]$res.Data.error } else { [string]$res.Error }
            Show-Notice -Title "Orchestrator — Ripresa" -Body "Impossibile riprendere: $msg" -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        }
    } elseif ($st.active -or ($orchState -in @("STARTING", "RUNNING", "SCOUT", "STRATEGIST", "INVESTIGATOR", "VALIDATOR", "ADJUDICATOR"))) {
        $res = Invoke-LocalApi -Path "/api/orchestrator/pause" -Method "POST" -Body @{} -TimeoutSec 4
        if ($res.Ok -and ($res.Data.ok -ne $false)) {
            Show-Notice -Title "Orchestrator" -Body "Run messa in PAUSA (checkpoint salvato nel Vault)."
        } else {
            $msg = if ($res.Data -and $res.Data.error) { [string]$res.Data.error } else { [string]$res.Error }
            Show-Notice -Title "Orchestrator — Pausa" -Body "Impossibile mettere in pausa: $msg" -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        }
    } else {
        Show-Notice -Title "Orchestrator" -Body "Stato attuale: $orchState. Nessuna run attiva da mettere in pausa o riprendere."
    }
})

$miOrchPurge = New-Object System.Windows.Forms.ToolStripMenuItem("Orchestrator: Distruggi Report in RAM")
$miOrchPurge.Add_Click({
    $res = Invoke-LocalApi -Path "/api/orchestrator/purge-report" -Method "POST" -Body @{} -TimeoutSec 3
    if ($res.Ok -and ($res.Data.ok -ne $false)) {
        Show-Notice -Title "Orchestrator — Wipe RAM" -Body "Report distrutto: buffer RAM azzerato in sicurezza."
    } elseif ($res.StatusCode -eq 0) {
        Show-Notice -Title "Orchestrator" -Body "Console Web non attiva su 127.0.0.1:8787." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
    } else {
        $msg = if ($res.Data -and $res.Data.error) { [string]$res.Data.error } else { [string]$res.Error }
        Show-Notice -Title "Orchestrator — Wipe RAM" -Body "Esito purge report: $msg" -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
    }
})

$miStatus = New-Object System.Windows.Forms.ToolStripMenuItem("Stato GPU / RAM / Servizi")
$miStatus.Add_Click({
    try {
        $telRes = Invoke-LocalApi -Path "/api/system/telemetry" -Method "GET" -TimeoutSec 2
        if (-not $telRes.Ok -or -not $telRes.Data) {
            $errText = if ($telRes.Error) { [string]$telRes.Error } else { "offline" }
            throw $errText
        }
        $tel = $telRes.Data
        $vram = if ($tel.gpu.available) { "$($tel.gpu.memoryUsedGb)/$($tel.gpu.memoryTotalGb) GiB ($($tel.gpu.temperatureC)C)" } else { "N/D" }
        $ram  = "$($tel.ram.freeGb)/$($tel.ram.totalGb) GiB liberi"
        $fast = if ($tel.backends.ollama.online) { "ON" } else { "OFF" }
        $hv   = if ($tel.backends.heavyMoe.online) { "ON" } else { "OFF" }

        $orch = $tel.orchestrator
        if (-not $orch) {
            $stFallback = Invoke-LocalApi -Path "/api/orchestrator/status" -Method "GET" -TimeoutSec 2
            if ($stFallback.Ok) { $orch = $stFallback.Data }
        }

        $orchText = "IDLE"
        if ($orch) {
            $oState = if ($orch.state) { ([string]$orch.state).ToUpper() } else { "IDLE" }
            $oRet   = if ($orch.retentionMode) { ([string]$orch.retentionMode).ToLower() } elseif ($orch.retention_mode) { ([string]$orch.retention_mode).ToLower() } else { "" }
            $oStep  = if ($null -ne $orch.step) { [int]$orch.step } elseif ($null -ne $orch.current_step) { [int]$orch.current_step } else { 0 }
            $oHasRep = [bool]($orch.hasReport -or $orch.has_report)
            $oActive = [bool]$orch.active

            if ($oState -eq "PAUSED") {
                $orchText = "PAUSED (Step $oStep)"
            } elseif ($oActive) {
                $orchText = "RUNNING ($oState · Step $oStep)"
            } elseif ($oHasRep -and $oRet -eq "ephemeral") {
                $orchText = "EPHEMERAL REPORT IN RAM"
            } elseif ($oHasRep) {
                $orchText = "REPORT PRONTO ($oState)"
            } else {
                $orchText = $oState
            }
        }

        Show-Notice -Title "Local LLM — Stato Live" -Body "Fast: $fast | Heavy MoE: $hv`nVRAM: $vram`nRAM: $ram`nOrchestrator: $orchText"
    } catch {
        Show-Notice -Title "Local LLM — Stato" -Body "Console Web non attiva. Fai doppio clic sull'icona per avviare." -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
    }
})

$miFolder = New-Object System.Windows.Forms.ToolStripMenuItem("Apri Cartella Progetto")
$miFolder.Add_Click({
    Start-Process "explorer.exe" "`"$ProjectRoot`""
})

$miExit = New-Object System.Windows.Forms.ToolStripMenuItem("Chiudi Tutto (Spegni App & Motori)")
$miExit.Add_Click({
    if ($tray) { $tray.Visible = $false }
    Invoke-LocalApi -Path "/api/orchestrator/stop" -Method "POST" -Body @{} -TimeoutSec 1 | Out-Null
    Invoke-LocalApi -Path "/api/orchestrator/purge-report" -Method "POST" -Body @{} -TimeoutSec 1 | Out-Null
    Start-Process -FilePath $PwshExe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$StopScript`"") -WorkingDirectory $ProjectRoot -WindowStyle Hidden
    [System.Windows.Forms.Application]::Exit()
})

$menu.Items.Add($miOpen)            | Out-Null
$menu.Items.Add("-")                | Out-Null
$menu.Items.Add($miFast)            | Out-Null
$menu.Items.Add($miHeavy)           | Out-Null
$menu.Items.Add($miFree)            | Out-Null
$menu.Items.Add("-")                | Out-Null
$menu.Items.Add($miOrchPauseResume) | Out-Null
$menu.Items.Add($miOrchPurge)       | Out-Null
$menu.Items.Add("-")                | Out-Null
$menu.Items.Add($miStatus)          | Out-Null
$menu.Items.Add($miFolder)          | Out-Null
$menu.Items.Add("-")                | Out-Null
$menu.Items.Add($miExit)            | Out-Null

$tray.ContextMenuStrip = $menu
$tray.Add_DoubleClick({
    Invoke-BgScript -ArgList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$LaunchScript`"", "-Mode", "auto") -StartMsg "" -DoneMsg ""
})

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    if ($tray) { $tray.Visible = $false; $tray.Dispose() }
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
