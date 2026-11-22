$ErrorActionPreference = 'Stop'
$script:OmniRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:OmniRuntime = Join-Path $script:OmniRoot '.omniharness'
$script:OmniSecretDir = Join-Path $script:OmniRuntime 'secrets'
$script:OmniComposeEnv = Join-Path $script:OmniSecretDir 'compose.env'

function Get-OmniDockerBackend {
    $deploymentEnv = Join-Path $script:OmniRuntime 'deployment.env'
    if (Test-Path -LiteralPath $deploymentEnv) {
        foreach ($setting in @('OMNI_DOCKER_BACKEND', 'OMNI_WSL_DISTRO')) {
            if (-not [Environment]::GetEnvironmentVariable($setting, 'Process')) {
                $line = @(Get-Content -LiteralPath $deploymentEnv | Where-Object { $_.StartsWith("$setting=") })
                if ($line.Count -eq 1) { [Environment]::SetEnvironmentVariable($setting, $line[0].Substring($setting.Length + 1), 'Process') }
            }
        }
    }
    $backend = if ($env:OMNI_DOCKER_BACKEND) { $env:OMNI_DOCKER_BACKEND.Trim().ToLowerInvariant() } else { 'native' }
    if ($backend -notin @('native', 'wsl')) { throw 'OMNI_DOCKER_BACKEND must be native or wsl.' }
    if ($backend -eq 'wsl') {
        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw 'WSL is not installed or wsl.exe is not available.' }
        if (-not $env:OMNI_WSL_DISTRO -or $env:OMNI_WSL_DISTRO -notmatch '^[A-Za-z0-9_.-]+$') {
            throw 'Set OMNI_WSL_DISTRO to the name of the existing dedicated WSL distribution.'
        }
    } elseif (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker CLI was not found. Install Docker Desktop, or explicitly configure OMNI_DOCKER_BACKEND=wsl and OMNI_WSL_DISTRO.'
    }
    return $backend
}

function Invoke-OmniDocker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $backend = Get-OmniDockerBackend
    if ($backend -eq 'native') {
        & docker @Arguments
        return
    }
    # WSL does not inherit arbitrary Windows environment variables. Pass only
    # the Compose settings that this project understands; never echo values.
    $previous = [Environment]::GetEnvironmentVariable('WSLENV', 'Process')
    $names = @('COMPOSE_PROJECT_NAME', 'OMNI_PUBLIC_PORT', 'OMNI_ALLOWED_ORIGINS',
        'OMNI_ENABLE_DEMO', 'OMNI_ENABLE_AGENT', 'OMNI_CHECKPOINT_SCHEMA',
        'OMNI_POSTGRES_PASSWORD', 'OMNI_UID', 'OMNI_GID', 'OMNI_IMAGE', 'OMNI_RELEASE_IMAGE',
        'OMNI_ENABLE_TOOLS', 'OMNI_INSTANCE_ID', 'OMNI_WORKSPACE_BROKER_URL', 'OMNI_SANDBOX_IMAGE', 'COMPOSE_PROFILES',
        'OMNI_DATABASE_NAME', 'OMNI_DEPLOY_DATA_DIR', 'OMNI_DEPLOY_SECRET_DIR', 'OMNI_REVIEW_FAILURE_POLICY', 'OMNI_REVIEW_REVISION_POLICY')
    $entries = @()
    if ($previous) { $entries += @($previous -split ':' | Where-Object { $_ }) }
    foreach ($name in $names) {
        if ([Environment]::GetEnvironmentVariable($name, 'Process') -and
            -not @($entries | Where-Object { ($_ -split '/')[0] -eq $name }).Count) {
            $entries += "$name/u"
        }
    }
    try {
        [Environment]::SetEnvironmentVariable('WSLENV', ($entries -join ':'), 'Process')
        # Windows PowerShell 5 can treat benign native stderr as a terminating
        # error even when it is redirected. Preserve the caller's preference.
        $ErrorActionPreference = 'Continue'
        & wsl.exe -d $env:OMNI_WSL_DISTRO -u root --exec docker @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('WSLENV', $previous, 'Process')
    }
    $global:LASTEXITCODE = $code
}

function ConvertTo-OmniComposePath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if ((Get-OmniDockerBackend) -eq 'native') { return $LiteralPath }
    if ($LiteralPath -notmatch '^[A-Za-z]:\\') {
        throw 'WSL Docker requires a drive-backed absolute project path; UNC paths are not supported.'
    }
    $ErrorActionPreference = 'Continue'
    $converted = & wsl.exe -d $env:OMNI_WSL_DISTRO -u root --exec wslpath -a $LiteralPath 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $converted) { throw 'Could not map the project path into the selected WSL distribution.' }
    return ($converted -join '').Trim()
}

