@echo off
chcp 65001 >nul
setlocal

set HERMES=%USERPROFILE%\.hermes
set SCRIPT_DIR=%~dp0

echo ============================================
echo  PCB Agent 安装程序
echo ============================================
echo.

mkdir "%HERMES%"                           2>nul
mkdir "%HERMES%\memories"                  2>nul
mkdir "%HERMES%\skills\hardware"           2>nul

xcopy /E /Y /I "%SCRIPT_DIR%skills" "%HERMES%\skills\" >nul
echo [OK] PCB skill 已安装

if not exist "%HERMES%\.env" (
    copy /Y "%SCRIPT_DIR%template.env" "%HERMES%\.env" >nul
    echo [!!] 请填写 API Key，正在打开配置文件...
    notepad "%HERMES%\.env"
) else (
    echo [OK] .env 已存在，跳过
)

if not exist "%HERMES%\config.yaml" (
    copy /Y "%SCRIPT_DIR%template-config.yaml" "%HERMES%\config.yaml" >nul
    echo [OK] config.yaml 已生成
) else (
    echo [OK] config.yaml 已存在，跳过
)

echo.
echo 安装完成！运行 start.bat 启动 Agent。
pause
