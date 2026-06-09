param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\dist\PCB-AGENT"),
    [string]$ModelConfigPath = $env:PCB_AGENT_MODEL_CONFIG_TXT,
    [string]$RouterSourceDir = $env:PCB_AGENT_ROUTER_SOURCE_DIR,
    [string]$PythonRuntimeSourceDir = $env:PCB_AGENT_PYTHON_RUNTIME_DIR,
    [string]$ModelSourceDir = $env:PCB_AGENT_MODEL_DIR,
    [switch]$SkipBuild,
    [switch]$SkipDeps,
    [switch]$IncludeDocs
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $RepoRoot "dist\agent-gateway"
$DeliverySrc = Join-Path $RepoRoot ".github\delivery"
$HardwareSkillsSrc = Join-Path $RepoRoot "skills\hardware"
$DocsSrc = Join-Path $RepoRoot "docs"
$ConfigSrc = Join-Path $RepoRoot "config.ini"
$ModelConfigSrc = if ($ModelConfigPath) { $ModelConfigPath } else { Join-Path $RepoRoot "model_config.txt" }
$DefaultMemoriesSrc = Join-Path $DeliverySrc "memories"
$PreferredRouterSourceDir = "F:\doctor\hermes-agent\邮件\new_routers\routers"
$DefaultRouterSourceDir = if (Test-Path $PreferredRouterSourceDir) { $PreferredRouterSourceDir } else { Join-Path $RepoRoot "routers" }
$DefaultPythonRuntimeSourceDir = Join-Path $RepoRoot "python_runtime"
$DefaultModelSourceDir = Join-Path $RepoRoot "model"

if (-not $RouterSourceDir) { $RouterSourceDir = $DefaultRouterSourceDir }
if (-not $PythonRuntimeSourceDir -and (Test-Path $DefaultPythonRuntimeSourceDir)) {
    $PythonRuntimeSourceDir = $DefaultPythonRuntimeSourceDir
}
if (-not $ModelSourceDir -and (Test-Path $DefaultModelSourceDir)) {
    $ModelSourceDir = $DefaultModelSourceDir
}

function Write-Info($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }

function Assert-Command($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "找不到命令: $name"
    }
}

function Copy-Files($srcPattern, $dst) {
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Recurse -Force $srcPattern $dst
}

function Copy-DirectoryFresh($src, $dst) {
    if (-not (Test-Path $src)) {
        throw "源目录不存在: $src"
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        robocopy $src $dst /MIR /MT:16 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -gt 7) {
            throw "robocopy 失败: $src -> $dst (exit $LASTEXITCODE)"
        }
        return
    }
    if (Test-Path $dst) {
        Remove-Item -Recurse -Force $dst
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
    }
    Copy-Item -Recurse -Force (Join-Path $src "*") $dst
}

function Copy-RouterDir($srcRoot, $srcNameCandidates, $dstName) {
    $src = $null
    foreach ($name in $srcNameCandidates) {
        $candidate = Join-Path $srcRoot $name
        if (Test-Path $candidate) {
            $src = $candidate
            break
        }
    }
    if (-not $src) {
        Write-Warn "未找到 router 源目录: $($srcNameCandidates -join ', ') under $srcRoot"
        return
    }
    Copy-DirectoryFresh $src (Join-Path $OutputDir "routers\$dstName")
}

function Update-DeliveryConfigRouterPaths($configPath) {
    if (-not (Test-Path $configPath)) {
        return
    }
    $text = Get-Content -Raw -Encoding UTF8 $configPath
    $text = $text `
        -replace '(?m)^(arc_dir\s*=\s*).+$', '$1.\routers\arc_windows' `
        -replace '(?m)^(135_dir\s*=\s*).+$', '$1.\routers\135_windows' `
        -replace '(?m)^(rl_arc_dir\s*=\s*).+$', '$1.\routers\arc_windows' `
        -replace '(?m)^(rl_135_dir\s*=\s*).+$', '$1.\routers\135_windows'
    Set-Content -Path $configPath -Value $text -Encoding UTF8
}

Write-Info "Repo root: $RepoRoot"
Write-Info "Output dir: $OutputDir"

if (-not $SkipBuild) {
    Assert-Command python
    Write-Info "Installing build dependencies..."
    python -m pip install --upgrade pip
    if (-not $SkipDeps) {
        python -m pip install -e ".[all]"
    }
    python -m pip install pyinstaller

    Write-Info "Building agent-gateway..."
    Push-Location $RepoRoot
    try {
        python -m PyInstaller agent-gateway.spec --noconfirm
    } finally {
        Pop-Location
    }
    Write-Ok "Build finished"
}

