@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%codex_discord_bot.py"
set "LOCK_DIR=%SCRIPT_DIR%.codex_discord_bot.lock"
set "PID_FILE=%LOCK_DIR%\launcher.pid"
set "LAUNCHER_PID="

for /f %%P in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID); [Console]::Write($p.ParentProcessId)"') do set "LAUNCHER_PID=%%P"

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: "%SCRIPT%"
  exit /b 1
)

mkdir "%LOCK_DIR%" >nul 2>nul
if errorlevel 1 (
  call :existing_launcher_alive
  if not errorlevel 1 (
    echo Codex Discord bot is already running.
    exit /b 0
  )
  echo Removing stale Codex Discord bot lock.
  rmdir /s /q "%LOCK_DIR%" >nul 2>nul
  mkdir "%LOCK_DIR%" >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Could not create lock directory: "%LOCK_DIR%"
    exit /b 1
  )
)

if defined LAUNCHER_PID (
  >"%PID_FILE%" echo %LAUNCHER_PID%
)

if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto run

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" goto run

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SCRIPT%" %*
  set "EXIT_CODE=%errorlevel%"
  rmdir /s /q "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT%" %*
  set "EXIT_CODE=%errorlevel%"
  rmdir /s /q "%LOCK_DIR%" >nul 2>nul
  exit /b %EXIT_CODE%
)

echo ERROR: Python executable not found.
rmdir /s /q "%LOCK_DIR%" >nul 2>nul
exit /b 1

:run
"%PYTHON_EXE%" "%SCRIPT%" %*
set "EXIT_CODE=%errorlevel%"
rmdir /s /q "%LOCK_DIR%" >nul 2>nul
exit /b %EXIT_CODE%

:existing_launcher_alive
set "EXISTING_PID="
if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do set "EXISTING_PID=%%P"
  if defined EXISTING_PID (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$pidText=$env:EXISTING_PID; if ($pidText -match '^\d+$' -and (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $pidText) -ErrorAction SilentlyContinue)) { exit 0 } exit 1" >nul 2>nul
    if not errorlevel 1 exit /b 0
  )
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$script=$env:SCRIPT; if (-not $script) { exit 1 }; $needle=$script.ToLowerInvariant(); foreach ($p in Get-CimInstance Win32_Process) { $cmd=[string]$p.CommandLine; if (($p.Name -eq 'py.exe' -or $p.Name -eq 'python.exe' -or $p.Name -eq 'pythonw.exe') -and $cmd.ToLowerInvariant().Contains($needle)) { exit 0 } }; exit 1" >nul 2>nul
if not errorlevel 1 exit /b 0
exit /b 1
