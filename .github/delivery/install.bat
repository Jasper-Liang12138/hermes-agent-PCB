@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

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

if exist "%SCRIPT_DIR%memories\intention_memory.md" (
    if not exist "%HERMES%\memories\MEMORY.md" (
        copy /Y "%SCRIPT_DIR%memories\intention_memory.md" "%HERMES%\memories\MEMORY.md" >nul
        echo [OK] PCB 意图识别 memory 已安装
    ) else (
        echo [OK] MEMORY.md 已存在，跳过
    )
)


if exist "%SCRIPT_DIR%SOUL.md" (
    if not exist "%HERMES%\SOUL.md" (
        copy /Y "%SCRIPT_DIR%SOUL.md" "%HERMES%\SOUL.md" >nul
        echo [OK] PCB Agent SOUL.md installed
    ) else (
        fc /B "%SCRIPT_DIR%SOUL.md" "%HERMES%\SOUL.md" >nul
        if errorlevel 1 (
            for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set SOUL_TS=%%I
            set SOUL_BACKUP=%HERMES%\SOUL.md.bak-!SOUL_TS!
            copy /Y "%HERMES%\SOUL.md" "!SOUL_BACKUP!" >nul
            copy /Y "%SCRIPT_DIR%SOUL.md" "%HERMES%\SOUL.md" >nul
            echo [OK] PCB Agent SOUL.md upgraded
            echo [OK] Previous SOUL.md backup: "!SOUL_BACKUP!"
        ) else (
            echo [OK] SOUL.md unchanged
        )
    )
) else (
    echo [WARN] SOUL.md not found in delivery directory, skipped
)

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
