@echo off
REM Start the wisprflow daemon in the background (no console window).
REM pythonw.exe is what keeps a black box from sitting on the taskbar forever.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo wisprflow is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0wf_daemon.py"
echo wisprflow is starting - look for its icon in the system tray.
echo The first start takes a minute while the speech model loads.
endlocal
