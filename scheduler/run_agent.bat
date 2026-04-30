@echo off
REM nim-agents-ops scheduler/run_agent.bat
REM usage: run_agent.bat agent_01_attendance
REM invoked by windows task scheduler. logs go to logs\<agent>_<date>.log
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist logs mkdir logs
set AGENT=%1
set LOGFILE=logs\%AGENT%_%date:~10,4%%date:~4,2%/%date:~7,2%.log
python -m agents.%AGENT% >> "%LOGFILE%" 2>&1
endlocal
