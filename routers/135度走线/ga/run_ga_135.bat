@echo off
setlocal

if "%~1"=="" (
  echo Usage: %~nx0 ^<eval_budget^> [extra args...]
  exit /b 1
)

set EVAL_BUDGET=%~1
shift
set EXTRA_ARGS=
:collect_args
if "%~1"=="" goto run_ga
set EXTRA_ARGS=%EXTRA_ARGS% %1
shift
goto collect_args

:run_ga
python "%~dp0train_ga_135.py" ^
  --algorithm ga ^
  --tag ga_135_windows ^
  --population-size 32 ^
  --elite-size 4 ^
  --mutation-rate 0.35 ^
  --crossover-rate 0.60 ^
  --local-search-rate 0.25 ^
  --eval-budget "%EVAL_BUDGET%" ^
  %EXTRA_ARGS%
