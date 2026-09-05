@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title AK YouTube Downloader

REM ---- find a real Python 3 (ignores the Microsoft Store stub) ----
set "PY="
for /f "tokens=*" %%v in ('py -3 -c "import sys;print(sys.version_info.major)" 2^>nul') do if "%%v"=="3" set "PY=py -3"
if not defined PY for /f "tokens=*" %%v in ('python -c "import sys;print(sys.version_info.major)" 2^>nul') do if "%%v"=="3" set "PY=python"
if not defined PY for /f "tokens=*" %%v in ('python3 -c "import sys;print(sys.version_info.major)" 2^>nul') do if "%%v"=="3" set "PY=python3"

if not defined PY (
  echo.
  echo   Python 3 is not installed ^(or Windows is redirecting it to the Microsoft Store^).
  echo.
  echo   1^) Install Python:  winget install Python.Python.3.12
  echo      or from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
  echo   2^) If you see "Python was not found... Microsoft Store":
  echo        Settings ^> Apps ^> Advanced app settings ^> App execution aliases
  echo        ^> switch OFF python.exe and python3.exe
  echo.
  pause
  exit /b 1
)

echo Using Python: %PY%

if not exist "requirements.txt" (
  echo.
  echo   Cannot find requirements.txt next to this file.
  echo.
  echo   You are probably running run.bat from INSIDE the ZIP file.
  echo   Windows copies the .bat alone to a temp folder, so the rest of the
  echo   project is missing.
  echo.
  echo   Fix: close this window, right-click the ZIP in File Explorer,
  echo        choose "Extract All...", open the extracted folder,
  echo        and run run.bat from there.
  echo.
  echo   Current folder: %CD%
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating environment ^(one time^)...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Could not create the environment.
  pause
  exit /b 1
)

set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --quiet
echo Installing dependencies...
"%VPY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo Dependency install failed. Check your internet connection.
  pause
  exit /b 1
)

echo.
"%VPY%" app.py
pause
