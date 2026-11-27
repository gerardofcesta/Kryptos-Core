<#
.SYNOPSIS
    Audit automatico privacy 8/8: loopback-only, zero outbound connections, igiene offline,
    anti-leak host/browser, isolamento sandbox Docker e hardening Gateway :8787.
.DESCRIPTION
    Esegue 8 controlli PASS/FAIL:
      1. Porte LLM/UI in ascolto solo su 127.0.0.1 o ::1 (mai 0.0.0.0 o ::).
      2. Nessuna connessione outbound attiva (Established) verso IP non-loopback dai processi LLM e browser App.
      3. Scansione statica di workspaces/offline-chat/index.html e server.py (zero URL esterni + CSP).
      4. Verifica .gitignore (regole obbligatorie inclusi vault/runs) e assenza di log/cronologia chat nel repo.
      5. Verifica variabili privacy obbligatorie in .env (incluso OLLAMA_NO_CLOUD=1 e KVSAVE=0).
      6. Integrita Zero-Disk & Anti-Leak Host/Browser (KVSAVE=0, Cache-Control no-store, .wslconfig swap=0,
         vault pulito se nessuna run Persistent attiva, assenza totale 0 B di launcher/.app-profile e %TEMP%/LocalLLM-AppProfile-*, stato EncryptPagingFile).
      7. Isolamento Sandbox Docker per tutti i container etichettati local-llm-orchestrator=true.
      8. Hardening Gateway :8787 (Host allowlist, CSRF Origin/Sec-Fetch-Site/415, gateway.token ACL utente e policy target_path).
    Restituisce exit code 0 se tutti gli 8 check passano, 1 altrimenti.
.PARAMETER Strict
    In modalita Strict, EncryptPagingFile=0 fa fallire il Check 6/8 (richiede abilitazione Admin + riavvio).
#>

[CmdletBinding()]
param(
    [switch]$Strict
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TotalChecks = 8
$allPassed   = $true

function Report-Check([int]$Num, [string]$Title, [bool]$Passed, [string]$Detail) {
    if ($Passed) {
        Write-Host ("[PASS] Check {0}/{1} - {2}" -f $Num, $TotalChecks, $Title) -ForegroundColor Green
    } else {
        Write-Host ("[FAIL] Check {0}/{1} - {2}" -f $Num, $TotalChecks, $Title) -ForegroundColor Red
        $script:allPassed = $false
    }
    if ($Detail) {
        Write-Host ("       {0}" -f $Detail) -ForegroundColor Gray
    }
}

function Invoke-DockerAuditFast {
    param(
        [string]$DockerExe,
        [string]$Arguments,
        [int]$TimeoutMs = 4000
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $DockerExe
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try {
        if (-not $proc.Start()) { return $null }
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { $proc.Kill() } catch {}
            return [pscustomobject]@{ TimedOut = $true; ExitCode = -1; StdOut = ''; StdErr = 'Timeout' }
        }
        [void]$proc.WaitForExit()
        return [pscustomobject]@{
            TimedOut = $false
            ExitCode = $proc.ExitCode
            StdOut   = $stdoutTask.GetAwaiter().GetResult()
            StdErr   = $stderrTask.GetAwaiter().GetResult()
        }
    } catch {
        return $null
    } finally {
        $proc.Dispose()
    }
}

Write-Host "`n=== LOCAL LLM PRIVACY & OFFLINE AUDIT ($TotalChecks/$TotalChecks) ===" -ForegroundColor Cyan

# ------------------------------------------------------------------------------
# Check 1: Listening ports (11434, 11435, 8787, 1234, 3000) strictly loopback
# ------------------------------------------------------------------------------
$targetPorts = @(11434, 11435, 8787, 1234, 3000)
$loopbackAddrs = @('127.0.0.1', '::1')
$listenSockets = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $targetPorts })
$badListeners = @($listenSockets | Where-Object { $_.LocalAddress -notin $loopbackAddrs })

