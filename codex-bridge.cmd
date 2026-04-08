@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%codex_desktop_bridge.py"
set "TELEGRAM_PY=%SCRIPT_DIR%codex_telegram_bot.py"
set "ENV_FILE=%SCRIPT_DIR%.env"
set "AUTO_START_TELEGRAM=1"
set "TELEGRAM_PYTHON_EXE="

if /I "%CODEX_BRIDGE_AUTO_START_TELEGRAM%"=="0" set "AUTO_START_TELEGRAM=0"

if /I "%~1"=="--no-bot" (
  set "AUTO_START_TELEGRAM=0"
  shift
)

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: "%SCRIPT%"
  exit /b 1
)

if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto after_python

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" goto after_python

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SCRIPT%" %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT%" %*
  exit /b %errorlevel%
)

echo ERROR: Python executable not found.
exit /b 1

:after_python
if /I "%~1"=="--bot-only" (
  if not exist "%TELEGRAM_PY%" (
    echo ERROR: Telegram bot script not found: "%TELEGRAM_PY%"
    exit /b 1
  )
  shift
  "%PYTHON_EXE%" "%TELEGRAM_PY%" %*
  exit /b %errorlevel%
)

:run
if "%AUTO_START_TELEGRAM%"=="1" if "%~1"=="" call :start_telegram
"%PYTHON_EXE%" "%SCRIPT%" %*
exit /b %errorlevel%

:start_telegram
if not exist "%TELEGRAM_PY%" exit /b 0

set "HAS_TELEGRAM_TOKEN="
if defined TELEGRAM_BOT_TOKEN set "HAS_TELEGRAM_TOKEN=1"
if not defined HAS_TELEGRAM_TOKEN findstr /R /I /C:"^[ ]*TELEGRAM_BOT_TOKEN[ ]*=" "%ENV_FILE%" >nul 2>nul && set "HAS_TELEGRAM_TOKEN=1"
if not defined HAS_TELEGRAM_TOKEN exit /b 0

set "TELEGRAM_PYTHON_EXE=%PYTHON_EXE%"
if /I "%PYTHON_EXE:~-10%"=="python.exe" (
  set "TELEGRAM_PYTHON_EXE=%PYTHON_EXE:~0,-10%pythonw.exe"
)
if not exist "%TELEGRAM_PYTHON_EXE%" set "TELEGRAM_PYTHON_EXE=%PYTHON_EXE%"

if /I "%TELEGRAM_PYTHON_EXE:~-11%"=="pythonw.exe" (
  start "Codex Telegram Bot" "%TELEGRAM_PYTHON_EXE%" "%TELEGRAM_PY%" --skip-old-updates
) else (
  start "Codex Telegram Bot" /min "%TELEGRAM_PYTHON_EXE%" "%TELEGRAM_PY%" --skip-old-updates
)
exit /b 0