if (-not (Test-Path $BuildDir)) {
    throw "未找到构建输出目录: $BuildDir"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

Write-Info "Copying built runtime files..."
Copy-Item -Recurse -Force (Join-Path $BuildDir "*") $OutputDir

Write-Info "Copying delivery scripts..."
foreach ($name in @("install.bat", "start.bat", "uninstall.bat", "stop-agent-api.bat", "sync_config.ps1", "template.env", "template-config.yaml", "README.md")) {
    $src = Join-Path $DeliverySrc $name
    if (Test-Path $src) {
        Copy-Item -Force $src $OutputDir
    }
}

if (Test-Path $ConfigSrc) {
    Write-Info "Copying editable config.ini..."
    Copy-Item -Force $ConfigSrc $OutputDir
    Update-DeliveryConfigRouterPaths (Join-Path $OutputDir "config.ini")
} else {
    Write-Warn "未找到 config.ini: $ConfigSrc"
}

if (Test-Path $ModelConfigSrc) {
    Write-Info "Copying editable model_config.txt..."
    Copy-Item -Force $ModelConfigSrc $OutputDir
}

Write-Info "Copying default memories..."
if (Test-Path $DefaultMemoriesSrc) {
    Copy-Files (Join-Path $DefaultMemoriesSrc "*") (Join-Path $OutputDir "memories")
} else {
    Write-Warn "未找到默认 memories 目录: $DefaultMemoriesSrc"
}

Write-Info "Copying PCB skills..."
if (Test-Path $HardwareSkillsSrc) {
    foreach ($skillName in @("pcb-intelligence", "pcb-reroute")) {
        $skillSrc = Join-Path $HardwareSkillsSrc $skillName
        if (Test-Path $skillSrc) {
            $skillDst = Join-Path $OutputDir "skills\hardware\$skillName"
            New-Item -ItemType Directory -Force -Path $skillDst | Out-Null
            Copy-Item -Recurse -Force (Join-Path $skillSrc "*") $skillDst
        } else {
            Write-Warn "未找到 skill 源目录: $skillSrc"
        }
    }
} else {
    Write-Warn "未找到 hardware skill 源目录: $HardwareSkillsSrc"
}

Write-Info "Copying routers with PCB-AGENT delivery names..."
if (Test-Path $RouterSourceDir) {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "routers") | Out-Null
    Copy-RouterDir $RouterSourceDir @("135_windows", "135_windows_0519") "135_windows"
    Copy-RouterDir $RouterSourceDir @("arc_windows", "arc_windows_0519") "arc_windows"
    Copy-RouterDir $RouterSourceDir @("135_linux", "135_linux_0519") "135_linux"
    Copy-RouterDir $RouterSourceDir @("arc_linux", "arc_linux_0519") "arc_linux"
    foreach ($fileName in @("best.pt", "README.md")) {
        $src = Join-Path $RouterSourceDir $fileName
        if (Test-Path $src) {
            Copy-Item -Force $src (Join-Path $OutputDir "routers\$fileName")
        }
    }
} else {
    Write-Warn "未找到 routers 源目录: $RouterSourceDir"
}

Write-Info "Preparing runtime output directories..."
foreach ($dirName in @("logs", "router_work")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir $dirName) | Out-Null
}

Write-Info "Copying python_runtime..."
if ($PythonRuntimeSourceDir -and (Test-Path $PythonRuntimeSourceDir)) {
    Copy-DirectoryFresh $PythonRuntimeSourceDir (Join-Path $OutputDir "python_runtime")
} else {
    Write-Warn "未复制 python_runtime。若需要 RL 功能，请使用 -PythonRuntimeSourceDir 或 PCB_AGENT_PYTHON_RUNTIME_DIR 指定来源。"
}

Write-Info "Copying model directory..."
if ($ModelSourceDir -and (Test-Path $ModelSourceDir)) {
    Copy-DirectoryFresh $ModelSourceDir (Join-Path $OutputDir "model")
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "model") | Out-Null
}

if ($IncludeDocs) {
    Write-Info "Copying docs..."
    if (Test-Path $DocsSrc) {
        Copy-Files (Join-Path $DocsSrc "*") (Join-Path $OutputDir "docs")
    }
} else {
    foreach ($legacyPath in @(
        (Join-Path $OutputDir "docs"),
        (Join-Path $OutputDir "使用说明.md"),
        (Join-Path $OutputDir "开发测试包使用说明.md"),
        (Join-Path $OutputDir "前端接入README.md")
    )) {
        if (Test-Path $legacyPath) {
            Remove-Item -Recurse -Force $legacyPath
        }
    }
}

if ((Test-Path (Join-Path $OutputDir "sync_config.ps1")) -and (Test-Path (Join-Path $OutputDir "config.ini"))) {
    Write-Info "Syncing external config.ini into packaged runtime..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OutputDir "sync_config.ps1")
}

Write-Ok "Delivery package ready: $OutputDir"