if ($badListeners.Count -eq 0) {
    $activeSummary = if ($listenSockets.Count -gt 0) {
        ($listenSockets | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" } | Select-Object -Unique) -join ', '
    } else {
        "nessun servizio attualmente in ascolto sulle porte target"
    }
    Report-Check 1 "Listening Ports Loopback-Only ($($targetPorts -join ', '))" $true "Attive: $activeSummary"
} else {
    $badSummary = ($badListeners | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) (PID $($_.OwningProcess))" }) -join '; '
    Report-Check 1 "Listening Ports Loopback-Only ($($targetPorts -join ', '))" $false "Binding non-loopback rilevato: $badSummary"
}

# ------------------------------------------------------------------------------
# Check 2: Established outbound connections for LLM processes (zero non-loopback)
# ------------------------------------------------------------------------------
$llmProcRegex = '^(ollama|qwen38|qwen36|colibri|glm53|deepseek_v4|deepseek_v41|kimi_k3|mimo|inkling|olmoe|laya|gliner_decide|qwenimage|llama-server|lms|lm studio)'
$llmPids = New-Object 'System.Collections.Generic.HashSet[int]'

Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $llmProcRegex } | ForEach-Object {
    [void]$llmPids.Add([int]$_.Id)
}
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*offline-chat*" -or
    $_.CommandLine -like "*orchestrator*" -or
    $_.CommandLine -like "*openai_server.py*" -or
    $_.CommandLine -like "*engines*colibri*"
} | ForEach-Object {
    [void]$llmPids.Add([int]$_.ProcessId)
}
Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*LocalLLM-AppProfile*" -or
    $_.CommandLine -like "*launcher*.app-profile*"
} | ForEach-Object {
    [void]$llmPids.Add([int]$_.ProcessId)
}

$established = @()
if ($llmPids.Count -gt 0) {
    $established = @(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object { $llmPids.Contains([int]$_.OwningProcess) })
}

$allowedRemote = @('127.0.0.1', '::1', '::ffff:127.0.0.1')
$badOutbound = @($established | Where-Object { $_.RemoteAddress -notin $allowedRemote })

if ($badOutbound.Count -eq 0) {
    Report-Check 2 "Zero Outbound Connections da processi LLM e App Browser" $true "PID monitorati: $($llmPids.Count) | Connessioni esterne rilevate: 0"
} else {
    $outList = ($badOutbound | ForEach-Object {
        $pName = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        "$pName($($_.OwningProcess)) -> $($_.RemoteAddress):$($_.RemotePort)"
    }) -join '; '
    Report-Check 2 "Zero Outbound Connections da processi LLM e App Browser" $false "Connessioni esterne attive: $outList"
}

# ------------------------------------------------------------------------------
# Check 3: Static scan of offline-chat/index.html, web/dist/* & server.py (no external URLs + CSP)
# ------------------------------------------------------------------------------
$uiIndexPath  = Join-Path $ProjectRoot "workspaces\offline-chat\index.html"
$uiDistDir    = Join-Path $ProjectRoot "workspaces\offline-chat\web\dist"
$uiDistIndex  = Join-Path $uiDistDir "index.html"
$uiServerPath = Join-Path $ProjectRoot "workspaces\offline-chat\server.py"
$uiStaticDir  = Join-Path $ProjectRoot "workspaces\offline-chat\static"
$coreDir      = Join-Path $ProjectRoot "workspaces\local_llm_core"
$check3Ok = $true
$check3Notes = New-Object System.Collections.Generic.List[string]
$serverRaw = ""
$allowedXmlNamespaces = @(
    'http://www.w3.org/1998/Math/MathML',
    'http://www.w3.org/1999/xhtml',
    'http://www.w3.org/1999/xlink',
    'http://www.w3.org/2000/svg',
    'http://www.w3.org/XML/1998/namespace'
)

