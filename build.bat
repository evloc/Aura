@echo off
setlocal
title Building AURA.exe
cd /d "%~dp0"

echo.
echo  ================================
echo   Building AURA.exe
echo  ================================
echo.

rem ---- find Python 3.12 (numpy 1.x, which AURA uses, has no wheels for 3.13+) ----
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo  Python 3.12 was not found.
  echo.
  echo  1. Download "Windows installer (64-bit)" for Python 3.12 from
  echo     https://www.python.org/downloads/windows/
  echo  2. In the installer, tick "Add python.exe to PATH", then Install.
  echo  3. Double-click build.bat again.
  echo.
  pause
  exit /b 1
)
echo  Using: %PY%
%PY% --version

rem ---- a private environment so nothing else on the PC is touched ----
if not exist ".buildenv\Scripts\python.exe" (
  echo.
  echo  Creating build environment...
  %PY% -m venv .buildenv || goto :fail
)
set "VPY=.buildenv\Scripts\python.exe"

echo.
echo  Installing what AURA needs (first time takes a minute)...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt pyinstaller || goto :fail

echo.
echo  Building...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile ^
  --name AURA --icon icon.ico ^
  --add-data "templates;templates" ^
  --add-data "version.json;." ^
  --collect-all soundfile ^
  --collect-all faster_whisper ^
  --collect-all ctranslate2 ^
  --collect-all av ^
  --hidden-import _cffi_backend ^
  app.py || goto :fail

copy /y "dist\AURA.exe" "AURA.exe" >nul || goto :fail

rem ---- the installer, if Inno Setup 6 is installed (free: jrsoftware.org/isdl.php) ----
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
  echo.
  echo  Building AURA-Setup.exe...
  "%ISCC%" /Q "/DAppVersion=0.0.0-local" installer.iss && copy /y "dist\AURA-Setup.exe" "AURA-Setup.exe" >nul
) else (
  echo.
  echo  ^(Tip: install Inno Setup 6 to also get AURA-Setup.exe from this script.^)
)

echo.
echo  Checking the new exe...
"AURA.exe" --selftest
echo.
echo  ================================
echo   Done!  AURA.exe is in this folder:
echo   %~dp0AURA.exe
echo  ================================
echo.
pause
exit /b 0

:fail
echo.
echo  Something went wrong - scroll up for the red error text,
echo  or take a screenshot of this window.
echo.
pause
exit /b 1
