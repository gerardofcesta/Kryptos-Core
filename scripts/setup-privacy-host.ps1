<#
.SYNOPSIS
    Hardening privacy dell'host Windows 11 & WSL2 (anti-swap leak, headroom Heavy MoE, cifratura pagefile).
.DESCRIPTION
    1. Installa o aggiorna in modo sicuro %USERPROFILE%\.wslconfig a partire da config\wslconfig.template,
       creando un backup automatico se il file esiste gia e preservando le sezioni non correlate
       mentre garantisce `swap=0` e `memory=3GB` sotto `[wsl2]`.
    2. Ispeziona `fsutil behavior query encryptpagingfile`:
       - Se gia `1`: conferma stato attivo.
       - Se `0` e shell elevata + `-ApplyPagefileEncryption`: abilita cifratura pagefile (richiede riavvio).
       - Se `0` e shell non elevata: non aggira mai UAC e stampa il comando Admin esatto da eseguire.
.PARAMETER ApplyWslConfig
    Applica o aggiorna %USERPROFILE%\.wslconfig (comportamento predefinito quando -CheckOnly non e impostato).
.PARAMETER ApplyPagefileEncryption
    Se eseguito da shell Administrator (elevata) senza -CheckOnly, esegue `fsutil behavior set encryptpagingfile 1`.
.PARAMETER CheckOnly
    Modalita sola lettura: verifica .wslconfig ed EncryptPagingFile senza apportare modifiche.
#>

