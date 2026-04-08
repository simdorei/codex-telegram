@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%codex_telegram_bot.py"

if not exist "%SCRIPT%" (
  echo ERROR: Script not found: "%SCRIPT%"
  exit /b 1
)

if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto run

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%PYTHON_EXE%" goto run

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

:run
"%PYTHON_EXE%" "%SCRIPT%" %*
exit /b %errorlevel%