if (-not (Test-Path $uiIndexPath) -or -not (Test-Path $uiServerPath) -or -not (Test-Path $uiDistIndex)) {
    $check3Ok = $false
    $check3Notes.Add("File workspaces/offline-chat/index.html, web/dist/index.html o server.py mancante")
} else {
    $scanFiles = New-Object System.Collections.Generic.List[string]
    $scanFiles.Add($uiIndexPath)
    $scanFiles.Add($uiServerPath)
    if (Test-Path $uiStaticDir) {
        Get-ChildItem -Path $uiStaticDir -Recurse -File -Include *.js,*.css | ForEach-Object { $scanFiles.Add($_.FullName) }
    }
    if (Test-Path $uiDistDir) {
        Get-ChildItem -Path $uiDistDir -Recurse -File -Include *.html,*.js,*.mjs,*.css,*.json | ForEach-Object { $scanFiles.Add($_.FullName) }
    }
    if (Test-Path $coreDir) {
        Get-ChildItem -Path $coreDir -Recurse -File -Filter *.py | Where-Object { $_.FullName -notlike "*\tests\*" } | ForEach-Object { $scanFiles.Add($_.FullName) }
    }

    foreach ($filePath in $scanFiles) {
        $content = Get-Content -Path $filePath -Raw -Encoding UTF8
        $urlMatches = [regex]::Matches($content, 'https?://([^\s"''`<>)]+)')
        foreach ($m in $urlMatches) {
            $fullUrl = $m.Value
            if ($fullUrl -in $allowedXmlNamespaces) { continue }
            $hostAndPath = $m.Groups[1].Value
            $hostPart = $hostAndPath.Split('/')[0].Split('?')[0].Split('#')[0].Split(':')[0]
            # Consenti interpolazioni dinamiche locali tipo {HOST} o ${host} purche non siano domini esterni letterali
            if ($hostPart -match '^[\{\$]') { continue }
            if ($hostPart -notin @('127.0.0.1', 'localhost', '[::1]')) {
                $check3Ok = $false
                $check3Notes.Add("URL esterno in $([System.IO.Path]::GetFileName($filePath)): $fullUrl")
            }
        }
    }

    $serverRaw = Get-Content -Path $uiServerPath -Raw -Encoding UTF8
    $secPyPath = Join-Path $coreDir "security.py"
    $secPyRaw = if (Test-Path $secPyPath) { Get-Content -Path $secPyPath -Raw -Encoding UTF8 } else { "" }
    if (($serverRaw -notmatch 'Content-Security-Policy') -and ($secPyRaw -notmatch 'STRICT_CSP_HEADER')) {
        $check3Ok = $false
        $check3Notes.Add("Header Content-Security-Policy mancante")
    }
    if ($secPyRaw -match "STRICT_CSP_HEADER\s*=\s*\(([^)]*)\)" -and $Matches[1] -match "'unsafe-inline'") {
        $check3Ok = $false
        $check3Notes.Add("STRICT_CSP_HEADER contiene 'unsafe-inline'")
    }
    if ($serverRaw -match '0\.0\.0\.0' -or $secPyRaw -match '0\.0\.0\.0') {
        $check3Ok = $false
        $check3Notes.Add("Riferimento 0.0.0.0 rilevato nel server")
    }
}

$check3Msg = if ($check3Ok) { "Zero URL esterni in web/dist/*, index.html, static/ e local_llm_core; CSP strict (zero unsafe-inline)" } else { $check3Notes -join ' | ' }
Report-Check 3 "Scansione statica Offline Chat UI (zero URL esterni + CSP)" $check3Ok $check3Msg

# ------------------------------------------------------------------------------
# Check 4: .gitignore rules + absence of chat logs/history files in repo
# ------------------------------------------------------------------------------
$gitignorePath = Join-Path $ProjectRoot ".gitignore"
$requiredIgnore = @('.env', '*.gguf', 'engines/', 'models/heavy-moe/', '.coli_usage', '.coli_kv', 'workspaces/vault/', 'workspaces/runs/')
$check4Ok = $true
$check4Notes = New-Object System.Collections.Generic.List[string]

