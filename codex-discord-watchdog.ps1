[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$LogHealthy
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotScript = Join-Path $ScriptDir 'codex_discord_bot.py'
$RuntimeLockPath = Join-Path $ScriptDir '.codex_discord_bot.runtime.lock'
$RestartRequestPath = Join-Path $ScriptDir '.codex_discord_bot.restart'
$StopRequestPath = Join-Path $ScriptDir '.codex_discord_bot.stop'
$DisablePath = Join-Path $ScriptDir '.codex_discord_bot.disabled'
$HeadlessLauncher = Join-Path $ScriptDir 'codex-discord-bot-headless.vbs'
$LauncherLogPath = Join-Path $ScriptDir 'discord_launcher.log'

function Write-LauncherLog {
    param([string]$Message)

    $timestamp = (Get-Date).ToString('s')
    Add-Content -LiteralPath $LauncherLogPath -Encoding UTF8 -Value "[$timestamp] $Message"
}

function Get-RuntimePid {
    if (-not (Test-Path -LiteralPath $RuntimeLockPath)) {
        return $null
    }

    try {
        $runtimePidText = (Get-Content -LiteralPath $RuntimeLockPath -Raw -ErrorAction Stop).Trim()
        if ($runtimePidText -notmatch '^\d+$') {
            Write-LauncherLog "runtime_lock_invalid lock=$RuntimeLockPath value=$runtimePidText"
            return $null
        }
        return [int]$runtimePidText
    } catch {
        Write-LauncherLog "runtime_lock_probe_failed lock=$RuntimeLockPath error=$($_.Exception.GetType().Name)"
        return $null
    }
}

function Test-IsBotProcess {
    param(
        $Process,
        [switch]$AllowRuntimeLockFallback
    )

    if ($Process -eq $null) {
        return $false
    }
    $name = [string]$Process.Name
    if ($name -ne 'py.exe' -and $name -ne 'python.exe' -and $name -ne 'pythonw.exe') {
        return $false
    }
    $needle = [IO.Path]::GetFullPath($BotScript).ToLowerInvariant()
    $commandLine = ([string]$Process.CommandLine).ToLowerInvariant()
    if (-not $commandLine -and $AllowRuntimeLockFallback) {
        return $true
    }
    return $commandLine.Contains($needle)
}

function Get-BotProcesses {
    foreach ($process in Get-CimInstance Win32_Process) {
        if (Test-IsBotProcess $process) {
            $process
        }
    }
}

function Stop-RuntimeBotProcess {
    $runtimePid = Get-RuntimePid
    if ($runtimePid -ne $null) {
        $runtimeProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$runtimePid" -ErrorAction SilentlyContinue
        if ($runtimeProcess -eq $null) {
            Write-LauncherLog "watchdog_restart_pid_not_running pid=$runtimePid"
            Remove-Item -LiteralPath $RuntimeLockPath -Force -ErrorAction SilentlyContinue
        } elseif (Test-IsBotProcess $runtimeProcess -AllowRuntimeLockFallback) {
            Write-LauncherLog "watchdog_restart_stop pid=$runtimePid name=$($runtimeProcess.Name)"
            Stop-Process -Id $runtimePid -Force -ErrorAction Stop
            Start-Sleep -Seconds 2
            Remove-Item -LiteralPath $RuntimeLockPath -Force -ErrorAction SilentlyContinue
            return
        } else {
            Write-LauncherLog "watchdog_restart_pid_mismatch pid=$runtimePid name=$($runtimeProcess.Name)"
            Remove-Item -LiteralPath $RuntimeLockPath -Force -ErrorAction SilentlyContinue
        }
    }

    $matchedProcesses = @(Get-BotProcesses)
    if ($matchedProcesses.Count -eq 0) {
        Write-LauncherLog "watchdog_restart_no_runtime_pid lock=$RuntimeLockPath"
        return
    }
    foreach ($process in $matchedProcesses) {
        Write-LauncherLog "watchdog_restart_stop_scan pid=$($process.ProcessId) name=$($process.Name)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    Start-Sleep -Seconds 2
}

function Test-BotProcessAlive {
    if (-not (Test-Path -LiteralPath $BotScript)) {
        return $false
    }

    $runtimePid = Get-RuntimePid
    if ($runtimePid -ne $null) {
        $runtimeProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$runtimePid" -ErrorAction SilentlyContinue
        if (Test-IsBotProcess $runtimeProcess) {
            return $true
        } elseif ($runtimeProcess -ne $null) {
            Write-LauncherLog "runtime_lock_pid_mismatch pid=$runtimePid name=$($runtimeProcess.Name)"
            Remove-Item -LiteralPath $RuntimeLockPath -Force -ErrorAction SilentlyContinue
        }
    }

    foreach ($process in Get-BotProcesses) {
        if ($process -ne $null) {
            return $true
        }
    }
    return $false
}

if (-not (Test-Path -LiteralPath $BotScript)) {
    Write-LauncherLog "watchdog_error reason=bot_script_missing script=$BotScript"
    exit 1
}

if (Test-Path -LiteralPath $StopRequestPath) {
    if ($DryRun) {
        Write-Output "stop_requested"
        exit 0
    }
    Write-LauncherLog "watchdog_stop_requested marker=$StopRequestPath"
    Remove-Item -LiteralPath $StopRequestPath -Force -ErrorAction SilentlyContinue
    Stop-RuntimeBotProcess
    exit 0
}

if (Test-Path -LiteralPath $DisablePath) {
    if ($DryRun) {
        Write-Output "disabled"
    } elseif ($LogHealthy) {
        Write-LauncherLog "watchdog_disabled marker=$DisablePath"
    }
    exit 0
}

if (Test-Path -LiteralPath $RestartRequestPath) {
    if ($DryRun) {
        Write-Output "restart_requested"
        exit 0
    }
    Write-LauncherLog "watchdog_restart_requested marker=$RestartRequestPath"
    Remove-Item -LiteralPath $RestartRequestPath -Force -ErrorAction SilentlyContinue
    Stop-RuntimeBotProcess
}

if (Test-BotProcessAlive) {
    if ($LogHealthy) {
        Write-LauncherLog "watchdog_ok script=$BotScript"
    }
    if ($DryRun) {
        Write-Output "running"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $HeadlessLauncher)) {
    Write-LauncherLog "watchdog_error reason=headless_launcher_missing launcher=$HeadlessLauncher"
    exit 1
}

if ($DryRun) {
    Write-Output "would_start"
    exit 0
}

Write-LauncherLog "watchdog_start_missing script=$BotScript launcher=$HeadlessLauncher"
Start-Process -FilePath 'wscript.exe' -ArgumentList @("`"$HeadlessLauncher`"") -WindowStyle Hidden
exit 0
