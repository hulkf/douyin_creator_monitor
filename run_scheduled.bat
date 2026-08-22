@echo off
setlocal
chcp 65001 >nul 2>&1

cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if exist "D:\Anaconda\python.exe" "D:\Anaconda\python.exe" "%~dp0scripts\rotate_runtime_logs.py" --log-dir "%~dp0logs" >nul 2>&1
set "TASK_LOG=%~dp0logs\task-launch.log"

echo.>>"%TASK_LOG%"
echo ============================================================>>"%TASK_LOG%"
echo [%date% %time%] Scheduled launcher started.>>"%TASK_LOG%"
call "%~dp0run_daily.bat" --no-pause >>"%TASK_LOG%" 2>&1
set "TASK_EL=%errorlevel%"
echo [%date% %time%] Scheduled launcher finished, exit=%TASK_EL%.>>"%TASK_LOG%"

exit /b %TASK_EL%
