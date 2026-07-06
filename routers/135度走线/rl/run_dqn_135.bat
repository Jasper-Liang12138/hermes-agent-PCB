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
if "%~1"=="" goto run_dqn
set EXTRA_ARGS=%EXTRA_ARGS% %1
shift
goto collect_args

:run_dqn
python "%~dp0train_dqn_135.py" ^
  --device auto ^
  --tag dqn_135_windows ^
  --n-envs 4 ^
  --max-episode-steps 12 ^
  --hidden-dim 256 ^
  --lr 4e-4 ^
  --gamma 0.975 ^
  --batch-size 64 ^
  --replay-size 6000 ^
  --warmup 40 ^
  --train-every 4 ^
  --target-update 40 ^
  --eps-start 0.9 ^
  --eps-end 0.04 ^
  --prune-strength 0.75 ^
  --eval-budget "%EVAL_BUDGET%" ^
  %EXTRA_ARGS%
