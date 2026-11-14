"""Opt-in real Docker isolation checks. Failed boundaries are never mocked as passing.

Requires the separate M4-A controller from scripts/m4_sandbox_probe.py. Every
fixture creates and destroys only its own sandbox. The production stack is not used.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNI_M4_SANDBOX_LIVE") != "1",
    reason="Set OMNI_M4_SANDBOX_LIVE=1 only with the isolated M4-A controller running",
)


@pytest.fixture(scope="module")
def probe_modules():
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    sys.path.insert(0, scripts)
    try:
        import m4_sandbox_adapter_probe
        import m4_sandbox_boundary_checks
        import m4_sandbox_checks
        import m4_sandbox_fault_checks

        yield m4_sandbox_checks, m4_sandbox_boundary_checks, m4_sandbox_fault_checks, m4_sandbox_adapter_probe
    finally:
        sys.path.remove(scripts)


@pytest.fixture(scope="module")
def baseline_report(probe_modules):
    return probe_modules[0].probe()


@pytest.fixture(scope="module")
def auth_report(probe_modules):
    return probe_modules[1].check()


@pytest.fixture(scope="module")
def fault_report(probe_modules):
    return probe_modules[2].check()


@pytest.fixture(scope="module")
def adapter_report(probe_modules):
    return asyncio.run(probe_modules[3].check())


@pytest.mark.parametrize(
    "name",
    [
        "controller_auth",
        "resources",
        "mounts",
        "non_root",
        "caps",
        "snapshot_io",
        "stdout",
        "network",
        "local_auth",
        "timeout",
        "cancel",
    ],
)
def test_sandbox_baseline(baseline_report, name):
    assert "error" not in baseline_report, baseline_report.get("error")
    assert baseline_report["checks"][name]["pass"], baseline_report["checks"][name]
    assert baseline_report["cleanup"] == "destroyed"


@pytest.mark.parametrize(
    "name",
    [
        "uid0_rejected",
        "native_token",
        "credential_separation",
        "filesystem_separation",
        "internal_init_rejected",
        "outside_mount_rejected",
    ],
)
def test_sandbox_management_auth(auth_report, name):
    assert "error" not in auth_report, auth_report.get("error")
    assert auth_report["checks"][name]["pass"], auth_report["checks"][name]
    assert auth_report["container_removed"]


@pytest.mark.parametrize("name", ["peer_execd_auth", "egress_sidecar_stop", "kill_cleanup"])
def test_sandbox_fault_boundary(fault_report, name):
    assert "error" not in fault_report, fault_report.get("error")
    assert fault_report["checks"][name]["pass"], fault_report["checks"][name]
    assert fault_report["container_removed"]


@pytest.mark.parametrize("name", ["python_node", "cancel_tree"])
def test_application_adapter_real_runtime(adapter_report, name):
    assert "error" not in adapter_report, adapter_report.get("error")
    assert adapter_report["checks"][name]["pass"], adapter_report["checks"][name]
