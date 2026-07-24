@echo off
setlocal
cd /d "%~dp0"

set "GUI_PYTHONW=D:\Anaconda\pythonw.exe"
if exist "%GUI_PYTHONW%" goto launch

for %%P in (pythonw.exe) do set "GUI_PYTHONW=%%~$PATH:P"
if defined GUI_PYTHONW goto launch

echo pythonw.exe was not found. Install Python or add it to PATH.
pause
exit /b 1

:launch
start "" "%GUI_PYTHONW%" "%~dp0scripts\gui_dashboard.py"
exit /b 0
