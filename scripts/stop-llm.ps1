<#
.SYNOPSIS
    Scarica i modelli dalla VRAM, rimuove container sandbox residui e arresta i servizi locali
    (Ollama, Colibri Heavy MoE, Offline Chat UI e cache temporanea del profilo App).
.PARAMETER KeepUI
    Mantiene attiva la Console Web Offline (workspaces/offline-chat/server.py) durante lo switch tra motori.
#>

[CmdletBinding()]
param(
    [switch]$KeepUI
)

$ErrorActionPreference = 'SilentlyContinue'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    foreach ($p in ($userPath -split ';')) {
        $dir = $p.Trim()
        if ($dir -and (Test-Path $dir) -and ($env:Path -notlike "*$dir*")) {
            $env:Path = "$env:Path;$dir"
        }
    }
}

function Invoke-DockerFast {
    param(
        [string]$DockerExe,
        [string]$Arguments,
        [int]$TimeoutMs = 3500
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

# 1. Rimozione immediata di eventuali container sandbox Orchestrator (sempre, anche con -KeepUI)
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCmd -and $dockerCmd.Source) {
    $psRes = Invoke-DockerFast -DockerExe $dockerCmd.Source -Arguments 'ps -aq --filter "label=local-llm-orchestrator=true"' -TimeoutMs 3500
    if ($psRes -and (-not $psRes.TimedOut) -and ($psRes.ExitCode -eq 0)) {
        $containerIds = @(
            ($psRes.StdOut -split "`r?`n") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -match '^[a-fA-F0-9]{6,}$' }
        )
        if ($containerIds.Count -gt 0) {
            Write-Host "Arresto/rimozione container sandbox Docker residui ($($containerIds.Count))..." -ForegroundColor Cyan
            $idArgs = ($containerIds -join ' ')
            [void](Invoke-DockerFast -DockerExe $dockerCmd.Source -Arguments "rm -f $idArgs" -TimeoutMs 8000)
        }
    }
}

Write-Host "Arresto modelli Ollama attivi in VRAM..." -ForegroundColor Cyan
$ollamaRunning = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if ($ollamaRunning -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
    $psOut = ollama ps 2>$null
    if ($psOut) {
        $psOut | Select-Object -Skip 1 | ForEach-Object {
            $name = ($_ -split '\s+')[0]
            if ($name) {
                Write-Host "  Unload Ollama: $name"
                ollama stop $name 2>$null | Out-Null
            }
        }
    }
}

Write-Host "Arresto processi Ollama..." -ForegroundColor Cyan
Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "Arresto eventuali processi Heavy MoE Colibri..." -ForegroundColor Cyan
$colibriExeNames = @(
    "qwen38", "qwen36", "colibri", "glm53", "deepseek_v41", "kimi_k3",
    "mimo", "inkling", "olmoe", "deepseek_v4", "laya", "gliner_decide", "qwenimage"
)
Get-Process -Name $colibriExeNames -ErrorAction SilentlyContinue | Stop-Process -Force

Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*openai_server.py*" -or
        $_.CommandLine -like "*engines*colibri*coli*" -or
        $_.CommandLine -match 'coli(\.cmd)?\s+serve\b'
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*engines*colibri*coli.cmd*serve*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

if (-not $KeepUI) {
    Write-Host "Arresto eventuale Console Offline Privata (server.py), System Tray e finestra App..." -ForegroundColor Cyan
    Get-CimInstance Win32_Process -Filter "Name LIKE 'pwsh%' OR Name LIKE 'powershell%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+"?[^"]*launcher\\tray-app\.ps1' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $appBrowsers = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe' OR Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -like "*LocalLLM-AppProfile*" -or
            $_.CommandLine -like "*launcher*.app-profile*"
        })
    foreach ($bp in $appBrowsers) {
        Stop-Process -Id $bp.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($appBrowsers.Count -gt 0) {
        Start-Sleep -Milliseconds 600
    }

    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*offline-chat*server.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    # Rimozione completa di eventuali profili effimeri %TEMP%\LocalLLM-AppProfile-* e legacy launcher\.app-profile (0 B residui)
    $cleanedProfiles = 0
    $legacyProfileDir = Join-Path $ProjectRoot "launcher\.app-profile"
    if (Test-Path $legacyProfileDir) {
        Remove-Item -Path $legacyProfileDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $legacyProfileDir)) { $cleanedProfiles++ }
    }
    $tempProfileDirs = @(Get-ChildItem -Path ([System.IO.Path]::GetTempPath()) -Directory -Filter "LocalLLM-AppProfile-*" -ErrorAction SilentlyContinue)
    foreach ($tp in $tempProfileDirs) {
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            Remove-Item -Path $tp.FullName -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $tp.FullName)) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path $tp.FullName)) { $cleanedProfiles++ }
    }
    if ($cleanedProfiles -gt 0) {
        Write-Host "  Rimossi completamente $cleanedProfiles profili browser App (0 B residui)." -ForegroundColor Gray
    }

    Write-Host "[OK] Tutti i servizi locali arrestati, profili App azzerati (0 B) e VRAM/RAM liberate." -ForegroundColor Green
} else {
    Write-Host "[OK] Motori LLM arrestati e VRAM/RAM liberate (Console Offline UI mantenuta attiva)." -ForegroundColor Green
}
