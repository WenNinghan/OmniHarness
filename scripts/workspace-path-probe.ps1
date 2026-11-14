param(
    [Parameter(Mandatory = $true)][string]$LiteralPath,
    [string]$WslDistro,
    [switch]$ProbeWrite
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$nativePython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nativeModule = Join-Path $projectRoot 'src\omniharness\workspace_io.py'
if (-not (Test-Path -LiteralPath $nativePython -PathType Leaf)) {
    throw 'The fixed native Python helper is unavailable; M4 workspace access must remain disabled.'
}
if ($LiteralPath -notmatch '^[A-Za-z]:[\\/]' -or $LiteralPath.Length -le 3) {
    throw 'Only a local NTFS project directory below a drive root is supported.'
}
$identityRaw = & $nativePython -I $nativeModule --inspect-path $LiteralPath
if ($LASTEXITCODE -ne 0) { throw 'Native NTFS path validation rejected the workspace.' }
$identity = ($identityRaw -join '') | ConvertFrom-Json
if (-not $identity.directory) { throw 'Workspace must be a directory.' }
$result = [ordered]@{
    schema_version = 1
    host_path = $identity.path
    native_identity = $identity
    wsl_path = $null
    wsl_same_object = $false
    write_probe = $false
}
if ($WslDistro) {
    if ($WslDistro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distribution name.' }
    if (-not $ProbeWrite) { throw 'WSL same-object verification requires the explicit -ProbeWrite option.' }
    $mapped = & wsl.exe -d $WslDistro --exec wslpath -a $identity.path
    if ($LASTEXITCODE -ne 0 -or -not $mapped) { throw 'WSL path mapping failed.' }
    $mapped = ($mapped -join '').Trim()
    $name = '.omni-path-probe-' + [guid]::NewGuid().ToString('N')
    $probePath = Join-Path $identity.path $name
    $nonce = [guid]::NewGuid().ToString('N')
    # CreateNew never overwrites a user file; cleanup names only this random file.
    $stream = [System.IO.File]::Open($probePath, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($nonce)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    try {
        $fileRaw = & $nativePython -I $nativeModule --inspect-path $probePath
        if ($LASTEXITCODE -ne 0) { throw 'Native probe-file identity validation failed.' }
        $fileIdentity = ($fileRaw -join '') | ConvertFrom-Json
        $linuxProbe = $mapped.TrimEnd('/') + '/' + $name
        $linuxNonce = & wsl.exe -d $WslDistro --exec cat -- $linuxProbe
        if ($LASTEXITCODE -ne 0 -or ($linuxNonce -join '') -ne $nonce) {
            throw 'WSL probe did not read the same Windows file.'
        }
        $linuxFileId = & wsl.exe -d $WslDistro --exec stat -c '%i' -- $linuxProbe
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the WSL file identity.' }
        # Fixed native executables with positional arguments; no shell command
        # interpolation, dependency install, or user-controlled executable.
        ($nonce + '-wsl') | & wsl.exe -d $WslDistro --exec tee -- $linuxProbe | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'WSL could not write the probe.' }
        if ([System.IO.File]::ReadAllText($probePath).TrimEnd("`r", "`n") -ne ($nonce + '-wsl')) {
            throw 'WSL write did not reach the same Windows object.'
        }
        $result.wsl_path = $mapped
        $result.wsl_same_object = $true
        $result.write_probe = $true
        $result['probe_native_identity'] = $fileIdentity
        $result['probe_wsl_identity'] = @{ file_id = ($linuxFileId -join '').Trim(); same_object = $true }
    } finally {
        if (Test-Path -LiteralPath $probePath) { Remove-Item -LiteralPath $probePath -Force }
    }
}
$result | ConvertTo-Json -Depth 5 -Compress
