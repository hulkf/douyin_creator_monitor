@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  Douyin Creator Monitor - Daily Pipeline Launcher
REM  Double-click to run, or use via Windows Task Scheduler.
REM  No AI Agent / Codex needed, no sandbox approval overhead.
REM
REM  Argument handling (label-free, robust):
REM    --no-pause  : consumed ONLY by this bat to suppress the
REM                  interactive "pause" at the end (used when
REM                  triggered by Task Scheduler / Agent). It is
REM                  NEVER forwarded to the Python pipeline.
REM    all other args (e.g. --creator zhiliao --max-works 3)
REM                  are forwarded verbatim to run_creator_pipeline.py.
REM ============================================================

cd /d "D:\JR_project\douyin_creator_monitor"

set PYTHON_EXE=D:\Anaconda\python.exe
set PIPELINE=D:\JR_project\douyin_creator_monitor\scripts\run_creator_pipeline.py
REM Keep redirected Python output UTF-8 for every stage, including reconcile.
set PYTHONIOENCODING=utf-8

REM ---- parse args without goto/labels (labels fail in some hosts) ----
REM  RAW always starts with a sentinel space. Without it, cmd expands the
REM  substitution on an empty %* to the literal "--no-pause=". The same
REM  space also keeps forwarded flags separated from the Python script path.
set "RAW= %*"
set "PYARGS=%RAW: --no-pause=%"
echo.%RAW% | findstr /i /c:"--no-pause" >nul 2>&1 && set NOPAUSE=1 || set NOPAUSE=0

REM Locale-independent timestamp for the bat-level log file.
REM (powershell -UFormat avoids the single-quote clash that the
REM  python one-liner hits inside a for/f '...' command.)
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -UFormat %%Y%%m%%d-%%H%%M%%S"') do set TS=%%i
REM Use absolute log paths because Task Scheduler may use another working directory.
REM Relative redirection can fail before Python starts and hide all output.
set BATLOG=%~dp0logs\bat-%TS%.log
set NCCLOG=%~dp0logs\new-creator-check-%TS%.log
set RECONLOG=%~dp0logs\reconcile-%TS%.log

echo === Starting pipeline ===
echo Time: %date% %time%
echo Command: "%PYTHON_EXE%" "%PIPELINE%"%PYARGS%
echo.

REM ---- Reconcile the creator base table before collection. ----
REM Recreate missing creator rows or work tables and repair stale pointers.
REM The main pipeline refreshes current profile statistics after collection.
echo === Reconcile creator base records and work tables ===
"%PYTHON_EXE%" "D:\JR_project\douyin_creator_monitor\scripts\check_and_onboard_new_creators.py" --reconcile --apply > "%RECONLOG%" 2>&1
set RECON_EL=%errorlevel%
echo    (reconcile details: %RECONLOG%)
echo.

REM ---- Discover and configure creators newly added to the base table. ----
echo === Checking and auto-onboarding new creators ===
"%PYTHON_EXE%" "D:\JR_project\douyin_creator_monitor\scripts\check_and_onboard_new_creators.py" --apply --no-collect > "%NCCLOG%" 2>&1
set ONBOARD_EL=%errorlevel%
echo    (onboarding details: %NCCLOG%)
echo.

REM Run the pipeline. All stdout/stderr (including any Python crash
REM traceback) is captured to %BATLOG% so nothing is lost on error.
REM Python output is captured in the bat-level log for diagnostics.
"%PYTHON_EXE%" "%PIPELINE%"%PYARGS% > "%BATLOG%" 2>&1
set EL=%errorlevel%

echo.
echo ----- Full output (same as %BATLOG%) -----
type "%BATLOG%"

REM Creator profile is synced inside the per-creator pipeline immediately after a successful capture.
REM This avoids writing stale snapshots for other creators during --creator runs.

REM ============================================================
REM  Final summary block - always shown, success or failure.
REM ============================================================
echo.
echo ############################################################
echo #                       RESULT
echo ############################################################
set FINAL_EL=%EL%
if not "%RECON_EL%"=="0" set FINAL_EL=%RECON_EL%
if not "%ONBOARD_EL%"=="0" set FINAL_EL=%ONBOARD_EL%
if "%FINAL_EL%"=="0" (
  echo #  [SUCCESS] Pipeline finished with NO errors.
  echo #  Exit code: 0
) else (
  echo #  [FAILED]  Pipeline ended with an ERROR.
  echo #  Exit code: %FINAL_EL%
  echo #  Stage codes: reconcile=%RECON_EL% onboarding=%ONBOARD_EL% pipeline=%EL%
  echo #  >> Check the output above or open the log file.
)
echo #  Log file : %BATLOG%
echo ############################################################

REM Keep the window open on manual double-click so you can read the
REM result. When triggered by Task Scheduler / Agent, --no-pause was
REM passed (and consumed above) so the task does not hang on input.
if "%NOPAUSE%"=="0" pause
exit /b %FINAL_EL%