function Start-OmniWslEngine {
    if ((Get-OmniDockerBackend) -ne 'wsl') { throw '-StartEngine is only available with OMNI_DOCKER_BACKEND=wsl.' }
    Invoke-OmniDocker -Arguments @('--version') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker CLI is unavailable in the selected WSL distribution.' }
    Invoke-OmniDocker -Arguments @('info', '--format', '{{.OSType}}') | Out-Null
    if ($LASTEXITCODE -eq 0) { return }
    [System.IO.Directory]::CreateDirectory($script:OmniRuntime) | Out-Null
    $wslExe = (Get-Command wsl.exe).Source
    $daemonCmd = "`"$wslExe`" -d $env:OMNI_WSL_DISTRO -u root -- sh -c `"if ! docker info >/dev/null 2>&1; then exec dockerd --host=unix:///var/run/docker.sock >/var/log/omniharness-dockerd.log 2>&1; else exec sleep infinity; fi`""
    $spawned = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $daemonCmd }
    if ($spawned.ReturnValue -ne 0) { throw "Could not launch detached WSL Docker daemon (WMI code $($spawned.ReturnValue))." }
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 300
        Invoke-OmniDocker -Arguments @('info', '--format', '{{.OSType}}') | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if (-not $ready) { throw 'WSL Docker engine did not become ready. Inspect /var/log/omniharness-dockerd.log inside WSL.' }
    Write-Host "WSL Docker engine started in $env:OMNI_WSL_DISTRO (process $($spawned.ProcessId))."
}

function Assert-OmniDocker {
    $backend = Get-OmniDockerBackend
    if ($backend -eq 'wsl') {
        Invoke-OmniDocker -Arguments @('--version') | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'The selected WSL distribution is unavailable or Docker CLI is not installed inside it.' }
    }
    Invoke-OmniDocker -Arguments @('compose', 'version', '--short') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2 is required.' }
    $engine = Invoke-OmniDocker -Arguments @('info', '--format', '{{.OSType}}')
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not available. Start the selected engine; for WSL use start.ps1 -StartEngine.' }
    if (($engine -join '').Trim() -ne 'linux') { throw 'OmniHarness requires Docker Linux containers. Switch the Docker Desktop engine to Linux containers.' }
}

function Set-OmniPrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    [System.IO.Directory]::CreateDirectory($LiteralPath) | Out-Null
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # icacls changes the DACL without asking for owner/audit privileges.
    & icacls.exe $LiteralPath '/inheritance:r' '/grant:r' "*${identity}:(OI)(CI)F" '/remove:d' "*$identity" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the local data directory to the current user.' }
    $otherIdentities = @((Get-Acl -LiteralPath $LiteralPath).Access | ForEach-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } | Where-Object { $_ -ne $identity } | Select-Object -Unique)
    foreach ($otherIdentity in $otherIdentities) {
        & icacls.exe $LiteralPath '/remove' "*$otherIdentity" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove an existing access rule from the local data directory.' }
    }
}

