@echo off
REM Send one command to the daemon: toggle (default), start, stop, cancel,
REM status, info, ping, note, lang, meeting, shutdown.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "%~dp0wf_toggle.py" %*
endlocal
