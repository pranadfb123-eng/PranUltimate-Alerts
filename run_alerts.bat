@echo off
:: ============================================================
:: PranUltimate Alert Runner
:: Called by Windows Task Scheduler at each candle close.
:: Loads Dhan + Telegram credentials, then runs check_alerts.py
:: ============================================================

cd /d C:\Users\prana\PranUltimate

:: ── Create logs folder if it doesn't exist ──────────────────
if not exist "logs" mkdir logs

:: ── Load Telegram secrets from alert_secrets.env ────────────
for /f "usebackq eol=# tokens=1,2 delims==" %%A in ("alert_secrets.env") do (
    if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
)

:: ── Load Dhan credentials from intraday_config.json ─────────
:: Extract client_id and access_token using PowerShell one-liner
for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content intraday_config.json | ConvertFrom-Json).client_id"') do set DHAN_CLIENT_ID=%%i
for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content intraday_config.json | ConvertFrom-Json).access_token"') do set DHAN_ACCESS_TOKEN=%%i

:: ── Find Python ──────────────────────────────────────────────
set PYTHON=
if exist "C:\Users\prana\AppData\Local\Programs\Python\Python313\python.exe" (
    set PYTHON=C:\Users\prana\AppData\Local\Programs\Python\Python313\python.exe
) else if exist "C:\Python313\python.exe" (
    set PYTHON=C:\Python313\python.exe
) else if exist "C:\Users\prana\AppData\Local\Programs\Python\Python314\python.exe" (
    set PYTHON=C:\Users\prana\AppData\Local\Programs\Python\Python314\python.exe
) else (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if "!PYTHON!"=="" set PYTHON=%%p
    )
)

if "!PYTHON!"=="" (
    echo [%date% %time%] ERROR: Python not found >> logs\alerts_runner.log
    exit /b 1
)

:: ── Run the checker, appending to daily log ──────────────────
set LOGFILE=logs\alerts_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log
echo. >> %LOGFILE%
echo [%date% %time%] === run_alerts.bat triggered === >> %LOGFILE%
"%PYTHON%" scanner\check_alerts.py >> %LOGFILE% 2>&1
echo [%date% %time%] === done (exit %errorlevel%) === >> %LOGFILE%
