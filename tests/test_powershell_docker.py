"""PowerShell backend contract; these tests never start Docker or change PATH."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")
COMMON = Path(__file__).resolve().parents[1] / "scripts" / "common.ps1"


def run_powershell(body: str, *, deployment_env: str | None = None, **extra_env: str):
    env = os.environ.copy()
    env.pop("OMNI_DOCKER_BACKEND", None)
    env.pop("OMNI_WSL_DISTRO", None)
    # A PS7 parent can otherwise make PS5 load incompatible core modules.
    system_root = env.get("SystemRoot", r"C:\Windows")
    env["PSModulePath"] = str(Path(system_root) / "System32/WindowsPowerShell/v1.0/Modules")
    env.update(extra_env)
    # Deployment defaults are an input too: never let the developer's running
    # instance choose the backend for a synthetic contract test.
    with TemporaryDirectory(prefix="omni-backend-test-") as directory:
        runtime = Path(directory)
        if deployment_env is not None:
            (runtime / "deployment.env").write_text(deployment_env, encoding="utf-8")
        escaped = runtime.as_posix().replace("'", "''")
        program = f". '{COMMON.as_posix()}'; $script:OmniRuntime='{escaped}'; {body}"
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", program],
            capture_output=True, text=True, env=env, timeout=20, encoding="utf-8", errors="replace",
        )


def test_default_backend_prefers_native_docker_without_probing_wsl():
    result = run_powershell("function docker {}; Get-OmniDockerBackend")
    assert result.returncode == 0 and result.stdout.strip() == "native"


def test_wsl_selection_requires_explicit_existing_name_format():
    result = run_powershell("Get-OmniDockerBackend", OMNI_DOCKER_BACKEND="wsl")
    assert result.returncode != 0 and "OMNI_WSL_DISTRO" in result.stderr


def test_saved_backend_is_used_only_without_an_explicit_process_override():
    profile = "OMNI_DOCKER_BACKEND=wsl\nOMNI_WSL_DISTRO=DedicatedTest\n"
    saved = run_powershell("function wsl.exe {}; Get-OmniDockerBackend", deployment_env=profile)
    explicit = run_powershell("function docker {}; Get-OmniDockerBackend",
                             deployment_env=profile, OMNI_DOCKER_BACKEND="native")
    assert saved.returncode == explicit.returncode == 0
    assert saved.stdout.strip() == "wsl" and explicit.stdout.strip() == "native"


def test_unc_paths_are_rejected_before_any_wsl_path_conversion():
    result = run_powershell(
        "function wsl.exe {}; ConvertTo-OmniComposePath '\\\\server\\share\\project'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest",
    )
    assert result.returncode != 0 and "UNC paths are not supported" in result.stderr


def test_wsl_env_is_restored_and_secret_not_placed_in_command_arguments():
    secret = "test-only-secret-for-env-contract"
    result = run_powershell(
        "function wsl.exe { $global:LASTEXITCODE=0; "
        "if (($args -join ' ') -match $env:OMNI_POSTGRES_PASSWORD) { throw 'secret in arguments' }; "
        "if ($env:WSLENV -notmatch 'OMNI_POSTGRES_PASSWORD/u') { throw 'missing WSLENV entry' } }; "
        "$before=$env:WSLENV; Invoke-OmniDocker -Arguments @('info') | Out-Null; "
        "if ($env:WSLENV -ne $before) { throw 'WSLENV changed' }; 'ok'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest",
        OMNI_POSTGRES_PASSWORD=secret, WSLENV="EXAMPLE/u",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"
    assert secret not in result.stdout + result.stderr


def test_wsl_failure_restores_environment_and_preserves_exit_code():
    result = run_powershell(
        "function wsl.exe { $global:LASTEXITCODE=19 }; "
        "$before=$env:WSLENV; Invoke-OmniDocker -Arguments @('info') | Out-Null; "
        "if ($env:WSLENV -ne $before -or $LASTEXITCODE -ne 19) { throw 'bad cleanup' }; 'ok'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest", WSLENV="EXAMPLE/u",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_wsl_transfers_explicit_agent_options_and_restores_wslenv():
    result = run_powershell(
        "function wsl.exe { $global:LASTEXITCODE=0; "
        "if ($env:WSLENV -notmatch 'OMNI_ENABLE_AGENT/u' -or "
        "$env:WSLENV -notmatch 'OMNI_CHECKPOINT_SCHEMA/u') { throw 'missing agent option' }; "
        "if ($env:OMNI_ENABLE_AGENT -ne 'true' -or $env:OMNI_CHECKPOINT_SCHEMA -ne 'test_checkpoints') "
        "{ throw 'unexpected agent option' } }; "
        "$before=$env:WSLENV; Invoke-OmniDocker -Arguments @('info') | Out-Null; "
        "if ($env:WSLENV -ne $before) { throw 'WSLENV changed' }; 'ok'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest", WSLENV="EXAMPLE/u",
        OMNI_ENABLE_AGENT="true", OMNI_CHECKPOINT_SCHEMA="test_checkpoints",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_wsl_does_not_inject_agent_options_when_unset():
    result = run_powershell(
        "$env:OMNI_ENABLE_AGENT=$null; $env:OMNI_CHECKPOINT_SCHEMA=$null; "
        "function wsl.exe { $global:LASTEXITCODE=0; "
        "if ($env:WSLENV -match 'OMNI_ENABLE_AGENT|OMNI_CHECKPOINT_SCHEMA') "
        "{ throw 'unset agent option passed' } }; "
        "$before=$env:WSLENV; Invoke-OmniDocker -Arguments @('info') | Out-Null; "
        "if ($env:WSLENV -ne $before) { throw 'WSLENV changed' }; 'ok'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest", WSLENV="EXAMPLE/u",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"


@pytest.mark.parametrize("source", ["default", "process", "envfile", "process_over_envfile"])
def test_real_wsl_compose_resolves_agent_options_equally_for_all_services(source, tmp_path):
    distro = os.environ.get("OMNI_TEST_DOCKER_WSL_DISTRO")
    if not distro:
        pytest.skip("Set OMNI_TEST_DOCKER_WSL_DISTRO to an existing dedicated WSL Docker distribution")
    # A synthetic env file avoids reading the user's optional project .env. No
    # container is started and the resolved config (which includes the fake
    # database password) is held in memory, never echoed into test output.
    env_file = tmp_path / "synthetic.env"
    env_content = "OMNI_POSTGRES_PASSWORD=synthetic-test-password\n"
    if source == "envfile":
        env_content += "OMNI_ENABLE_AGENT=true\nOMNI_CHECKPOINT_SCHEMA=test_checkpoints\n"
    elif source == "process_over_envfile":
        env_content += "OMNI_ENABLE_AGENT=false\nOMNI_CHECKPOINT_SCHEMA=ignored_schema\n"
    env_file.write_text(env_content, encoding="utf-8")
    program = (
        "$env:OMNI_ENABLE_AGENT=$null; $env:OMNI_CHECKPOINT_SCHEMA=$null; "
        + ("$env:OMNI_ENABLE_AGENT='true'; $env:OMNI_CHECKPOINT_SCHEMA='test_checkpoints'; "
           if source in {"process", "process_over_envfile"} else "")
        + "$env:OMNI_POSTGRES_PASSWORD=$null; $env:OMNI_IMAGE=$null; "
        "$mappedRoot=ConvertTo-OmniComposePath $script:OmniRoot; "
        "$mappedFile=ConvertTo-OmniComposePath (Join-Path $script:OmniRoot 'compose.yaml'); "
        "$mappedEnv=ConvertTo-OmniComposePath $env:OMNI_TEST_ENV_FILE; "
        "$config=Invoke-OmniDocker -Arguments @('compose','--project-directory',$mappedRoot,"
        "'--env-file',$mappedEnv,'-f',$mappedFile,'config','--format','json'); "
        "if ($LASTEXITCODE -ne 0) { throw 'Compose config failed' }; "
        "$obj=($config -join \"`n\") | ConvertFrom-Json; "
        "foreach ($name in @('init','api','worker')) { $e=$obj.services.$name.environment; "
        "Write-Output ($name + '=' + $e.OMNI_ENABLE_AGENT + ',' + $e.OMNI_CHECKPOINT_SCHEMA) }"
    )
    result = run_powershell(
        program, OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO=distro, OMNI_TEST_ENV_FILE=str(env_file),
    )
    assert result.returncode == 0, "Compose config did not complete successfully"
    expected = "false,omniharness_checkpoints" if source == "default" else "true,test_checkpoints"
    assert result.stdout.splitlines() == [f"{name}={expected}" for name in ("init", "api", "worker")]


def test_wsl_executes_docker_without_a_command_shell():
    result = run_powershell(
        "function wsl.exe { $global:LASTEXITCODE=0; "
        "if ($args[4] -ne '--exec' -or $args[5] -ne 'docker') { throw 'shell invocation' } }; "
        "Invoke-OmniDocker -Arguments @('ps','--filter','name=^/(foo|bar)$') | Out-Null; 'ok'",
        OMNI_DOCKER_BACKEND="wsl", OMNI_WSL_DISTRO="DedicatedTest",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_no_build_rejects_a_project_tag_with_a_different_image():
    result = run_powershell(
        "function Invoke-OmniDocker { param([string[]]$Arguments); $global:LASTEXITCODE=0; "
        "return ('sha256:' + ('b' * 64)) }; "
        "$context=@{Image='omniharness:project-test';OwnedImages=@(('sha256:' + ('a' * 64)))}; "
        "Initialize-OmniExistingImage $context",
    )
    assert result.returncode != 0 and "does not match its existing containers" in result.stderr


def test_no_build_derives_missing_tag_from_verified_owned_image_id():
    result = run_powershell(
        "function Invoke-OmniDocker { param([string[]]$Arguments); "
        "if ($Arguments[0] -eq 'image') { $global:LASTEXITCODE=1; return }; "
        "if ($Arguments[0] -ne 'tag' -or $Arguments[1] -ne ('sha256:' + ('a' * 64))) "
        "{ throw 'unsafe tag' }; $global:LASTEXITCODE=0 }; "
        "$context=@{Image='omniharness:project-test';OwnedImages=@(('sha256:' + ('a' * 64)))}; "
        "Initialize-OmniExistingImage $context; 'ok'",
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"
