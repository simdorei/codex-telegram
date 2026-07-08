@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%codex_discord_bot.py"
set "SCRIPT_ARGS=%*"
set "ENV_FILE=%SCRIPT_DIR%.env"
set "LOCK_DIR=%SCRIPT_DIR%.codex_discord_bot.lock"
set "RUNTIME_LOCK_FILE=%SCRIPT_DIR%.codex_discord_bot.runtime.lock"
set "PID_FILE=%LOCK_DIR%\launcher.pid"
set "LAUNCHER_PID="
set "LOG_PATH=%CODEX_DISCORD_LOG_PATH%"
set "LAUNCHER_LOG_PATH=%SCRIPT_DIR%discord_launcher.log"

if not defined LOG_PATH set "LOG_PATH=%SCRIPT_DIR%codex_discord_bot.log"
if not defined PYTHON_EXE if exist "%ENV_FILE%" (
  set "LOAD_ENV_NAME=PYTHON_EXE"
  call :load_env_value
)

for /f %%P in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID); [Console]::Write($p.ParentProcessId)"') do set "LAUNCHER_PID=%%P"

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: "%SCRIPT%"
  exit /b 1
)

echo.
echo Codex Discord frontend bridge is starting.
echo Script: "%SCRIPT%"
echo Log:    "%LOG_PATH%"
echo Close this window to stop the visible launcher, or use codex-discord-bot-headless.vbs for headless operation.
echo.
set "LAUNCHER_LOG_MESSAGE=visible_start script=%SCRIPT% log=%LOG_PATH%"
call :log_launcher

call :existing_bot_process_alive
if errorlevel 1 goto create_lock
echo Codex Discord bot is already running.
echo Log: "%LOG_PATH%"
set "LAUNCHER_LOG_MESSAGE=already_running process_scan script=%SCRIPT% log=%LOG_PATH%"
call :log_launcher
goto finish_success

:create_lock
mkdir "%LOCK_DIR%" >nul 2>nul
if not errorlevel 1 goto lock_ready

call :existing_launcher_alive
if errorlevel 1 goto stale_lock
echo Codex Discord bot is already running.
echo Log: "%LOG_PATH%"
set "LAUNCHER_LOG_MESSAGE=already_running script=%SCRIPT% log=%LOG_PATH%"
call :log_launcher
goto finish_success

:stale_lock
echo Removing stale Codex Discord bot lock.
set "LAUNCHER_LOG_MESSAGE=stale_lock_removed lock=%LOCK_DIR%"
call :log_launcher
rmdir /s /q "%LOCK_DIR%" >nul 2>nul
mkdir "%LOCK_DIR%" >nul 2>nul
if not errorlevel 1 goto lock_ready
echo ERROR: Could not create lock directory: "%LOCK_DIR%"
set "LAUNCHER_LOG_MESSAGE=lock_create_failed lock=%LOCK_DIR%"
call :log_launcher
goto finish_error

:lock_ready
if defined LAUNCHER_PID (
  >"%PID_FILE%" echo %LAUNCHER_PID%
)

if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto run

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" goto run

where py >nul 2>nul
if %errorlevel%==0 (
  set "LAUNCHER_LOG_MESSAGE=run launcher=py script=%SCRIPT%"
  call :log_launcher
  py -3 "%SCRIPT%" %SCRIPT_ARGS%
  set "EXIT_CODE=%errorlevel%"
  set "LAUNCHER_LOG_MESSAGE=exit code=%EXIT_CODE% script=%SCRIPT%"
  call :log_launcher
  rmdir /s /q "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

where python >nul 2>nul
if %errorlevel%==0 (
  set "LAUNCHER_LOG_MESSAGE=run launcher=python script=%SCRIPT%"
  call :log_launcher
  python "%SCRIPT%" %SCRIPT_ARGS%
  set "EXIT_CODE=%errorlevel%"
  set "LAUNCHER_LOG_MESSAGE=exit code=%EXIT_CODE% script=%SCRIPT%"
  call :log_launcher
  rmdir /s /q "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

echo ERROR: Python executable not found.
set "LAUNCHER_LOG_MESSAGE=python_not_found script=%SCRIPT%"
call :log_launcher
rmdir /s /q "%LOCK_DIR%" >nul 2>nul
exit /b 1

:finish_success
exit /b 0

:finish_error
exit /b 1

:run
set "LAUNCHER_LOG_MESSAGE=run python_exe=%PYTHON_EXE% script=%SCRIPT%"
call :log_launcher
"%PYTHON_EXE%" "%SCRIPT%" %SCRIPT_ARGS%
set "EXIT_CODE=%errorlevel%"
set "LAUNCHER_LOG_MESSAGE=exit code=%EXIT_CODE% script=%SCRIPT%"
call :log_launcher
rmdir /s /q "%LOCK_DIR%" >nul 2>nul
exit /b %EXIT_CODE%

:log_launcher
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$path=$env:LAUNCHER_LOG_PATH; $message=$env:LAUNCHER_LOG_MESSAGE; if ($path -and $message) { Add-Content -LiteralPath $path -Encoding UTF8 -Value ('[' + (Get-Date).ToString('s') + '] ' + $message) }" >nul 2>nul
set "LAUNCHER_LOG_MESSAGE="
exit /b 0

:load_env_value
for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
  if /I "%%A"=="%LOAD_ENV_NAME%" if not "%%B"=="" set "%LOAD_ENV_NAME%=%%~B"
)
set "LOAD_ENV_NAME="
exit /b 0

:existing_launcher_alive
set "EXISTING_PID="
if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do set "EXISTING_PID=%%P"
  if defined EXISTING_PID (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$pidText=$env:EXISTING_PID; if ($pidText -match '^\d+$' -and (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $pidText) -ErrorAction SilentlyContinue)) { exit 0 } exit 1" >nul 2>nul
    if not errorlevel 1 exit /b 0
  )
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$script=$env:SCRIPT; if (-not $script) { exit 1 }; $needle=$script.ToLowerInvariant(); foreach ($p in Get-CimInstance Win32_Process) { $cmd=[string]$p.CommandLine; if (($p.Name -eq 'py.exe' -or $p.Name -eq 'python.exe' -or $p.Name -eq 'pythonw.exe') -and $cmd.ToLowerInvariant().Contains($needle)) { exit 0 } }; exit 1" >nul 2>nul
if not errorlevel 1 exit /b 0

call :existing_bot_process_alive
if not errorlevel 1 exit /b 0
exit /b 1

:existing_bot_process_alive
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$script=[IO.Path]::GetFullPath($env:SCRIPT).ToLowerInvariant(); $names=@('py.exe','python.exe','pythonw.exe'); $lock=$env:RUNTIME_LOCK_FILE; if ($lock -and (Test-Path -LiteralPath $lock)) { $pidText=(Get-Content -LiteralPath $lock -Raw -ErrorAction SilentlyContinue).Trim(); if ($pidText -match '^\d+$') { $p=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $pidText) -ErrorAction SilentlyContinue; if ($p) { $name=[string]$p.Name; $cmd=([string]$p.CommandLine).ToLowerInvariant(); if ($names -contains $name -and $cmd.Contains($script)) { exit 0 }; Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue } } }; foreach ($p in Get-CimInstance Win32_Process) { $name=[string]$p.Name; if (-not ($names -contains $name)) { continue }; $cmd=([string]$p.CommandLine).ToLowerInvariant(); if ($cmd.Contains($script)) { exit 0 } }; exit 1" >nul 2>nul
exit /b %errorlevel%
