<#
轻量封装 PCB_AGENT_LangGraph Windows exe。
默认打包 LangGraph Agent、配置、工具脚本、routers、DRC vendor、可解释性模型代码/权重和 explain Python runtime。
不会复制旧 Hermes/SWSD 的 memories、skills；只带可解释性模型必需的 python_runtime，避免交付包过度臃肿。

输出目录默认：F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT
运行入口：agent.exe
#>
param(
    [string]$OutputDir = "F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT",
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$Config = ".\config.live.ini",
    [switch]$SkipRouters,
    [switch]$SkipDrcVendor,
    [switch]$SkipExplainModel,
    [switch]$SkipExplainRuntime,
    [string]$ExplainRuntime = ".\runtime\explain_python",
    [string]$SourceExplainRuntime = "",
    [switch]$CreateExplainRuntime,
    [string]$ExplainRuntimePython = "",
    [switch]$Clean,
    [switch]$InstallPyInstaller,
    [switch]$InstallRequirements
)

$ErrorActionPreference = "Stop"
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

function Resolve-ProjectPath([string]$PathValue) {
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $projectRoot $PathValue))
}

function Copy-IfExists([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Host "[skip] Missing: $Source"
        return
    }
    $parent = Split-Path -Parent $Destination
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    Write-Host "[copy] $Source -> $Destination"
}

function Get-RuntimePython([string]$RuntimePath) {
    $embeddedPython = Join-Path $RuntimePath "python.exe"
    if (Test-Path -LiteralPath $embeddedPython) { return $embeddedPython }
    $venvPython = Join-Path $RuntimePath "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    return ""
}

function Ensure-ExplainRuntime([string]$RuntimePath, [string]$SourceRuntimePath) {
    $resolvedRuntime = Resolve-ProjectPath $RuntimePath
    if (Test-Path -LiteralPath $resolvedRuntime) {
        Write-Host "[runtime] Using project explain runtime: $resolvedRuntime"
        return $resolvedRuntime
    }

    if ($SourceRuntimePath) {
        $resolvedSource = Resolve-ProjectPath $SourceRuntimePath
        if (Test-Path -LiteralPath $resolvedSource) {
            Write-Host "[runtime] Copying source explain runtime into project runtime."
            Copy-IfExists $resolvedSource $resolvedRuntime
            return $resolvedRuntime
        }
        Write-Warning "Source explain runtime does not exist: $resolvedSource"
    }

    if (-not $CreateExplainRuntime) {
        return ""
    }

    $builder = Join-Path $projectRoot "scripts\build_explain_runtime.ps1"
    if (-not (Test-Path -LiteralPath $builder)) {
        throw "Explain runtime builder not found: $builder"
    }
    $runtimePython = $ExplainRuntimePython
    if (-not $runtimePython) { $runtimePython = $pythonPath }
    Write-Host "[runtime] Creating explain runtime at $resolvedRuntime"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $builder -TargetRuntime $RuntimePath -Python $runtimePython -CreateVenv -InstallRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create explain runtime."
    }
    if (-not (Test-Path -LiteralPath $resolvedRuntime)) {
        throw "Explain runtime was not created: $resolvedRuntime"
    }
    return $resolvedRuntime
}

$pythonPath = Resolve-ProjectPath $Python
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = $Python }

& $pythonPath -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Python is not runnable: $pythonPath" }

$pyInstallerOk = $true
& $pythonPath -m PyInstaller --version | Out-Host
if ($LASTEXITCODE -ne 0) { $pyInstallerOk = $false }
$requirementsPath = Join-Path $projectRoot "requirements.txt"
if (Test-Path -LiteralPath $requirementsPath) {
    & $pythonPath -c "import langgraph, langchain_core, websockets; print('runtime requirements ok')"
    if ($LASTEXITCODE -ne 0) {
        if (-not $InstallRequirements) {
            throw "Runtime requirements are missing. Re-run with -InstallRequirements or install requirements.txt in the selected Python environment."
        }
        & $pythonPath -m pip install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw "Failed to install runtime requirements." }
    }
}
if (-not $pyInstallerOk) {
    if (-not $InstallPyInstaller) {
        throw "PyInstaller is not installed. Re-run with -InstallPyInstaller or install it in the selected Python environment."
    }
    & $pythonPath -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }
}

$distRoot = Resolve-ProjectPath ".\dist\pyinstaller"
$workRoot = Resolve-ProjectPath ".\build\pyinstaller"
$outPath = Resolve-ProjectPath $OutputDir

if ($Clean) {
    foreach ($path in @($distRoot, $workRoot, $outPath)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
}

& $pythonPath -m PyInstaller .\pcb-agent-langgraph.spec --noconfirm --clean --distpath $distRoot --workpath $workRoot
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$builtDir = Join-Path $distRoot "PCB-AGENT-langgraph"
if (-not (Test-Path -LiteralPath $builtDir)) { throw "PyInstaller output not found: $builtDir" }

if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outPath) | Out-Null
Copy-Item -LiteralPath $builtDir -Destination $outPath -Recurse -Force

