@echo off
REM Diagnose why dictation is not working.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo wisprflow is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0wf_doctor.py"
pause
endlocal