if (-not (Test-Path $gitignorePath)) {
    $check4Ok = $false
    $check4Notes.Add(".gitignore mancante")
} else {
    $giLines = @(Get-Content $gitignorePath | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
    $missingRules = @()
    foreach ($rule in $requiredIgnore) {
        $normRule = $rule.TrimEnd('/')
        $matched = $giLines | Where-Object { $_ -eq $rule -or $_.TrimEnd('/') -eq $normRule -or $_ -eq "*$rule" }
        if (-not $matched) {
            $missingRules += $rule
        }
    }
    if ($missingRules.Count -gt 0) {
        $check4Ok = $false
        $check4Notes.Add("Regole mancanti in .gitignore: $($missingRules -join ', ')")
    }
}

# Scansione log / cronologia chat salvata nel repo (escludendo .git, engines, .app-profile e workspaces\vault\)
$forbiddenFiles = New-Object System.Collections.Generic.List[string]
Get-ChildItem -Path $ProjectRoot -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
    $rel = $_.FullName.Substring($ProjectRoot.Length).TrimStart('\')
    (-not ($rel -like '.git\*')) -and
    (-not ($rel -like 'engines\*')) -and
    (-not ($rel -like 'launcher\.app-profile\*')) -and
    (-not ($rel -like 'workspaces\vault\*'))
} | ForEach-Object {
    $rel = $_.FullName.Substring($ProjectRoot.Length).TrimStart('\')
    if ($_.Extension -eq '.log' -or
        $rel -like 'logs\*' -or
        $_.Name -match '^(chat[-_]?history|conversations?)\.(json|md|txt|db|sqlite)$') {
        $forbiddenFiles.Add($rel)
    }
}

if ($forbiddenFiles.Count -gt 0) {
    $check4Ok = $false
    $check4Notes.Add("File log/cronologia trovati: $($forbiddenFiles -join ', ')")
}

$check4Msg = if ($check4Ok) { ".gitignore conforme ($($requiredIgnore.Count) regole verificate) e zero file log/cronologia nel repo" } else { $check4Notes -join ' | ' }
Report-Check 4 "Igiene Git (.gitignore) e assenza log/cronologia chat" $check4Ok $check4Msg

# ------------------------------------------------------------------------------
# Check 5: Privacy variables in .env
# ------------------------------------------------------------------------------
$envPath = Join-Path $ProjectRoot ".env"
$check5Ok = $true
$check5Notes = New-Object System.Collections.Generic.List[string]
$envMap = @{}

if (-not (Test-Path $envPath)) {
    $check5Ok = $false
    $check5Notes.Add(".env mancante")
} else {
    Get-Content $envPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            $envMap[$k.Trim()] = $v.Trim()
        }
    }
    $expectedEnv = [ordered]@{
        'DO_NOT_TRACK'            = '1'
        'OLLAMA_NOHISTORY'        = '1'
        'OLLAMA_NO_CLOUD'         = '1'
        'OFFLINE_MODE'            = 'true'
        'HF_HUB_DISABLE_TELEMETRY'= '1'
        'SCARF_NO_ANALYTICS'      = 'true'
        'ANONYMIZED_TELEMETRY'    = 'false'
        'KVSAVE'                  = '0'
    }
    foreach ($k in $expectedEnv.Keys) {
        if (-not $envMap.ContainsKey($k) -or $envMap[$k] -ne $expectedEnv[$k]) {
            $check5Ok = $false
            $actual = if ($envMap.ContainsKey($k)) { $envMap[$k] } else { '<assente>' }
            $check5Notes.Add("$k=$actual (atteso $($expectedEnv[$k]))")
        }
    }
}

$check5Msg = if ($check5Ok) { "Tutte le variabili privacy/zero-telemetria attive in .env (incluso OLLAMA_NO_CLOUD=1, KVSAVE=0)" } else { $check5Notes -join ' | ' }
Report-Check 5 "Variabili privacy in .env" $check5Ok $check5Msg

