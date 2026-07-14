@echo off
REM PranUltimate — Daily Bot Report Emailer
REM Runs email_report.py for today's date.
REM Scheduled by Task Scheduler at 3:45 PM IST, Mon-Fri.

cd /d "%~dp0"

REM Read Python path from intraday_config.json if present, otherwise use default
set PYTHON=python

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set PYTHON=C:\Users\prana\AppData\Local\Programs\Python\Python313\python.exe
)

echo [%date% %time%] Starting email report...
%PYTHON% -B "%~dp0intraday\email_report.py"

if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] Email sent successfully.
) else (
    echo [%date% %time%] Email report FAILED with exit code %ERRORLEVEL%.
)
