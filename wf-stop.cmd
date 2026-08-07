@echo off
REM Ask the daemon to shut down cleanly (it finishes any in-flight dictation first).
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "%~dp0wf_toggle.py" shutdown
endlocal