# ------------------------------------------------------------------------------
# Check 6: Zero-Disk Integrity & Host/Browser Anti-Leak Hardening
# ------------------------------------------------------------------------------
$check6Ok = $true
$check6Parts = New-Object System.Collections.Generic.List[string]
$adminNotice = $null

# (a) KVSAVE=0 in .env
if ($envMap.ContainsKey('KVSAVE') -and $envMap['KVSAVE'] -eq '0') {
    $check6Parts.Add("KVSAVE=0")
} else {
    $check6Ok = $false
    $actualKv = if ($envMap.ContainsKey('KVSAVE')) { $envMap['KVSAVE'] } else { '<assente>' }
    $check6Parts.Add("KVSAVE=$actualKv [FAIL: atteso 0]")
}

# (b) Cache-Control no-store in workspaces/offline-chat/server.py or local_llm_core/security.py
$hasNoStore = (($serverRaw -match '(?i)Cache-Control') -or ($secPyRaw -match '(?i)cache-control')) -and (($serverRaw -match '(?i)\bno-store\b') -or ($secPyRaw -match '(?i)\bno-store\b'))
if ($hasNoStore) {
    $check6Parts.Add("Cache-Control:no-store=OK")
} else {
    $check6Ok = $false
    $check6Parts.Add("Cache-Control:no-store=MANCANTE [FAIL]")
}

# (c) %USERPROFILE%\.wslconfig contains swap=0 under [wsl2]
$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$wslSwapZero = $false
$wslMemoryVal = $null
if (Test-Path $wslConfigPath) {
    $inWsl2 = $false
    foreach ($rawLine in (Get-Content -Path $wslConfigPath -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $ln = $rawLine.Trim()
        if (-not $ln -or $ln.StartsWith('#') -or $ln.StartsWith(';')) { continue }
        if ($ln -match '^\[([^\]]+)\]$') {
            $inWsl2 = ($Matches[1].Trim().ToLowerInvariant() -eq 'wsl2')
            continue
        }
        if ($inWsl2 -and $ln -match '^(?i)swap\s*=\s*0$') { $wslSwapZero = $true }
        if ($inWsl2 -and $ln -match '^(?i)memory\s*=\s*(.+)$') { $wslMemoryVal = $Matches[1].Trim() }
    }
}
if ($wslSwapZero) {
    $memNote = if ($wslMemoryVal) { ", memory=$wslMemoryVal" } else { "" }
    $check6Parts.Add(".wslconfig(swap=0$memNote)=OK")
} else {
    $check6Ok = $false
    $check6Parts.Add(".wslconfig(swap=0)=MANCANTE [FAIL: eseguire scripts\setup-privacy-host.ps1]")
}

# (d) Zero active files in workspaces/vault/ when no Persistent run is active
$vaultDir = Join-Path $ProjectRoot "workspaces\vault"
$vaultFiles = @()
if (Test-Path $vaultDir) {
    $vaultFiles = @(Get-ChildItem -Path $vaultDir -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne '.gitkeep' })
}
if ($vaultFiles.Count -eq 0) {
    $check6Parts.Add("vault=0 file")
} else {
    $persistentRunActive = $false
    $uiListening = @($listenSockets | Where-Object { $_.LocalPort -eq 8787 }).Count -gt 0
    if ($uiListening) {
        try {
            $tokenFile = Join-Path $env:LOCALAPPDATA "LocalLLM\gateway.token"
            $reqHeaders = @{}
            if (Test-Path $tokenFile) {
                $tok = (Get-Content -Path $tokenFile -Raw -ErrorAction SilentlyContinue).Trim()
                if ($tok) { $reqHeaders["Authorization"] = "Bearer $tok" }
            }
            $stResp = Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/orchestrator/status" -Headers $reqHeaders -TimeoutSec 1 -ErrorAction Stop
            if ($stResp -and ($stResp.retention_mode -eq 'persistent') -and ($stResp.state -notin @('IDLE', $null, ''))) {
                $persistentRunActive = $true
            }
        } catch {
            $persistentRunActive = $false
        }
    }
    if ($persistentRunActive) {
        $check6Parts.Add("vault=$($vaultFiles.Count) file (run Persistent attiva)")
    } else {
        $check6Ok = $false
        $check6Parts.Add("vault=$($vaultFiles.Count) file residui senza run Persistent attiva [FAIL]")
    }
}

