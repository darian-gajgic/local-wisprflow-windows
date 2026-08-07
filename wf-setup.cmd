@echo off
REM Re-run the guided setup: models, GPU libraries, hotkey, autostart.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo wisprflow is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0wf_setup.py" %*
pause
endlocal
