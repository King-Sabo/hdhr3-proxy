@echo off
REM ---------------------------------------------------------------
REM  HDHR3 -> Plex bridge.  Edit the settings below, then run.
REM ---------------------------------------------------------------

REM Device ID (from: hdhomerun_config discover). FFFFFFFF = first found.
set DEVICE=1220EEF8

REM eu-cable = DVB-C (cable)   |   eu-bcast = DVB-T (antenna)
set SOURCE=eu-cable

set PORT=5004

REM Everything else. Run check.bat to see the effect before a real grab.
REM   --channels 1-49    --exclude QVC    --no-shopping
REM   --epg-days 2       --epg-dwell 120
set OPTIONS=--epg-days 2 --epg-dwell 120

cd /d "%~dp0"
py -3 hdhr3_proxy.py -d %DEVICE% --channelmap %SOURCE% --port %PORT% %OPTIONS% %*
echo.
echo Proxy stopped.
pause