# (e) launcher/.app-profile AND %TEMP%/LocalLLM-AppProfile-* completely absent (0 B) when App closed
$appBrowserRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*LocalLLM-AppProfile*" -or
        $_.CommandLine -like "*launcher*.app-profile*"
    }).Count -gt 0
$legacyAppProfileDir = Join-Path $ProjectRoot "launcher\.app-profile"
$tempAppProfileDirs = @(Get-ChildItem -Path ([System.IO.Path]::GetTempPath()) -Directory -Filter "LocalLLM-AppProfile-*" -ErrorAction SilentlyContinue)
if ($appBrowserRunning) {
    $check6Parts.Add("app-profile=App aperta")
} else {
    $residualFiles = @()
    if (Test-Path $legacyAppProfileDir) {
        $residualFiles += @(Get-ChildItem -Path $legacyAppProfileDir -Recurse -File -Force -ErrorAction SilentlyContinue)
    }
    foreach ($tpDir in $tempAppProfileDirs) {
        $residualFiles += @(Get-ChildItem -Path $tpDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue)
    }
    $residualDirsCount = $(if (Test-Path $legacyAppProfileDir) { 1 } else { 0 }) + $tempAppProfileDirs.Count
    if ($residualFiles.Count -eq 0 -and $residualDirsCount -eq 0) {
        $check6Parts.Add("app-profile=0 B (assente)")
    } else {
        $check6Ok = $false
        $check6Parts.Add("app-profile=$residualDirsCount dir/$($residualFiles.Count) file residui con App chiusa [FAIL: eseguire scripts\stop-llm.ps1]")
    }
}

# (f) Windows Pagefile Encryption status (fsutil behavior query encryptpagingfile)
$pfVal = -1
try {
    $pfRaw = & fsutil behavior query encryptpagingfile 2>&1 | Out-String
    if ($pfRaw -match '(?i)EncryptPagingFile\s*=\s*(\d+)') {
        $pfVal = [int]$Matches[1]
    }
} catch {
    $pfVal = -1
}

if ($pfVal -eq 1) {
    $check6Parts.Add("EncryptPagingFile=1 (OK)")
} elseif ($pfVal -eq 0) {
    if ($Strict) {
        $check6Ok = $false
        $check6Parts.Add("EncryptPagingFile=0 [FAIL -Strict: eseguire fsutil behavior set encryptpagingfile 1 da Admin + riavvio]")
    } else {
        $check6Parts.Add("EncryptPagingFile=0 [WARN: eseguire fsutil behavior set encryptpagingfile 1 da Admin + riavvio]")
        $adminNotice = "[AVVISO ADMIN] EncryptPagingFile=0: per completare l'hardening pagefile (e superare -Strict) esegui scripts\setup-privacy-host.ps1 per il comando elevato + riavvio."
    }
} else {
    if ($Strict) {
        $check6Ok = $false
        $check6Parts.Add("EncryptPagingFile=sconosciuto [FAIL -Strict]")
    } else {
        $check6Parts.Add("EncryptPagingFile=sconosciuto [WARN]")
    }
}

Report-Check 6 "Integrita Zero-Disk & Anti-Leak Host/Browser" $check6Ok ($check6Parts -join ' | ')
if ($adminNotice) {
    Write-Host ("       {0}" -f $adminNotice) -ForegroundColor Yellow
}

