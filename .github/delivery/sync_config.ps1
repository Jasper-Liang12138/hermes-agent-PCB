$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "config.ini"
$dst = Join-Path $root "_internal\config.ini"

if (-not (Test-Path -LiteralPath $src)) {
    throw "config.ini not found: $src"
}
if (-not (Test-Path -LiteralPath (Split-Path -Parent $dst))) {
    throw "_internal directory not found: $(Split-Path -Parent $dst)"
}

$text = [System.IO.File]::ReadAllText($src).TrimStart([char]0xFEFF)
[System.IO.File]::WriteAllText($dst, $text, [System.Text.UTF8Encoding]::new($false))
