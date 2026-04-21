@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%USERPROFILE%\.hermes\.env" (
    echo 未检测到配置，请先运行 install.bat
    pause
    exit /b 1
)

agent.exe %*