function Initialize-OmniDirectories {
    [System.IO.Directory]::CreateDirectory($script:OmniRuntime) | Out-Null
    Set-OmniPrivateDirectory -LiteralPath $script:OmniSecretDir
    Set-OmniPrivateDirectory -LiteralPath (Join-Path $script:OmniRuntime 'data')
    if (-not (Test-Path -LiteralPath $script:OmniComposeEnv)) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $password = [System.BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
        $content = "OMNI_POSTGRES_PASSWORD=$password`nOMNI_UID=1000`nOMNI_GID=1000`n"
        [System.IO.File]::WriteAllText($script:OmniComposeEnv, $content, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Get-OmniComposeArguments {
    if (-not (Test-Path -LiteralPath $script:OmniComposeEnv)) {
        throw 'Local deployment has not been initialized. Run scripts/start.ps1 first.'
    }
    $arguments = @('--project-directory', (ConvertTo-OmniComposePath $script:OmniRoot), '-f', (ConvertTo-OmniComposePath (Join-Path $script:OmniRoot 'compose.yaml')))
    $localEnv = Join-Path $script:OmniRoot '.env'
    if (Test-Path -LiteralPath $localEnv) { $arguments += @('--env-file', (ConvertTo-OmniComposePath $localEnv)) }
    $arguments += @('--env-file', (ConvertTo-OmniComposePath $script:OmniComposeEnv))
    $deploymentEnv = Join-Path $script:OmniRuntime 'deployment.env'
    if (Test-Path -LiteralPath $deploymentEnv) { $arguments += @('--env-file', (ConvertTo-OmniComposePath $deploymentEnv)) }
    return $arguments
}

function Get-OmniPhysicalPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not ('OmniHarness.PathIdentity' -as [type])) {
        $oldTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
        $oldTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
        $compileDir = Join-Path $script:OmniRuntime 'cache\ps-compile'
        [System.IO.Directory]::CreateDirectory($compileDir) | Out-Null
        try {
            [Environment]::SetEnvironmentVariable('TEMP', $compileDir, 'Process')
            [Environment]::SetEnvironmentVariable('TMP', $compileDir, 'Process')
            Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace OmniHarness {
  public static class PathIdentity {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path, uint length, uint flags);
    public static string Resolve(string path) {
      using (var file = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
        if (file.IsInvalid) throw new IOException("Cannot open path for ownership verification.");
        var result = new StringBuilder(32768);
        uint length = GetFinalPathNameByHandle(file, result, (uint)result.Capacity, 0);
        if (length == 0 || length >= result.Capacity) throw new IOException("Cannot resolve path for ownership verification.");
        string value = result.ToString();
        if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + value.Substring(8);
        if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) return value.Substring(4);
        return value;
      }
    }
  }
}
'@
        } finally {
            [Environment]::SetEnvironmentVariable('TEMP', $oldTemp, 'Process')
            [Environment]::SetEnvironmentVariable('TMP', $oldTmp, 'Process')
        }
    }
    try { return [OmniHarness.PathIdentity]::Resolve($LiteralPath).TrimEnd('\') }
    catch { throw 'Cannot resolve a project ownership path; refusing to access Compose resources.' }
}

function ConvertFrom-OmniLabelPath {
    param([Parameter(Mandatory = $true)][string]$LabelPath)
    if ((Get-OmniDockerBackend) -eq 'wsl' -and $LabelPath.StartsWith('/')) {
        $ErrorActionPreference = 'Continue'
        $converted = & wsl.exe -d $env:OMNI_WSL_DISTRO -u root --exec wslpath -w $LabelPath 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $converted) { throw 'Cannot map an existing Compose ownership label to Windows.' }
        return ($converted -join '').Trim()
    }
    return $LabelPath
}