$configPath = Resolve-ProjectPath $Config
if (Test-Path -LiteralPath $configPath) {
    Copy-Item -LiteralPath $configPath -Destination (Join-Path $outPath "config.ini") -Force
} elseif (Test-Path -LiteralPath (Join-Path $projectRoot "config.example.ini")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "config.example.ini") -Destination (Join-Path $outPath "config.ini") -Force
} else {
    Write-Warning "No config file copied. Provide config.ini before running the exe."
}

Copy-IfExists (Join-Path $projectRoot "config.example.ini") (Join-Path $outPath "config.example.ini")
Copy-IfExists (Join-Path $projectRoot "requirements.txt") (Join-Path $outPath "requirements.txt")
Copy-IfExists (Join-Path $projectRoot "requirements-explain.txt") (Join-Path $outPath "requirements-explain.txt")
Copy-IfExists (Join-Path $projectRoot "README.md") (Join-Path $outPath "README.md")
Copy-IfExists (Join-Path $projectRoot "convert.py") (Join-Path $outPath "convert.py")
Copy-IfExists (Join-Path $projectRoot "tools") (Join-Path $outPath "tools")

if (-not $SkipRouters) { Copy-IfExists (Join-Path $projectRoot "routers") (Join-Path $outPath "routers") }
if (-not $SkipDrcVendor) { Copy-IfExists (Join-Path $projectRoot "vendor") (Join-Path $outPath "vendor") }
if (-not $SkipExplainModel) { Copy-IfExists (Join-Path $projectRoot "explain_model") (Join-Path $outPath "explain_model") }
if (-not $SkipExplainRuntime) {
    $runtimeSource = Ensure-ExplainRuntime $ExplainRuntime $SourceExplainRuntime
    if ($runtimeSource -and (Test-Path -LiteralPath $runtimeSource)) {
        Copy-IfExists $runtimeSource (Join-Path $outPath "runtime\explain_python")
        $packageConfig = Join-Path $outPath "config.ini"
        if (Test-Path -LiteralPath $packageConfig) {
            $configText = Get-Content -Raw -Path $packageConfig
            $packageRuntime = Join-Path $outPath "runtime\explain_python"
            $runtimePython = Get-RuntimePython $packageRuntime
            $configRuntime = ".\runtime\explain_python\python.exe"
            if ($runtimePython -like "*\Scripts\python.exe") {
                $configRuntime = ".\runtime\explain_python\Scripts\python.exe"
            }
            $configText = [regex]::Replace($configText, "(?m)^python_executable\s*=.*$", "python_executable = $configRuntime")
            Set-Content -Path $packageConfig -Value $configText -Encoding UTF8
            Write-Host "[config] explain_model.python_executable -> $configRuntime"
        }
    } else {
        Write-Warning "Explain runtime not found. Checked project runtime: $ExplainRuntime. Pass -SourceExplainRuntime or -CreateExplainRuntime."
    }
}

$startBat = @(
    "@echo off",
    "setlocal",
    "cd /d %~dp0",
    "agent.exe --config config.ini %*"
)
# ====== 功能：生成前端约定的启动脚本 ======
Set-Content -Path (Join-Path $outPath "start.bat") -Value $startBat -Encoding ASCII

$stopBat = @(
    "@echo off",
    "setlocal",
    "powershell -NoProfile -ExecutionPolicy Bypass -Command `"`$root=[IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); `$exe=[IO.Path]::Combine(`$root,'agent.exe'); `$procs=@(Get-CimInstance Win32_Process -Filter \`"Name = 'agent.exe'\`" | Where-Object { `$_.ExecutablePath -and ([IO.Path]::GetFullPath(`$_.ExecutablePath) -ieq `$exe) }); if (`$procs.Count -eq 0) { Write-Host 'agent.exe is not running.'; exit 0 }; foreach (`$p in `$procs) { Stop-Process -Id `$p.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host 'agent.exe stopped.'`""
)
# ====== 功能：生成前端约定的停止脚本 ======
Set-Content -Path (Join-Path $outPath "stop-agent-api.bat") -Value $stopBat -Encoding ASCII

$exePath = Join-Path $outPath "agent.exe"
Write-Host ""
Write-Host "Lite package ready: $outPath"
Write-Host "Executable: $exePath"
Write-Host "Start: $(Join-Path $outPath 'start.bat')"
Write-Host "Stop: $(Join-Path $outPath 'stop-agent-api.bat')"
Write-Host "Config: $(Join-Path $outPath 'config.ini')"
Write-Host "Explain runtime: $(Join-Path $outPath 'runtime\explain_python')"

