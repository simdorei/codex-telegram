[CmdletBinding()]
param(
    [switch]$Once
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotScript = Join-Path $ScriptDir 'codex_discord_bot.py'
$RuntimeLockPath = Join-Path $ScriptDir '.codex_discord_bot.runtime.lock'
$RestartRequestPath = Join-Path $ScriptDir '.codex_discord_bot.restart'
$HeadlessLauncher = Join-Path $ScriptDir 'codex-discord-bot-headless.vbs'
$LauncherLogPath = Join-Path $ScriptDir 'discord_launcher.log'
$BotLogPath = Join-Path $ScriptDir 'codex_discord_bot.log'

function Write-LauncherLog {
    param([string]$Message)

    $timestamp = (Get-Date).ToString('s')
    Add-Content -LiteralPath $LauncherLogPath -Encoding UTF8 -Value "[$timestamp] $Message"
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

function Get-BotProcess {
    if (Test-Path -LiteralPath $RuntimeLockPath) {
        $pidText = (Get-Content -LiteralPath $RuntimeLockPath -Raw -ErrorAction SilentlyContinue).Trim()
        if ($pidText -match '^\d+$') {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidText" -ErrorAction SilentlyContinue
            if (Test-IsBotProcess $process -AllowRuntimeLockFallback) {
                return $process
            }
        }
    }

    foreach ($process in Get-CimInstance Win32_Process) {
        if (Test-IsBotProcess $process) {
            return $process
        }
    }
    return $null
}

function Get-BotStatus {
    $process = Get-BotProcess
    if ($process -eq $null) {
        return [pscustomobject]@{
            Running = $false
            Pid = $null
            Text = 'Codex Discord bridge stopped'
            Icon = 'Warning'
        }
    }
    return [pscustomobject]@{
        Running = $true
        Pid = [int]$process.ProcessId
        Text = "Codex Discord bridge running (PID $($process.ProcessId))"
        Icon = 'Application'
    }
}

function Limit-TrayText {
    param([string]$Text)

    if ($Text.Length -le 63) {
        return $Text
    }
    return $Text.Substring(0, 60) + '...'
}

function Request-BotRestart {
    New-Item -ItemType File -LiteralPath $RestartRequestPath -Force | Out-Null
    $task = Get-ScheduledTask -TaskName 'Codex Discord Bot' -ErrorAction SilentlyContinue
    if ($task -ne $null) {
        Start-ScheduledTask -TaskName 'Codex Discord Bot'
        Write-LauncherLog "tray_restart_requested task='Codex Discord Bot'"
        return
    }
    if (Test-Path -LiteralPath $HeadlessLauncher) {
        Start-Process -FilePath 'wscript.exe' -ArgumentList @("`"$HeadlessLauncher`"") -WindowStyle Hidden
        Write-LauncherLog "tray_restart_requested launcher=$HeadlessLauncher"
    }
}

if ($Once) {
    $status = Get-BotStatus
    if ($status.Running) {
        Write-Output "running pid=$($status.Pid)"
        exit 0
    }
    Write-Output "stopped"
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$mutexHash = [BitConverter]::ToString(
    [Security.Cryptography.SHA256]::Create().ComputeHash(
        [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($ScriptDir).ToLowerInvariant())
    )
).Replace('-', '').Substring(0, 16)
$createdNew = $false
$mutex = New-Object Threading.Mutex($true, "Local\CodexDiscordTray_$mutexHash", [ref]$createdNew)
if (-not $createdNew) {
    Write-LauncherLog "tray_duplicate_exit script=$PSCommandPath"
    exit 0
}

$notify = New-Object Windows.Forms.NotifyIcon
$notify.Icon = [Drawing.SystemIcons]::Information
$notify.Text = 'Codex Discord bridge starting'
$notify.Visible = $true

$menu = New-Object Windows.Forms.ContextMenuStrip
$statusItem = New-Object Windows.Forms.ToolStripMenuItem
$statusItem.Text = 'Starting...'
$statusItem.Enabled = $false
[void]$menu.Items.Add($statusItem)

$openLogItem = New-Object Windows.Forms.ToolStripMenuItem
$openLogItem.Text = 'Open bot log'
$openLogItem.Add_Click({
    if (Test-Path -LiteralPath $BotLogPath) {
        Start-Process -FilePath 'notepad.exe' -ArgumentList @("`"$BotLogPath`"")
    }
})
[void]$menu.Items.Add($openLogItem)

$openFolderItem = New-Object Windows.Forms.ToolStripMenuItem
$openFolderItem.Text = 'Open bridge folder'
$openFolderItem.Add_Click({
    Start-Process -FilePath 'explorer.exe' -ArgumentList @("`"$ScriptDir`"")
})
[void]$menu.Items.Add($openFolderItem)

$restartItem = New-Object Windows.Forms.ToolStripMenuItem
$restartItem.Text = 'Restart bot'
$restartItem.Add_Click({
    try {
        Request-BotRestart
        $notify.ShowBalloonTip(3000, 'Codex Discord bridge', 'Restart requested.', [Windows.Forms.ToolTipIcon]::Info)
    } catch {
        Write-LauncherLog "tray_restart_failed error=$($_.Exception.GetType().Name)"
        $notify.ShowBalloonTip(3000, 'Codex Discord bridge', 'Restart request failed. Check discord_launcher.log.', [Windows.Forms.ToolTipIcon]::Error)
    }
})
[void]$menu.Items.Add($restartItem)

[void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))

$exitItem = New-Object Windows.Forms.ToolStripMenuItem
$exitItem.Text = 'Exit tray icon'
$exitItem.Add_Click({
    [Windows.Forms.Application]::Exit()
})
[void]$menu.Items.Add($exitItem)

$notify.ContextMenuStrip = $menu

function Update-TrayStatus {
    $status = Get-BotStatus
    $statusItem.Text = $status.Text
    $notify.Text = Limit-TrayText $status.Text
    if ($status.Running) {
        $notify.Icon = [Drawing.SystemIcons]::Application
    } else {
        $notify.Icon = [Drawing.SystemIcons]::Warning
    }
}

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({ Update-TrayStatus })

try {
    Write-LauncherLog "tray_start script=$PSCommandPath"
    [Windows.Forms.Application]::EnableVisualStyles()
    Update-TrayStatus
    $timer.Start()
    [Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
    Write-LauncherLog "tray_exit script=$PSCommandPath"
}
