@echo off
REM nim-agents-ops · launch flask api on localhost:5001
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"
python -m api.app
endlocal
