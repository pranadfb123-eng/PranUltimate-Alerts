@echo off
:: ============================================================
:: setup_bot_scanner_tasks.bat
:: Run ONCE to register the bot (9:00 AM) and scanner (4:00 PM)
:: as weekday Task Scheduler tasks.
:: ============================================================

echo Registering PranUltimate bot and scanner tasks...

set BOT_SCRIPT=C:\Users\prana\PranUltimate\run_bot.bat
set SCAN_SCRIPT=C:\Users\prana\PranUltimate\run_scanner.bat

:: Delete old tasks if they exist (clean re-register)
schtasks /delete /tn "PranUltimate_Bot" /f >nul 2>&1
schtasks /delete /tn "PranUltimate_Scanner" /f >nul 2>&1

:: Bot: starts at 9:00 AM, Mon-Fri
:: bot.py runs until 3:20 PM and exits on its own
schtasks /create /tn "PranUltimate_Bot" /tr "%BOT_SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:00 /f

:: Scanner: starts at 4:00 PM, Mon-Fri
:: scan.py runs for ~2 hours then exits on its own
schtasks /create /tn "PranUltimate_Scanner" /tr "%SCAN_SCRIPT%" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:00 /f

echo.
echo Done. Tasks registered:
echo   PranUltimate_Bot     - weekdays at 09:00 AM
echo   PranUltimate_Scanner - weekdays at 04:00 PM
echo.
echo Verify with:
echo   schtasks /query /tn "PranUltimate_Bot"
echo   schtasks /query /tn "PranUltimate_Scanner"
echo.
pause
