<#
构建可解释性模型运行环境。
- SourceRuntime 是本机已有 python_runtime 的可选复制来源；远端 GitHub 用户需要改成自己的路径。
- 优先复制 SourceRuntime；如果没有可复制 runtime，则自动用 requirements-explain.txt 创建 TargetRuntime。
#>
param(
    [string]$SourceRuntime = "",
    [string]$TargetRuntime = ".\runtime\explain_python",
    [string]$Python = "python",
    [switch]$CreateVenv,
    [switch]$CopyOnly,
    [switch]$Force,
    [switch]$InstallRequirements
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$targetPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $TargetRuntime))
$requirementsPath = Join-Path $projectRoot "requirements-explain.txt"

Write-Host "Project root: $projectRoot"
Write-Host "Target runtime: $targetPath"

function Get-RuntimePython([string]$RuntimePath) {
    $embeddedPython = Join-Path $RuntimePath "python.exe"
    if (Test-Path -LiteralPath $embeddedPython) { return $embeddedPython }
    $venvPython = Join-Path $RuntimePath "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    return ""
}

function Copy-Runtime([string]$SourcePath, [string]$DestinationPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestinationPath) | Out-Null
    robocopy $SourcePath $DestinationPath /MIR /XD __pycache__ .pytest_cache /XF *.pyc | Out-Host
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
}

function New-ExplainVenv([string]$DestinationPath) {
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "requirements-explain.txt not found: $requirementsPath"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestinationPath) | Out-Null
    & $Python -m venv $DestinationPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create venv with Python command: $Python"
    }
}

if ((Test-Path -LiteralPath $targetPath) -and $Force) {
    Remove-Item -LiteralPath $targetPath -Recurse -Force
}

$copiedRuntime = $false
$createdRuntime = $false

if ($SourceRuntime) {
    # Copy mode: use this when you already have a tested explain-model Python runtime.
    # IMPORTANT: SourceRuntime is machine-local. If it is missing, this script falls
    # back to venv mode unless -CopyOnly is set.
    $sourcePath = [System.IO.Path]::GetFullPath($SourceRuntime)
    Write-Host "Source runtime: $sourcePath"
    if (Test-Path -LiteralPath $sourcePath) {
        if ((Test-Path -LiteralPath $targetPath) -and -not $Force) {
            Write-Host "Target runtime already exists. Use -Force to rebuild: $targetPath"
        } else {
            Copy-Runtime $sourcePath $targetPath
            $copiedRuntime = $true
        }
    } else {
        if ($CopyOnly) {
            throw "Source runtime does not exist and -CopyOnly was set: $sourcePath"
        }
        Write-Warning "Source runtime does not exist: $sourcePath. Falling back to venv creation."
        $CreateVenv = $true
    }
} else {
    $CreateVenv = $true
}

if ($CreateVenv -and -not (Test-Path -LiteralPath $targetPath)) {
    # Portable mode: use this on a fresh clone. It creates the runtime from source
    # using requirements-explain.txt. Network/index access may be required.
    New-ExplainVenv $targetPath
    $createdRuntime = $true
    $InstallRequirements = $true
} elseif ($CreateVenv -and (Test-Path -LiteralPath $targetPath)) {
    Write-Host "Target runtime already exists. Use -Force to rebuild: $targetPath"
    $InstallRequirements = $InstallRequirements -or (-not $copiedRuntime)
}

$pythonExe = Get-RuntimePython $targetPath
if (-not $pythonExe) {
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
    if ($createdRuntime -or $InstallRequirements) {
        throw "Explain runtime import check failed."
    }
    if ($CopyOnly) {
        throw "Explain runtime import check failed and -CopyOnly was set."
    }
    Write-Warning "Existing/copied runtime import check failed. Installing requirements and retrying."
    & $pythonExe -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install explain model requirements."
    }
    & $pythonExe -c "import torch, torchvision, PIL, numpy, matplotlib; print('explain runtime ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Explain runtime import check failed after installing requirements."
    }
}

Write-Host "Explain runtime is ready: $pythonExe"
Write-Host "Use this in config.ini:"
Write-Host "[explain_model]"
Write-Host "python_executable = $pythonExe"

