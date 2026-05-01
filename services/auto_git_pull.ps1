# Auto-deploy for KF Japanese Lab Bots.
#
# Pulls origin/main once per minute. When changes affect a bot folder,
# restarts the corresponding Task Scheduler task. Modeled after the
# KaleidoAIMusic auto_git_pull pattern.
#
# Manual actions never automated:
#   - requirements.txt changes  → run pip install in the bot's .venv
#   - services/* changes        → re-run install_auto_deploy.ps1 if needed
#
# Logging policy: silent on "Already up to date" to avoid filling the log
# every minute. Writes only on actual pulls and errors.

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $RepoRoot "logs"
$LogPath = Join-Path $LogDir "auto_git_pull.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Map: regex of changed-file path -> { task name, process path filter }
# ProcessFilter is used to force-kill lingering pythonw children that
# `schtasks /End` does not stop (it terminates the task but leaves any
# spawned process running).
$BotTaskMap = @(
    @{ Pattern = '^kf_tenshi/.*\.(py|bat|env\.example)$';      Task = 'KFTenshi';      ProcessFilter = '*kf-japanese-lab-bots*kf_tenshi*' }
    @{ Pattern = '^kf_role_logger/.*\.(py|bat|env\.example)$'; Task = 'KFRoleLogger'; ProcessFilter = '*kf-japanese-lab-bots*kf_role_logger*' }
)

$WarnPatterns = @(
    @{ Pattern = '^.*requirements\.txt$'; Note = 'manual: activate the affected venv and run pip install -r requirements.txt' }
    @{ Pattern = '^services/.*';          Note = 'manual: re-run install_auto_deploy.ps1 if the schedule itself changed' }
)

function Restart-Task {
    param(
        [string]$TaskName,
        [string]$ProcessFilter
    )

    $endOut = & schtasks /End /TN $TaskName 2>&1 | Out-String
    Start-Sleep -Seconds 2

    # Use CIM/WMI here, not Get-Process. The auto-pull task runs at Limited
    # integrity while KF Tenshi runs at Highest, and Get-Process cannot read
    # `.Path` across that boundary — Win32_Process.ExecutablePath can.
    $killed = 0
    if ($ProcessFilter) {
        $stale = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -like $ProcessFilter }
        foreach ($p in $stale) {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}
        }
        if ($killed -gt 0) { Start-Sleep -Seconds 1 }
    }

    $runOut = & schtasks /Run /TN $TaskName 2>&1 | Out-String

    Add-Content -Path $LogPath -Value "[$timestamp] restart $TaskName (killed=$killed)" -Encoding UTF8
    if ($endOut.Trim()) { Add-Content -Path $LogPath -Value ("  end : " + $endOut.Trim()) -Encoding UTF8 }
    if ($runOut.Trim()) { Add-Content -Path $LogPath -Value ("  run : " + $runOut.Trim()) -Encoding UTF8 }
}

try {
    $headBefore = (& git -C $RepoRoot rev-parse HEAD 2>$null).Trim()

    $output = & git -C $RepoRoot pull --ff-only 2>&1 | Out-String
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        Add-Content -Path $LogPath -Value "[$timestamp] FAIL exit=$exitCode" -Encoding UTF8
        Add-Content -Path $LogPath -Value $output -Encoding UTF8
        exit 0
    }

    if ($output -match "Already up to date") {
        exit 0
    }

    Add-Content -Path $LogPath -Value "[$timestamp] PULL OK" -Encoding UTF8
    Add-Content -Path $LogPath -Value $output.TrimEnd() -Encoding UTF8

    $headAfter = (& git -C $RepoRoot rev-parse HEAD 2>$null).Trim()
    $changedFiles = & git -C $RepoRoot diff --name-only "$headBefore" "$headAfter" 2>$null

    # Bot restarts (deduplicated — restart each task only once even if multiple files matched)
    $restartedTasks = @{}
    foreach ($entry in $BotTaskMap) {
        $matched = @($changedFiles | Where-Object { $_ -match $entry.Pattern })
        if ($matched.Count -gt 0 -and -not $restartedTasks.ContainsKey($entry.Task)) {
            $sample = ($matched | Select-Object -First 5) -join ', '
            if ($matched.Count -gt 5) { $sample += ", ... ($($matched.Count) files)" }
            Add-Content -Path $LogPath -Value "[$timestamp] $($entry.Task) trigger: $sample" -Encoding UTF8
            Restart-Task -TaskName $entry.Task -ProcessFilter $entry.ProcessFilter
            $restartedTasks[$entry.Task] = $true
        }
    }

    # Manual-action warnings (never auto-applied)
    foreach ($entry in $WarnPatterns) {
        $matched = @($changedFiles | Where-Object { $_ -match $entry.Pattern })
        if ($matched.Count -gt 0) {
            Add-Content -Path $LogPath -Value "[$timestamp] WARN $($entry.Note): $($matched -join ', ')" -Encoding UTF8
        }
    }

    Add-Content -Path $LogPath -Value "" -Encoding UTF8
}
catch {
    Add-Content -Path $LogPath -Value "[$timestamp] EXCEPTION: $_" -Encoding UTF8
    exit 0
}
