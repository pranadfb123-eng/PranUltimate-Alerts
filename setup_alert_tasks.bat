@echo off
:: ============================================================
:: setup_alert_tasks.bat
:: Run ONCE as Administrator to register all 7 alert-check
:: tasks in Windows Task Scheduler. Each fires on weekdays
:: at a 1H/2H candle-close boundary (IST times below).
::
:: Schedule (IST):  10:15, 11:15, 12:15, 13:15, 14:15, 15:15, 15:30
:: ============================================================

echo Registering PranUltimate alert tasks...

set SCRIPT=C:\Users\prana\PranUltimate\run_alerts.bat
set TASKPREFIX=PranUltimate_Alert

:: Delete old tasks if they exist (clean re-register)
for %%T in (1015 1115 1215 1315 1415 1515 1530) do (
    schtasks /delete /tn "%TASKPREFIX%_%%T" /f >nul 2>&1
)

:: Create one task per candle close
schtasks /create /tn "%TASKPREFIX%_1015" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 10:15 /f
schtasks /create /tn "%TASKPREFIX%_1115" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 11:15 /f
schtasks /create /tn "%TASKPREFIX%_1215" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 12:15 /f
schtasks /create /tn "%TASKPREFIX%_1315" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 13:15 /f
schtasks /create /tn "%TASKPREFIX%_1415" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 14:15 /f
schtasks /create /tn "%TASKPREFIX%_1515" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:15 /f
schtasks /create /tn "%TASKPREFIX%_1530" /tr "%SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:30 /f

echo.
echo Done. Verify with:  schtasks /query /tn "PranUltimate_Alert_1015"
echo.
pause
