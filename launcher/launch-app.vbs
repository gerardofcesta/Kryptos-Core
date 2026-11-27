' Wrapper silenzioso per avviare Local LLM come App Desktop (zero finestra console)
Option Explicit
Dim fso, shell, scriptDir, psScript, pwshPath, cmd, modeArg
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(scriptDir, "launch-app.ps1")

pwshPath = "C:\Program Files\PowerShell\7\pwsh.exe"
If Not fso.FileExists(pwshPath) Then
    pwshPath = "powershell.exe"
End If

modeArg = "auto"
If WScript.Arguments.Count > 0 Then
    modeArg = WScript.Arguments(0)
End If

cmd = """" & pwshPath & """ -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & psScript & """ -Mode " & modeArg
shell.Run cmd, 0, False
