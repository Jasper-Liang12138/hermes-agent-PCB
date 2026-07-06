<#
构建可解释性模型运行环境。
- SourceRuntime 是本机已有 python_runtime 的可选复制来源；远端 GitHub 用户需要改成自己的路径。
- 如果没有 SourceRuntime，可先用项目内 requirements-explain.txt 在 TargetRuntime 创建环境，再把 config.ini 指向 TargetRuntime\python.exe。
#>
param(
    [string]$SourceRuntime = "",
    [string]$TargetRuntime = ".\runtime\explain_python",
    [string]$Python = "python",
    [switch]$CreateVenv,
    [switch]$Force,
    [switch]$InstallRequirements
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$targetPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $TargetRuntime))
$requirementsPath = Join-Path $projectRoot "requirements-explain.txt"

Write-Host "Project root: $projectRoot"
Write-Host "Target runtime: $targetPath"

if ((Test-Path -LiteralPath $targetPath) -and $Force) {
    Remove-Item -LiteralPath $targetPath -Recurse -Force
}

if ($SourceRuntime) {
    # Copy mode: use this when you already have a tested explain-model Python runtime.
    # IMPORTANT: SourceRuntime is machine-local. Developers who clone from GitHub must
    # pass their own runtime path, or use -CreateVenv to build one from requirements.
    $sourcePath = [System.IO.Path]::GetFullPath($SourceRuntime)
    Write-Host "Source runtime: $sourcePath"
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Source runtime does not exist: $sourcePath"
    }
    if ((Test-Path -LiteralPath $targetPath) -and -not $Force) {
        Write-Host "Target runtime already exists. Use -Force to rebuild: $targetPath"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
        robocopy $sourcePath $targetPath /MIR /XD __pycache__ .pytest_cache /XF *.pyc | Out-Host
        if ($LASTEXITCODE -gt 7) {
            throw "robocopy failed with exit code $LASTEXITCODE"
        }
    }
} elseif ($CreateVenv) {
    # Portable mode: use this on a fresh clone. It creates the runtime from source
    # using requirements-explain.txt. Network/index access may be required.
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "requirements-explain.txt not found: $requirementsPath"
    }
    if (Test-Path -LiteralPath $targetPath) {
        Write-Host "Target runtime already exists. Use -Force to rebuild: $targetPath"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
        & $Python -m venv $targetPath
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create venv with Python command: $Python"
        }
    }
    $InstallRequirements = $true
} else {
    throw "Choose one mode: pass -SourceRuntime <path> to copy an existing runtime, or pass -CreateVenv to build from requirements-explain.txt."
}

$pythonExe = Join-Path $targetPath "python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = Join-Path $targetPath "Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "python.exe not found in target runtime: $targetPath"
}

if ($InstallRequirements) {
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "requirements-explain.txt not found: $requirementsPath"
    }
    & $pythonExe -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install explain model requirements."
    }
}

& $pythonExe -c "import torch, torchvision, PIL, numpy, matplotlib; print('explain runtime ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Explain runtime import check failed."
}

Write-Host "Explain runtime is ready: $pythonExe"
Write-Host "Use this in config.ini:"
Write-Host "[explain_model]"
Write-Host "python_executable = $pythonExe"

