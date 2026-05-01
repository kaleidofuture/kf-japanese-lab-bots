' Hidden launcher for auto_git_pull.ps1 — wscript + Run window=0 fully
' suppresses the brief PowerShell console flicker that -WindowStyle Hidden
' alone leaves visible.
'
' Invoked by Task Scheduler:
'   wscript.exe <repo>\services\run_hidden_pull.vbs

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = scriptDir & "\auto_git_pull.ps1"

Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & target & """", 0, False
