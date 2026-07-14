@echo off
REM Registers a Windows Task Scheduler task to email the daily bot report at 3:45 PM IST.
REM Run this once as Administrator (or it will prompt for elevation).

set TASK_NAME=PranUltimate_EmailReport
set BAT_FILE=%~dp0run_email_report.bat

echo Registering scheduled task: %TASK_NAME%
echo Script: %BAT_FILE%
echo Schedule: Mon-Fri at 15:45

schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "cmd /c \"%BAT_FILE%\"" ^
    /sc WEEKLY ^
    /d MON,TUE,WED,THU,FRI ^
    /st 15:45 ^
    /f ^
    /rl HIGHEST

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: Scheduled task "%TASK_NAME%" created.
    echo It will run every weekday at 3:45 PM and email the bot report to pranadfb123@gmail.com.
) else (
    echo.
    echo ERROR: Could not create task. Try running this bat file as Administrator.
)

pause
