@echo off
rem Builds the SLAI CADCon installer for Windows. Double-click this file, or run it from a terminal.
rem It needs Node.js, Python 3.12+ (only to run the build script) and Rust (with the Visual Studio C++ Build Tools), and internet access: it downloads the
rem embeddable Python and the packages (including PyTorch, CPU build) that go INTO the installer. Nothing is installed on this computer.
rem This folder must sit in a SLAI checkout (SLAI\applications\CADCon): the installer carries a copy of SLAI's src, checkpointing, logs and tools.
rem The first run takes a long time and downloads a few hundred MB; later runs reuse npm's and cargo's caches.
rem   build-windows.cmd            the standard bundle
rem   build-windows.cmd full       also the extra packages other SLAI agents import (much larger)
setlocal
cd /d "%~dp0"
set PROFILE=standard
if /i "%~1"=="full" set PROFILE=full
echo.
echo === SLAI CADCon: building the Windows installer (profile: %PROFILE%) ===
echo.

if not exist "..\..\src\agents\agent_factory.py" (
  echo This folder is not inside a SLAI checkout: ..\..\src\agents\agent_factory.py was not found.
  goto :fail
)

call npm.cmd install || goto :fail
call npm.cmd run build || goto :fail
rem the native window's start-up page uses the shared start-up screen's stylesheet
call npm.cmd run splash || goto :fail

rem Stage SLAI + CADCon + a Python runtime with all packages into src-tauri\engine, then check the result by starting CADCon from it (--check).
python tools\desktop\build_bundle.py --slai-root ..\.. --profile %PROFILE% || goto :fail

call npm.cmd run tauri -- build || goto :fail

echo.
echo === Done. The installer is in src-tauri\target\release\bundle\nsis ===
start "" "src-tauri\target\release\bundle\nsis"
pause
exit /b 0

:fail
echo.
echo === The build stopped. Read the last lines above. ===
pause
exit /b 1
