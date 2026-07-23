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

REM ---- parse args without goto/labels (labels fail in some hosts) ----
REM  RAW holds every argument passed to this bat. PYARGS is RAW with
REM  every "--no-pause" token stripped out, so only real pipeline
REM  flags reach Python. NOPAUSE is detected via findstr.
set "RAW=%*"
set "PYARGS=%RAW:--no-pause=%"
echo.%RAW% | findstr /i /c:"--no-pause" >nul 2>&1 && set NOPAUSE=1 || set NOPAUSE=0

REM Locale-independent timestamp for the bat-level log file.
REM (powershell -UFormat avoids the single-quote clash that the
REM  python one-liner hits inside a for/f '...' command.)
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -UFormat %%Y%%m%%d-%%H%%M%%S"') do set TS=%%i
REM 日志路径用 %~dp0 绝对化：计划任务的工作目录未必是项目目录，
REM 相对路径 logs\ 会重定向失败且静默吞掉所有输出（曾导致 255 且无日志）。
set BATLOG=%~dp0logs\bat-%TS%.log
set NCCLOG=%~dp0logs\new-creator-check-%TS%.log

echo === Starting pipeline ===
echo Time: %date% %time%
echo Command: "%PYTHON_EXE%" "%PIPELINE%"%PYARGS%
echo.

REM ---- 每次运行前检查飞书《达人基础信息表》是否有新增达人 ----
REM 自动接入：发现新增达人即 --apply --no-collect
REM   · 加入日常更新数据（写入 pipeline.json creators）
REM   · 把信息补充完全：建作品表(若无) + 回写 作品表关联/所属平台/SecUID/
REM     抖音UID/最近检查时间/主页采集状态/达人昵称 到基础信息表
REM 真正的采集交给随后的主流水线（--no-collect 避免重复抓）。
REM 若只想看报告不接入，可手动加 --dry-run 运行该脚本。
echo === Checking & auto-onboarding new creators from 达人基础信息表 ===
"%PYTHON_EXE%" "D:\JR_project\douyin_creator_monitor\scripts\check_and_onboard_new_creators.py" --apply --no-collect > "%NCCLOG%" 2>&1
set ONBOARD_EL=%errorlevel%
echo    (新增达人接入详情见 %NCCLOG%)
echo.

REM Run the pipeline. All stdout/stderr (including any Python crash
REM traceback) is captured to %BATLOG% so nothing is lost on error.
REM PYTHONIOENCODING ensures Chinese text is written as UTF-8 in the log
REM file (without this, Windows defaults to GBK and type shows garbled text).
set PYTHONIOENCODING=utf-8
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
if not "%ONBOARD_EL%"=="0" set FINAL_EL=%ONBOARD_EL%
if "%FINAL_EL%"=="0" (
  echo #  [SUCCESS] Pipeline finished with NO errors.
  echo #  Exit code: 0
) else (
  echo #  [FAILED]  Pipeline ended with an ERROR.
  echo #  Exit code: %FINAL_EL%
  echo #  Stage codes: onboarding=%ONBOARD_EL% pipeline=%EL%
  echo #  >> Check the output above or open the log file.
)
echo #  Log file : %BATLOG%
echo ############################################################

REM Keep the window open on manual double-click so you can read the
REM result. When triggered by Task Scheduler / Agent, --no-pause was
REM passed (and consumed above) so the task does not hang on input.
if "%NOPAUSE%"=="0" pause
exit /b %FINAL_EL%
