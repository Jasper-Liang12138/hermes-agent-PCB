param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\dist\PCB-AGENT"),
    [switch]$SkipBuild,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $RepoRoot "dist\agent-gateway"
$DeliverySrc = Join-Path $RepoRoot ".github\delivery"
$HardwareSkillsSrc = Join-Path $RepoRoot "skills\hardware"
$DocsSrc = Join-Path $RepoRoot "docs"

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
foreach ($name in @("install.bat", "start.bat", "uninstall.bat", "sync_config.ps1", "template.env", "template-config.yaml", "使用说明.md", "开发测试包使用说明.md")) {
    $src = Join-Path $DeliverySrc $name
    if (Test-Path $src) {
        Copy-Item -Force $src $OutputDir
    }
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

Write-Info "Copying docs..."
if (Test-Path $DocsSrc) {
    Copy-Files (Join-Path $DocsSrc "*") (Join-Path $OutputDir "docs")
}

if ((Test-Path (Join-Path $OutputDir "sync_config.ps1")) -and (Test-Path (Join-Path $OutputDir "config.ini"))) {
    Write-Info "Syncing external config.ini into packaged runtime..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OutputDir "sync_config.ps1")
}

Write-Ok "Delivery package ready: $OutputDir"
