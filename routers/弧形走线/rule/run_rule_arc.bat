@echo off
setlocal
if "%~1"=="" (
  echo Usage: %~nx0 ^<eval_budget^> [extra args...]
  exit /b 1
)
set EVAL_BUDGET=%~1
shift
python "%~dp0rule_search_arc.py" --tag rule_arc_exe --eval-budget "%EVAL_BUDGET%" %*
