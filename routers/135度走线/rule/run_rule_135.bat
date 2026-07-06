@echo off
setlocal
if "%~1"=="" (
  echo Usage: %~nx0 ^<eval_budget^> [extra args...]
  exit /b 1
)
set EVAL_BUDGET=%~1
shift
python "%~dp0rule_search_135.py" --tag rule_135_exe --eval-budget "%EVAL_BUDGET%" %*
