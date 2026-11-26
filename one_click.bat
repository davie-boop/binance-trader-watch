@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=C:\Users\pingf\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "TASK=BinanceTraderFetch15min"
set "TRADER=5075281354358777856"

REM ============================================================
REM  Mode dispatch. Task Scheduler calls this file with --fetch.
REM ============================================================
if /i "%~1"=="--fetch" goto FETCH
if /i "%~1"=="--dashboard" goto DASHBOARD
if /i "%~1"=="--status" goto STATUS
if /i "%~1"=="--uninstall" goto UNINSTALL
if /i "%~1"=="--test-notify" goto TESTNOTIFY
goto SETUP

REM ---- silent fetch (called by Task Scheduler every 15 min) ----
:FETCH
if not exist "logs" mkdir "logs"
"%PY%" -m trader_watcher.cli trades --limit 3 >> "logs\schedule_fetch.log" 2>&1
echo %date% %time% >> "logs\schedule_fetch.log" 2>&1
exit /b 0

REM ---- default: one-click setup + immediate fetch ----
:SETUP
title Binance Trader Watch - One Click Setup
echo ============================================================
echo   One-click setup   -  trader %TRADER%
echo ============================================================
echo.
echo [1/2] Installing scheduled task (every 15 minutes)...
schtasks /create /tn "%TASK%" /tr "\"%~f0\" --fetch" /sc minute /mo 15 /f >nul 2>&1
if errorlevel 1 (
    echo       [FAILED] Right-click this file and choose "Run as administrator".
) else (
    echo       [OK] Task installed: %TASK%
)
echo.
echo [2/2] Fetching latest 3 records now...
echo.
if not exist "logs" mkdir "logs"
"%PY%" -m trader_watcher.cli trades --limit 3
echo.
echo ============================================================
echo  Done. Task runs every 15 min even if WorkBuddy is closed.
echo  Log: logs\schedule_fetch.log
echo.
echo  Next action:
echo    [1] Realtime dashboard (10s refresh)
echo    [2] Check task status and log
echo    [3] Uninstall scheduled task
echo    [4] Test WeChat notification
echo    [0] Exit
echo ============================================================
set "N="
set /p "N=Enter choice [0]: "
if "%N%"=="1" goto DASHBOARD
if "%N%"=="2" goto STATUS
if "%N%"=="3" goto UNINSTALL
if "%N%"=="4" goto TESTNOTIFY
exit /b 0

:TESTNOTIFY
title Binance Trader Watch - Test WeChat Notification
echo Sending a test message to WeChat...
"%PY%" -m trader_watcher.cli test-notify
pause
exit /b 0

:DASHBOARD
title Binance Trader Watch - Realtime Dashboard
echo Starting realtime dashboard. Press q to quit, number to switch trader.
"%PY%" -m trader_watcher.cli live --limit 5 --interval 10
pause
exit /b 0

:STATUS
echo.
echo ---- Scheduled task ----
schtasks /query /tn "%TASK%" /fo LIST
echo.
echo ---- Fetch log (tail) ----
if exist "logs\schedule_fetch.log" (
    for /f "skip=1 delims=" %%A in ('type "logs\schedule_fetch.log"') do echo %%A
) else (
    echo No log file yet.
)
echo.
pause
exit /b 0

:UNINSTALL
echo.
echo Removing scheduled task %TASK% ...
schtasks /delete /tn "%TASK%" /f
echo.
pause
exit /b 0
