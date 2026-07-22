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
set BAT=D:\JR_project\douyin_creator_monitor\run_daily.bat

REM 每天 03:00 运行（不要用 /sc ONCE /sd 过去日期，那是 9009 隐患源）。
REM 若需无人值守（用户未登录也跑），请在任务计划程序里把该任务的
REM "安全选项"改为"不管用户是否登录都要运行"并保存密码；或在此加 /ru <用户> /rp <密码>。
schtasks /create /tn %TN% /tr "%BAT% --no-pause" /sc DAILY /st 03:00 /f
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
