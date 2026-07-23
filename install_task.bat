@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  Register Windows scheduled task "DouyinCreatorMonitor"
REM  After registration:
REM    Agent trigger : schtasks /run /tn DouyinCreatorMonitor
REM    Manual trigger : double-click run_daily.bat
REM    Check status   : schtasks /query /tn DouyinCreatorMonitor
REM  Usage: double-click this file (Run as Admin if needed)
REM ============================================================

set TN=DouyinCreatorMonitor
set REGISTER_SCRIPT=%~dp0scripts\register_scheduled_task.ps1

REM Run daily at 03:00. The PowerShell helper also sets a stable cmd launcher
REM and project working directory. Interactive logon avoids storing a password.
powershell -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_SCRIPT%"
set CREATE_EL=%errorlevel%
if "%CREATE_EL%"=="0" schtasks /query /tn %TN% >nul 2>&1
if not "%errorlevel%"=="0" set CREATE_EL=%errorlevel%

if "%CREATE_EL%"=="0" (
  echo [OK] Task "%TN%" registered successfully.
  echo.
  echo   Agent trigger : schtasks /run /tn %TN%
  echo   Manual trigger : double-click run_daily.bat
  echo   Check status   : schtasks /query /tn %TN%
) else (
  echo [FAILED] Right-click this file ^> "Run as administrator" and try again.
)
pause
exit /b %CREATE_EL%
