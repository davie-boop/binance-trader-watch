@echo off
chcp 65001 >nul
title Uninstall local scheduled task

echo ============================================================
echo   Uninstall local Binance fetch task
echo ============================================================
echo.
echo Removing scheduled task BinanceTraderFetch15min ...
schtasks /delete /tn "BinanceTraderFetch15min" /f
echo.
echo ---- Verify ----
schtasks /query /tn "BinanceTraderFetch15min" >nul 2>&1
if errorlevel 1 (
    echo [OK] Task removed. Local scheduled fetch is now disabled.
    echo      GitHub Actions will continue fetching every 15 min.
) else (
    echo [FAILED] Task still exists. Make sure you run this as Administrator.
)
echo.
pause
exit /b 0
