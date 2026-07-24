@echo off
setlocal
cd /d "%~dp0"

set "WEB_PYTHON=D:\Anaconda\python.exe"
if exist "%WEB_PYTHON%" goto launch

for %%P in (python.exe) do set "WEB_PYTHON=%%~$PATH:P"
if defined WEB_PYTHON goto launch

echo python.exe was not found. Install Python or add it to PATH.
pause
exit /b 1

:launch
start "Douyin Creator Monitor Web" "%WEB_PYTHON%" "%~dp0scripts\web_dashboard.py"
exit /b 0