[CmdletBinding()]
param(
    [switch]$ApplyWslConfig,
    [switch]$ApplyPagefileEncryption,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ProjectRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TemplatePath  = Join-Path $ProjectRoot "config\wslconfig.template"
$WslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$Utf8NoBom     = New-Object System.Text.UTF8Encoding($false)

function Test-IsAdministrator {
    try {
        $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Parse-Wsl2Settings([string]$FilePath) {
    $result = @{
        Exists              = $false
        HasWsl2Section      = $false
        Swap                = $null
        Memory              = $null
        Processors          = $null
        LocalhostForwarding = $null
    }
    if (-not (Test-Path $FilePath)) { return $result }

    $result.Exists = $true
    $inWsl2 = $false
    foreach ($rawLine in (Get-Content -Path $FilePath -Encoding UTF8)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        if ($line -match '^\[([^\]]+)\]$') {
            $inWsl2 = ($Matches[1].Trim().ToLowerInvariant() -eq 'wsl2')
            if ($inWsl2) { $result.HasWsl2Section = $true }
            continue
        }
        if ($inWsl2 -and $line -match '^([A-Za-z0-9_.-]+)\s*=\s*(.+)$') {
            $k = $Matches[1].Trim().ToLowerInvariant()
            $v = $Matches[2].Trim()
            switch ($k) {
                'swap'                { $result.Swap = $v }
                'memory'              { $result.Memory = $v }
                'processors'          { $result.Processors = $v }
                'localhostforwarding' { $result.LocalhostForwarding = $v }
            }
        }
    }
    return $result
}

function Merge-WslConfigContent([string[]]$ExistingLines) {
    $out = New-Object System.Collections.Generic.List[string]
    $inWsl2 = $false
    $foundWsl2Section = $false
    $seenSwap = $false
    $seenMemory = $false
    $seenProcessors = $false
    $seenLocalhost = $false

    $insertMissingWsl2Keys = {
        if (-not $seenMemory)     { $out.Add("memory=3GB") }
        if (-not $seenProcessors) { $out.Add("processors=8") }
        if (-not $seenSwap)       { $out.Add("swap=0") }
        if (-not $seenLocalhost)  { $out.Add("localhostForwarding=true") }
    }

    foreach ($rawLine in $ExistingLines) {
        $trimmed = $rawLine.Trim()
        if ($trimmed -match '^\[([^\]]+)\]$') {
            $secName = $Matches[1].Trim().ToLowerInvariant()
            if ($inWsl2 -and $secName -ne 'wsl2') {
                & $insertMissingWsl2Keys
                $inWsl2 = $false
            }
            if ($secName -eq 'wsl2') {
                $inWsl2 = $true
                $foundWsl2Section = $true
            }
            $out.Add($rawLine)
            continue
        }

        if ($inWsl2 -and $trimmed -and (-not $trimmed.StartsWith('#')) -and (-not $trimmed.StartsWith(';')) -and ($trimmed -match '^([A-Za-z0-9_.-]+)\s*=\s*(.*)$')) {
            $k = $Matches[1].Trim().ToLowerInvariant()
            if ($k -eq 'swap') {
                $seenSwap = $true
                $out.Add("swap=0")
            } elseif ($k -eq 'memory') {
                $seenMemory = $true
                $out.Add("memory=3GB")
            } elseif ($k -eq 'processors') {
                $seenProcessors = $true
                $out.Add($rawLine)
            } elseif ($k -eq 'localhostforwarding') {
                $seenLocalhost = $true
                $out.Add($rawLine)
            } else {
                $out.Add($rawLine)
            }
            continue
        }

        $out.Add($rawLine)
    }

    if ($inWsl2) {
        & $insertMissingWsl2Keys
    } elseif (-not $foundWsl2Section) {
        if ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -ne '') {
            $out.Add("")
        }
        $out.Add("# Local LLM Privacy Hardening (swap=0 anti-leak + memory=3GB headroom Heavy MoE)")
        $out.Add("[wsl2]")
        $out.Add("memory=3GB")
        $out.Add("processors=8")
        $out.Add("swap=0")
        $out.Add("localhostForwarding=true")
    }

    return ($out -join "`r`n") + "`r`n"
}

function Get-EncryptPagingFileState {
    try {
        $raw = & fsutil behavior query encryptpagingfile 2>&1 | Out-String
        if ($raw -match '(?i)EncryptPagingFile\s*=\s*(\d+)') {
            return [pscustomobject]@{
                Available = $true
                Value     = [int]$Matches[1]
                Raw       = $raw.Trim()
            }
        }
        return [pscustomobject]@{
            Available = $false
            Value     = -1
            Raw       = $raw.Trim()
        }
    } catch {
        return [pscustomobject]@{
            Available = $false
            Value     = -1
            Raw       = $_.Exception.Message
        }
    }
}

Write-Host "`n=== LOCAL LLM HOST PRIVACY HARDENING ===" -ForegroundColor Cyan

# ------------------------------------------------------------------------------
# 1. Configurazione WSL2 (%USERPROFILE%\.wslconfig)
# ------------------------------------------------------------------------------
$wslCompliant = $false
$shouldApplyWsl = (-not $CheckOnly) -or $ApplyWslConfig
if ($CheckOnly) { $shouldApplyWsl = $false }

$wslState = Parse-Wsl2Settings -FilePath $WslConfigPath
$alreadyHardened = $wslState.Exists -and $wslState.HasWsl2Section -and ($wslState.Swap -eq '0') -and ($wslState.Memory -ieq '3GB')

if ($CheckOnly) {
    if ($alreadyHardened) {
        Write-Host "[OK]   .wslconfig conforme: $WslConfigPath (swap=$($wslState.Swap), memory=$($wslState.Memory))" -ForegroundColor Green
        $wslCompliant = $true
    } elseif (-not $wslState.Exists) {
        Write-Host "[WARN] .wslconfig assente: $WslConfigPath (eseguire scripts\setup-privacy-host.ps1 per crearlo)" -ForegroundColor Yellow
    } else {
        Write-Host "[WARN] .wslconfig non conforme: swap=$($wslState.Swap), memory=$($wslState.Memory) (attesi swap=0, memory=3GB)" -ForegroundColor Yellow
    }
} elseif ($shouldApplyWsl) {
    if (-not (Test-Path $TemplatePath)) {
        Write-Host "[ERR]  Template mancante: $TemplatePath" -ForegroundColor Red
        exit 1
    }

    if (-not $wslState.Exists) {
        $templateContent = Get-Content -Path $TemplatePath -Raw -Encoding UTF8
        [System.IO.File]::WriteAllText($WslConfigPath, $templateContent, $Utf8NoBom)
        Write-Host "[OK]   Creato $WslConfigPath da config\wslconfig.template (swap=0, memory=3GB, processors=8)." -ForegroundColor Green
        Write-Host "       Nota: se WSL2/Docker Desktop era gia attivo, esegui 'wsl --shutdown' per applicare i limiti." -ForegroundColor Gray
    } elseif ($alreadyHardened) {
        Write-Host "[OK]   $WslConfigPath gia conforme (swap=0, memory=3GB). Nessuna modifica necessaria." -ForegroundColor Green
    } else {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $backupPath = "$WslConfigPath.bak.$stamp"
        Copy-Item -Path $WslConfigPath -Destination $backupPath -Force
        Write-Host "[INFO] Backup .wslconfig esistente salvato in: $backupPath" -ForegroundColor Cyan

        $existingLines = @(Get-Content -Path $WslConfigPath -Encoding UTF8)
        $mergedContent = Merge-WslConfigContent -ExistingLines $existingLines
        [System.IO.File]::WriteAllText($WslConfigPath, $mergedContent, $Utf8NoBom)
        Write-Host "[OK]   Aggiornato $WslConfigPath (swap=0, memory=3GB applicati sotto [wsl2], altre sezioni preservate)." -ForegroundColor Green
        Write-Host "       Nota: esegui 'wsl --shutdown' (o riavvia Docker Desktop) per applicare i limiti." -ForegroundColor Gray
    }

    $verifyState = Parse-Wsl2Settings -FilePath $WslConfigPath
    $wslCompliant = $verifyState.Exists -and $verifyState.HasWsl2Section -and ($verifyState.Swap -eq '0') -and ($verifyState.Memory -ieq '3GB')
    if (-not $wslCompliant) {
        Write-Host "[ERR]  Verifica post-scrittura fallita per $WslConfigPath." -ForegroundColor Red
        exit 1
    }
}

# ------------------------------------------------------------------------------
# 2. Verifica / Guida Windows Pagefile Encryption (EncryptPagingFile)
# ------------------------------------------------------------------------------
$pf = Get-EncryptPagingFileState
$isAdmin = Test-IsAdministrator

if ($pf.Available -and $pf.Value -eq 1) {
    Write-Host "[OK]   Windows Pagefile Encryption attiva (EncryptPagingFile = 1)." -ForegroundColor Green
} elseif ($pf.Available -and $pf.Value -eq 0) {
    if ($isAdmin -and $ApplyPagefileEncryption -and (-not $CheckOnly)) {
        Write-Host "[INFO] Shell elevata rilevata: attivazione Windows Pagefile Encryption..." -ForegroundColor Cyan
        $setOut = & fsutil behavior set encryptpagingfile 1 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK]   EncryptPagingFile impostato a 1. Riavvio di Windows necessario per rendere effettiva la cifratura." -ForegroundColor Green
            if ($setOut.Trim()) {
                Write-Host ("       {0}" -f $setOut.Trim()) -ForegroundColor Gray
            }
        } else {
            Write-Host "[ERR]  Errore durante fsutil behavior set encryptpagingfile 1: $($setOut.Trim())" -ForegroundColor Red
            exit 1
        }
    } elseif ($isAdmin -and (-not $CheckOnly)) {
        Write-Host "[WARN] Windows Pagefile Encryption disabilitata (EncryptPagingFile = 0)." -ForegroundColor Yellow
        Write-Host "       La shell e gia elevata: riesegui con -ApplyPagefileEncryption oppure esegui:" -ForegroundColor Gray
        Write-Host "       fsutil behavior set encryptpagingfile 1" -ForegroundColor White
        Write-Host "       (Riavvio di Windows necessario dopo l'attivazione.)" -ForegroundColor Gray
    } else {
        Write-Host ""
        Write-Host "+------------------------------------------------------------------------------+" -ForegroundColor Yellow
        Write-Host "| [AZIONE ADMIN RICHIESTA] Windows Pagefile Encryption = 0 (Disabilitata)      |" -ForegroundColor Yellow
        Write-Host "+------------------------------------------------------------------------------+" -ForegroundColor Yellow
        Write-Host "| Per proteggere eventuali pagine RAM scritte su pagefile.sys, esegui questo   |" -ForegroundColor Yellow
        Write-Host "| comando (aprira prompt UAC legittimo) e poi riavvia Windows:                 |" -ForegroundColor Yellow
        Write-Host "+------------------------------------------------------------------------------+" -ForegroundColor Yellow
        Write-Host "Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile -Command `"fsutil behavior set encryptpagingfile 1; Read-Host Premi Invio`"'" -ForegroundColor White
        Write-Host ""
    }
} else {
    Write-Host "[WARN] Impossibile interrogare fsutil behavior query encryptpagingfile: $($pf.Raw)" -ForegroundColor Yellow
}

if ($CheckOnly -and (-not $wslCompliant)) {
    exit 1
}

exit 0
