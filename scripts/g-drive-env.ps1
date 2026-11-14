# Dot-source this file before running any project command.
# Keeps every cache, temp file and downloaded runtime on the G: checkout.
# Windows PowerShell 5.1 compatible; does not change machine-wide settings.
$ErrorActionPreference = 'Stop'
$gRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$gCache = Join-Path $gRoot '.omniharness\cache'
foreach ($name in @('uv', 'python', 'pip', 'npm', 'tmp', 'playwright', 'xdg', 'hf')) {
    [System.IO.Directory]::CreateDirectory((Join-Path $gCache $name)) | Out-Null
}
$env:UV_CACHE_DIR = Join-Path $gCache 'uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $gCache 'python'
$env:PIP_CACHE_DIR = Join-Path $gCache 'pip'
$env:npm_config_cache = Join-Path $gCache 'npm'
$env:NPM_CONFIG_CACHE = Join-Path $gCache 'npm'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $gCache 'playwright'
$env:XDG_CACHE_HOME = Join-Path $gCache 'xdg'
$env:HF_HOME = Join-Path $gCache 'hf'
$gTmp = Join-Path $gCache 'tmp'
$env:TEMP = $gTmp
$env:TMP = $gTmp
$env:TMPDIR = $gTmp
Write-Output "G-drive environment active. Root: $gRoot"
