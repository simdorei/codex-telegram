@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%codex_discord_bot.py"
set "LOCK_DIR=%SCRIPT_DIR%.codex_discord_bot.lock"

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: "%SCRIPT%"
  exit /b 1
)

mkdir "%LOCK_DIR%" >nul 2>nul
if errorlevel 1 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$task = Get-ScheduledTask -TaskName 'Codex Discord Bot' -ErrorAction SilentlyContinue; if ($task -and $task.State -eq 'Running') { exit 0 } exit 1" >nul 2>nul
  if not errorlevel 1 (
    echo Codex Discord bot is already running.
    exit /b 0
  )
  echo Removing stale Codex Discord bot lock.
  rmdir "%LOCK_DIR%" >nul 2>nul
  mkdir "%LOCK_DIR%" >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Could not create lock directory: "%LOCK_DIR%"
    exit /b 1
  )
)

if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto run

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" goto run

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SCRIPT%" %*
  set "EXIT_CODE=%errorlevel%"
  rmdir "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT%" %*
  set "EXIT_CODE=%errorlevel%"
  rmdir "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

echo ERROR: Python executable not found.
rmdir "%LOCK_DIR%" >nul 2>nul
exit /b 1

:run
"%PYTHON_EXE%" "%SCRIPT%" %*
set "EXIT_CODE=%errorlevel%"
rmdir "%LOCK_DIR%" >nul 2>nul
exit /b %EXIT_CODE%
