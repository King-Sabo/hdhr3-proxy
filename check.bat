@echo off
REM Diagnostic report: tuner, lineup, filters, guide. Touches no tuner.
set DEVICE=1220EEF8
set SOURCE=eu-cable
set PORT=5004
set OPTIONS=--epg-days 2 --epg-dwell 120

cd /d "%~dp0"
py -3 hdhr3_proxy.py -d %DEVICE% --channelmap %SOURCE% --port %PORT% %OPTIONS% --check %*
pause