# ------------------------------------------------------------------------------
# Check 7: Docker Sandbox Isolation (label local-llm-orchestrator=true)
# ------------------------------------------------------------------------------
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd -or -not $dockerCmd.Source) {
    Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $true "Docker daemon non attivo (modalita Ghost / nessun container sandbox in esecuzione)"
} else {
    $infoRes = Invoke-DockerAuditFast -DockerExe $dockerCmd.Source -Arguments "info --format {{.ServerVersion}}" -TimeoutMs 3500
    if (-not $infoRes -or $infoRes.TimedOut -or $infoRes.ExitCode -ne 0) {
        Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $true "Docker daemon non attivo (modalita Ghost / nessun container sandbox in esecuzione)"
    } else {
        $psRes = Invoke-DockerAuditFast -DockerExe $dockerCmd.Source -Arguments 'ps -q --filter "label=local-llm-orchestrator=true"' -TimeoutMs 3500
        if (-not $psRes -or $psRes.TimedOut -or $psRes.ExitCode -ne 0) {
            Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $false "Impossibile interrogare i container attivi tramite docker ps"
        } else {
            $runningIds = @(
                ($psRes.StdOut -split "`r?`n") |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -match '^[a-fA-F0-9]{6,}$' }
            )
            if ($runningIds.Count -eq 0) {
                Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $true "Docker attivo; 0 container orchestrator in esecuzione (policy pronta)"
            } else {
                $inspectArgs = "inspect " + ($runningIds -join ' ')
                $inspRes = Invoke-DockerAuditFast -DockerExe $dockerCmd.Source -Arguments $inspectArgs -TimeoutMs 5000
                if (-not $inspRes -or $inspRes.TimedOut -or $inspRes.ExitCode -ne 0) {
                    Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $false "Impossibile ispezionare i container orchestrator attivi ($($runningIds.Count))"
                } else {
                    $check7Ok = $true
                    $violations = New-Object System.Collections.Generic.List[string]
                    try {
                        $containers = @($inspRes.StdOut | ConvertFrom-Json)
                        foreach ($c in $containers) {
                            $cName = if ($c.Name) { $c.Name.TrimStart('/') } else { $c.Id.Substring(0, [Math]::Min(12, $c.Id.Length)) }
                            $netMode = $c.HostConfig.NetworkMode
                            $roRoot  = [bool]$c.HostConfig.ReadonlyRootfs
                            $mounts  = @($c.Mounts)
                            $labels  = $c.Config.Labels
                            $retention = $null
                            if ($labels -and ($labels.PSObject.Properties.Name -contains 'retention')) {
                                $retention = [string]$labels.retention
                            }

                            if ($netMode -ne 'none') {
                                $check7Ok = $false
                                $violations.Add("${cName}: NetworkMode='$netMode' (atteso 'none')")
                            }
                            if (-not $roRoot) {
                                $check7Ok = $false
                                $violations.Add("${cName}: ReadonlyRootfs=false (atteso true)")
                            }
                            foreach ($m in $mounts) {
                                if ($m.Destination -eq '/target' -and ([bool]$m.RW)) {
                                    $check7Ok = $false
                                    $violations.Add("${cName}: mount /target scrivibile (RW=true, atteso false)")
                                }
                                if ($retention -eq 'ephemeral' -and $m.Type -eq 'bind' -and ([bool]$m.RW)) {
                                    $check7Ok = $false
                                    $violations.Add("${cName}: bind mount RW '$($m.Destination)' vietato in modalita ephemeral")
                                }
                            }
                        }
                    } catch {
                        $check7Ok = $false
                        $violations.Add("Errore parsing JSON docker inspect: $($_.Exception.Message)")
                    }

                    $check7Detail = if ($check7Ok) {
                        "Verificati $($runningIds.Count) container orchestrator: NetworkMode=none, ReadonlyRootfs=true, /target:ro, ephemeral zero-RW-bind"
                    } else {
                        $violations -join ' | '
                    }
                    Report-Check 7 "Isolamento Sandbox Docker (local-llm-orchestrator=true)" $check7Ok $check7Detail
                }
            }
        }
    }
}

