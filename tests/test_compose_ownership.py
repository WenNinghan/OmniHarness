"""Exercise PowerShell ownership decisions without touching Docker resources."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")
COMMON = Path(__file__).resolve().parents[1] / "scripts" / "common.ps1"

MOCK = r"""
function Get-OmniDockerBackend { 'native' }
function Get-OmniComposeArguments { @() }
function Invoke-OmniDocker {
  param([string[]]$Arguments)
  $global:LASTEXITCODE=0
  if ($Arguments[0] -eq 'compose') { return '{"name":"ownership-test"}' }
  if ($Arguments[0] -eq 'ps') { if ($env:CASE -notin @('orphan','unlabeled-volume','empty')) { return 'abc123' }; return }
  if ($Arguments[0] -eq 'volume') {
    if ($env:CASE -eq 'orphan') { return 'ownership-test_postgres-data' }
    if ($env:CASE -eq 'unlabeled-volume' -and $Arguments[-1] -like 'name=*') { return 'ownership-test_postgres-data' }
    return
  }
  if ($Arguments[0] -eq 'inspect' -and $Arguments[2] -eq '{{.Image}}') { return ('sha256:' + ('a' * 64)) }
  if ($Arguments[0] -eq 'inspect') {
    $directory=$script:OmniRoot; $file=Join-Path $script:OmniRoot 'compose.yaml'
    if ($env:CASE -eq 'different') { $directory=$env:SystemRoot }
    if ($env:CASE -eq 'wrong-config') { $file=Join-Path $script:OmniRoot 'README.md' }
    if ($env:CASE -eq 'missing') { $directory='' }
    return (@{'com.docker.compose.project.working_dir'=$directory;
      'com.docker.compose.project.config_files'=$file;
      'com.docker.compose.project'='ownership-test';
      'com.docker.compose.service'='api'} | ConvertTo-Json -Compress)
  }
  throw 'Unexpected mocked Docker operation.'
}
"""


def run_case(case: str):
    env = os.environ.copy()
    env["CASE"] = case
    env["PSModulePath"] = str(Path(env.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/Modules")
    program = f". '{COMMON.as_posix()}'; {MOCK}; $result=Get-OmniComposeContext; $result.Project"
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", program],
        capture_output=True, text=True, env=env, timeout=25, encoding="utf-8", errors="replace",
    )


def test_same_physical_directory_and_config_are_accepted():
    result = run_case("same")
    assert result.returncode == 0 and result.stdout.strip() == "ownership-test"


@pytest.mark.parametrize("case", ["different", "wrong-config", "missing", "orphan", "unlabeled-volume"])
def test_unverified_ownership_is_rejected(case):
    result = run_case(case)
    assert result.returncode != 0
