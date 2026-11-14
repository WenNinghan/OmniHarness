"""Actual preview registration/file transport smoke; never calls any model."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from m4_preview_deploy import PRIVATE, PROJECT, wsl

BASE = "http://127.0.0.1:18792"
FIXTURE = Path("G:/OmniHarness-M4-Fixtures/预览注册 中文 空格")


def main():
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "docs").mkdir(exist_ok=True)
    for path, content in (
        (FIXTURE / "docs/hello.txt", "M4 preview native workspace probe\n中文与空格路径\n"),
        (FIXTURE / ".env", "M4_TEST_PLACEHOLDER=not-a-real-secret\n"),
    ):
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    code = wsl("docker", "exec", PROJECT + "-api-1", "omniharness", "auth-code").decode().splitlines()[-1]
    report = {"api_url": BASE, "fixture": str(FIXTURE), "model_calls": 0}
    with httpx.Client(
        base_url=BASE, headers={"Origin": BASE, "X-Omni-Request": "1"}, trust_env=False, timeout=60
    ) as client:
        response = client.get("/health")
        response.raise_for_status()
        report["health"] = response.json()
        client.post("/api/v1/auth/pair", json={"code": code}).raise_for_status()
        tools = client.get("/api/v1/tools/status")
        tools.raise_for_status()
        report["tools"] = tools.json()
        assert report["tools"]["broker_ready"] is True
        models = client.get("/api/v1/model-configs")
        models.raise_for_status()
        report["model_count"] = len(models.json())
        rows = client.get("/api/v1/workspaces").json()
        row = next((r for r in rows if r["host_path"] == str(FIXTURE) and r["ready"]), None)
        if row is None:
            response = client.post(
                "/api/v1/workspaces",
                json={
                    "name": "M4 预览 · 中文路径",
                    "host_path": str(FIXTURE),
                    "read_allow": ["."],
                    "write_allow": ["docs"],
                    "command_approval": True,
                },
            )
            if response.status_code != 201:
                report["registration_error"] = response.json()
                (PRIVATE / "smoke-report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            response.raise_for_status()
            row = response.json()
        assert row["ready"] is True
        report["workspace"] = row
        response = client.get(f"/api/v1/workspaces/{row['id']}/files")
        response.raise_for_status()
        report["file_listing"] = response.json()
        assert all(entry["path"] != ".env" for entry in report["file_listing"]["entries"])
    (PRIVATE / "smoke-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
