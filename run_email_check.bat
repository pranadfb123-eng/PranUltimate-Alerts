@echo off
cd /d "%~dp0"
set PYTHON=python
python -B "%~dp0intraday\email_report.py" > "%~dp0email_report_output.txt" 2>&1
echo Exit code: %ERRORLEVEL% >> "%~dp0email_report_output.txt"
