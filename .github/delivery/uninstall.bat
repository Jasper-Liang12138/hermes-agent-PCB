@echo off
chcp 65001 >nul
echo 此操作将删除 %USERPROFILE%\.hermes\（包含记忆、配置、skill）
set /p CONFIRM=确认删除？(y/N):
if /i "%CONFIRM%" neq "y" exit /b 0
rmdir /S /Q "%USERPROFILE%\.hermes"
echo 已卸载。
pause
