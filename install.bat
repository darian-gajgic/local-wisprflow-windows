@echo off
REM Double-click entry point for the local-wisprflow installer.
REM PowerShell scripts cannot be launched by double-click (the default execution policy
REM blocks them), so this shim starts PowerShell with a per-process policy override.
REM Nothing here needs Administrator rights.
setlocal
cd /d "%~dp0"
echo Starting the local-wisprflow installer...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
  echo Installer exited with code %RC%.
  echo If something failed, run  wf-doctor.cmd  for a diagnosis.
)
pause
endlocal
