# Register the KFLabBotsAutoPull task that runs auto_git_pull.ps1 every
# minute via the hidden VBS launcher.
#
# Run once on the host machine to install (or to update an existing
# registration after changing this script). After that the task scheduler
# keeps it running across reboots.
#
# Usage (from this services/ folder, in PowerShell):
#   powershell -ExecutionPolicy Bypass -File install_auto_deploy.ps1

$TaskName = "KFLabBotsAutoPull"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VbsPath = Join-Path $ScriptDir "run_hidden_pull.vbs"

if (-not (Test-Path $VbsPath)) {
    Write-Error "VBS launcher not found: $VbsPath"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`""

# Trigger: start now, repeat every 1 minute, run forever (3650 days = ~10y)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$trigger.Repetition = (New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler -ClientOnly `
    -Property @{ Interval = "PT1M"; Duration = "P3650D"; StopAtDurationEnd = $true })

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# RunLevel Highest matches the KF Tenshi task, so the auto-pull worker can read
# ExecutablePath of bot processes via Win32_Process and force-kill stale ones.
# Limited integrity hides Highest processes' details across the UAC boundary.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Auto-deploy: pull origin/main every 1 minute and restart KF Lab bots when their source changes."

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Updating existing task: $TaskName"
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} else {
    Write-Host "Registering new task: $TaskName"
    Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
}

Write-Host "Done. The task will start the next minute boundary and run every 1 minute."
Write-Host "Logs: <repo>\logs\auto_git_pull.log (silent on 'Already up to date' to avoid bloat)."