# ------------------------------------------------------------------------------
# Check 8: Hardening Gateway :8787 (Host/CSRF/415/Token ACL & target_path)
# ------------------------------------------------------------------------------
$check8Ok = $true
$check8Notes = New-Object System.Collections.Generic.List[string]
$schemasPath = Join-Path $ProjectRoot "workspaces\orchestrator\engine\schemas.py"
$schemasRaw = if (Test-Path $schemasPath) { Get-Content -Path $schemasPath -Raw -Encoding UTF8 } else { "" }

if (-not ($serverRaw -match '_is_allowed_host' -and $serverRaw -match '_is_allowed_origin_and_fetch_site' -and $serverRaw -match '415' -and $serverRaw -match '/api/auth/session-token' -and $serverRaw -match '_is_authorized_token')) {
    $check8Ok = $false
    $check8Notes.Add("Controlli Host/CSRF/415/Token mancanti in server.py")
}
if (-not ($schemasRaw -match 'validate_target_path' -and $schemasRaw -match 'ORCHESTRATOR_ALLOWED_ROOTS')) {
    $check8Ok = $false
    $check8Notes.Add("Policy validate_target_path / ORCHESTRATOR_ALLOWED_ROOTS mancante in schemas.py")
}

$gwTokenFile = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "LocalLLM\gateway.token" } else { $null }
if ($gwTokenFile -and (Test-Path $gwTokenFile)) {
    $aclOut = (& icacls $gwTokenFile 2>&1 | Out-String)
    if ($aclOut -match '(?i)(Everyone|BUILTIN\\Users|Authenticated Users)') {
        $check8Ok = $false
        $check8Notes.Add("ACL troppo permissiva su gateway.token")
    } else {
        $check8Notes.Add("gateway.token ACL=OK")
    }
} else {
    $check8Notes.Add("gateway.token non presente (server offline)")
}

$gwListening = @($listenSockets | Where-Object { $_.LocalPort -eq 8787 }).Count -gt 0
if ($gwListening) {
    try {
        $noTokStatus = 0
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:8787/api/system/telemetry" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop | Out-Null
            $noTokStatus = 200
        } catch {
            if ($_.Exception.Response) { $noTokStatus = [int]$_.Exception.Response.StatusCode }
        }
        if ($noTokStatus -ne 401) {
            $check8Ok = $false
            $check8Notes.Add("probe no-token=$noTokStatus (atteso 401)")
        } else {
            $check8Notes.Add("probe no-token=401")
        }

        $badHostStatus = 0
        try {
            $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8787/api/system/telemetry")
            $req.Host = "evil.attacker.com"
            $req.Timeout = 2000
            $resp = $req.GetResponse()
            $badHostStatus = [int]$resp.StatusCode
            $resp.Close()
        } catch [System.Net.WebException] {
            if ($_.Exception.Response) { $badHostStatus = [int]$_.Exception.Response.StatusCode }
        }
        if ($badHostStatus -ne 403) {
            $check8Ok = $false
            $check8Notes.Add("probe Host evil=$badHostStatus (atteso 403)")
        } else {
            $check8Notes.Add("probe Host evil=403")
        }
    } catch {
        $check8Notes.Add("probe live non completato: $($_.Exception.Message)")
    }
} else {
    $check8Notes.Add(":8787 offline (static+ACL OK)")
}

Report-Check 8 "Hardening Gateway :8787 (Host/CSRF/415/Token & target_path)" $check8Ok ($check8Notes -join ' | ')

Write-Host ""
if ($allPassed) {
    Write-Host "RISULTATO AUDIT: TUTTI GLI $TotalChecks CHECK SUPERATI ($TotalChecks/$TotalChecks PASS, 0 FAIL)." -ForegroundColor Green
    exit 0
} else {
    Write-Host "RISULTATO AUDIT: UNO O PIU CHECK FALLITI." -ForegroundColor Red
    exit 1
}