function Get-OmniComposeContext {
    $arguments = Get-OmniComposeArguments
    $configuration = Invoke-OmniDocker -Arguments (@('compose') + $arguments + @('config', '--format', 'json'))
    if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the Compose project name safely.' }
    try { $resolved = ($configuration -join "`n") | ConvertFrom-Json; $project = $resolved.name }
    catch { throw 'Invalid Compose configuration; refusing to access project resources.' }
    if (-not $project -or $project -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw 'Invalid Compose project name.' }
    $physicalRoot = Get-OmniPhysicalPath $script:OmniRoot
    $physicalConfig = Get-OmniPhysicalPath (Join-Path $script:OmniRoot 'compose.yaml')
    $ids = @(Invoke-OmniDocker -Arguments @('ps', '-aq', '--filter', "label=com.docker.compose.project=$project"))
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect existing Compose containers; refusing to continue.' }
    $namedIds = @(Invoke-OmniDocker -Arguments @('ps', '-aq', '--filter', "name=^/${project}-(api|worker|postgres|init)-[0-9]+$"))
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect expected Compose container names; refusing to continue.' }
    $ids = @(($ids + $namedIds) | Where-Object { $_ } | Select-Object -Unique)
    $ownedImages = @()
    foreach ($id in ($ids | Where-Object { $_ })) {
        $labels = Invoke-OmniDocker -Arguments @('inspect', '--format', '{{json .Config.Labels}}', $id)
        if ($LASTEXITCODE -ne 0 -or -not $labels) { throw 'Cannot inspect a Compose container; refusing to continue.' }
        try { $metadata = ($labels -join '') | ConvertFrom-Json }
        catch { throw 'Invalid Compose ownership labels; refusing to continue.' }
        $labelDir = $metadata.'com.docker.compose.project.working_dir'
        $labelConfig = $metadata.'com.docker.compose.project.config_files'
        $labelProject = $metadata.'com.docker.compose.project'
        $labelService = $metadata.'com.docker.compose.service'
        if (-not $labelDir -or -not $labelConfig -or -not $labelProject -or -not $labelService) { throw 'A same-name Compose container lacks ownership labels; refusing to continue.' }
        if ($labelProject -cne $project) { throw 'A same-name container has a conflicting Compose project label; refusing to continue.' }
        $actualDir = Get-OmniPhysicalPath (ConvertFrom-OmniLabelPath $labelDir)
        $configEntries = @($labelConfig -split ',' | Where-Object { $_ })
        if ($configEntries.Count -lt 1) { throw 'Invalid Compose configuration label; refusing to continue.' }
        $actualConfig = Get-OmniPhysicalPath (ConvertFrom-OmniLabelPath $configEntries[0])
        $overlaysOwned = $true
        foreach ($extraEntry in ($configEntries | Select-Object -Skip 1)) {
            $extraPhysical = Get-OmniPhysicalPath (ConvertFrom-OmniLabelPath $extraEntry)
            if (-not $extraPhysical.StartsWith(($physicalRoot + '\'), [StringComparison]::OrdinalIgnoreCase)) {
                $overlaysOwned = $false
                break
            }
        }
        if (-not [String]::Equals($actualDir, $physicalRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not [String]::Equals($actualConfig, $physicalConfig, [StringComparison]::OrdinalIgnoreCase) -or
            -not $overlaysOwned) {
            throw 'The Compose project name is already owned by a different directory or configuration. Choose a different COMPOSE_PROJECT_NAME.'
        }
        if ($labelService -in @('api', 'worker', 'init')) {
            $imageResult = @(Invoke-OmniDocker -Arguments @('inspect', '--format', '{{.Image}}', $id))
            $imageId = if ($imageResult.Count) { $imageResult[0] } else { $null }
            if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Cannot verify the existing application image.' }
            $ownedImages += $imageId
        }
    }
    $volumes = @(Invoke-OmniDocker -Arguments @('volume', 'ls', '-q', '--filter', "label=com.docker.compose.project=$project"))
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Compose volumes; refusing to continue.' }
    $expectedVolume = "${project}_postgres-data"
    $names = @(Invoke-OmniDocker -Arguments @('volume', 'ls', '-q', '--filter', "name=^${expectedVolume}$"))
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the expected database volume; refusing to continue.' }
    foreach ($name in ($names | Where-Object { $_ -eq $expectedVolume })) {
        if ($name -notin $volumes) { throw 'The expected database volume lacks matching Compose ownership labels; refusing to continue.' }
    }
    if (@($ids | Where-Object { $_ }).Count -eq 0 -and @($volumes | Where-Object { $_ }).Count -gt 0) {
        throw 'A same-name Compose volume exists without containers proving ownership. Refusing to access it.'
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($physicalRoot.ToUpperInvariant() + "`n" + $project))
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() } finally { $hash.Dispose() }
    $releaseImage = $resolved.services.api.image
    $pinned = $releaseImage -match '^(?:[^\s@]+@)?sha256:[a-f0-9]{64}$'
    $chosenImage = if ($pinned) { $releaseImage } else { "omniharness:project-$($digest.Substring(0, 24))" }
    return @{ Project=$project; Image=$chosenImage; PinnedRelease=$pinned; OwnedImages=$ownedImages }
}

function Initialize-OmniExistingImage {
    param([Parameter(Mandatory = $true)]$Context)
    $images = @($Context.OwnedImages | Select-Object -Unique)
    $current = @(Invoke-OmniDocker -Arguments @('image', 'inspect', $Context.Image, '--format', '{{.Id}}'))
    if ($LASTEXITCODE -eq 0) {
        if ($images.Count -gt 0 -and ($images.Count -ne 1 -or $current.Count -ne 1 -or $current[0] -ne $images[0])) {
            throw 'The project image tag does not match its existing containers; run start.ps1 without -NoBuild.'
        }
        return
    }
    if ($images.Count -ne 1) { throw 'No safely attributable project image exists. Run start.ps1 without -NoBuild.' }
    Invoke-OmniDocker -Arguments @('tag', $images[0], $Context.Image) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project-specific image tag.' }
}

function Invoke-OmniCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $baseArguments = Get-OmniComposeArguments
    $context = Get-OmniComposeContext
    $lockedProject = $context.Project
    # Serialize cooperating PowerShell invocations with the same project name,
    # then re-check ownership while holding the lock.
    $mutex = New-Object System.Threading.Mutex($false, "Local\OmniHarness-Compose-$($context.Project)")
    $held = $false
    try {
        try { $held = $mutex.WaitOne([TimeSpan]::FromSeconds(60)) }
        catch [System.Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'Another OmniHarness PowerShell command is using this Compose project.' }
        $context = Get-OmniComposeContext
        if ($context.Project -ne $lockedProject) { throw 'Compose project name changed during the ownership check; retry.' }
        if ($Arguments[0] -eq 'up' -and '--no-build' -in $Arguments) { Initialize-OmniExistingImage $context }
        $previousImage = [Environment]::GetEnvironmentVariable('OMNI_IMAGE', 'Process')
        try {
            [Environment]::SetEnvironmentVariable('OMNI_IMAGE', $context.Image, 'Process')
            Invoke-OmniDocker -Arguments (@('compose') + $baseArguments + $Arguments)
            $code = $LASTEXITCODE
        } finally { [Environment]::SetEnvironmentVariable('OMNI_IMAGE', $previousImage, 'Process') }
        if ($code -ne 0) { throw "Docker Compose command failed (exit $code). Run scripts/doctor.ps1 for a redacted diagnostic." }
        $global:LASTEXITCODE = $code
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
