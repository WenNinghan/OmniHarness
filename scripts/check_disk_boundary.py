#!/usr/bin/env python3
"""Verify OmniHarness caches, temp files and build outputs stay on the G: drive.

Read-only with respect to C: drive paths: legacy caches are measured, never removed.
Usage:
  python scripts/check_disk_boundary.py            # report only
  python scripts/check_disk_boundary.py --strict  # exit 1 on any violation
  python scripts/check_disk_boundary.py --init    # create the G-drive cache dirs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".omniharness"
CACHE = RUNTIME / "cache"
REPORT_PATH = RUNTIME / "disk-boundary-report.json"
MAX_FILES = 20000

GROUPS = (
    ("uv cache", ("UV_CACHE_DIR",), CACHE / "uv"),
    ("uv python", ("UV_PYTHON_INSTALL_DIR",), CACHE / "python"),
    ("pip cache", ("PIP_CACHE_DIR",), CACHE / "pip"),
    ("npm cache", ("npm_config_cache", "NPM_CONFIG_CACHE"), CACHE / "npm"),
    ("temp", ("TEMP", "TMP", "TMPDIR"), CACHE / "tmp"),
    ("playwright", ("PLAYWRIGHT_BROWSERS_PATH",), CACHE / "playwright"),
    ("hf cache", ("HF_HOME",), CACHE / "hf"),
)

PROJECT_PATHS = (
    ".venv",
    "web/node_modules",
    ".omniharness",
    "dist",
    "web/dist",
)


def norm(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def env_get(name: str) -> tuple[str | None, str | None]:
    target = name.upper()
    for key, value in os.environ.items():
        if key.upper() == target:
            return key, value
    return None, None


def measure(path: Path) -> dict | None:
    if not path.is_dir():
        return None
    total = 0
    count = 0
    truncated = False
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
                    count += 1
            except OSError:
                continue
            if count >= MAX_FILES:
                truncated = True
                break
    except OSError:
        return {"files": 0, "bytes": 0, "truncated": True, "error": "unreadable"}
    return {"files": count, "bytes": total, "truncated": truncated}


def legacy_paths() -> list[tuple[str, Path]]:
    local = os.environ.get("LOCALAPPDATA")
    profile = os.environ.get("USERPROFILE")
    result: list[tuple[str, Path]] = []
    if local:
        base = Path(local)
        result += [
            ("uv/cache", base / "uv" / "cache"),
            ("uv/python", base / "uv" / "python"),
            ("pip/cache", base / "pip" / "cache"),
            ("npm-cache", base / "npm-cache"),
            ("ms-playwright", base / "ms-playwright"),
        ]
    if profile:
        result.append(("~/.npm", Path(profile) / ".npm"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit 1 on any violation")
    parser.add_argument("--init", action="store_true", help="create G-drive cache directories")
    parser.add_argument("--json", action="store_true", help="print the JSON report to stdout")
    args = parser.parse_args()

    if args.init:
        for _, _, expected in GROUPS:
            expected.mkdir(parents=True, exist_ok=True)

    groups = []
    for label, names, expected in GROUPS:
        configured = []
        issues = []
        matched = False
        for name in names:
            actual_key, value = env_get(name)
            if value is None:
                continue
            configured.append({"name": actual_key, "value": value})
            if norm(value) == norm(expected):
                matched = True
            else:
                issues.append(f"{actual_key} points outside the G-drive cache: {value}")
        if not configured:
            issues.append("no cache variable is set; source scripts/g-drive-env.ps1 first")
        groups.append({
            "name": label,
            "expected": str(expected),
            "configured": configured,
            "ok": matched and not issues,
            "issues": issues,
        })

    drive = ROOT.drive.upper()
    windows = sys.platform.startswith("win")
    drive_ok = (not windows) or drive == "G:"
    drive_note = f"root drive {drive or '(none)'}" + ("" if drive_ok else "; expected G:")

    paths = []
    for relative in PROJECT_PATHS:
        path = ROOT / relative
        on_g = (not windows) or path.drive.upper() == "G:"
        paths.append({"path": str(path), "exists": path.exists(), "on_g_drive": on_g})

    legacy = []
    for label, path in legacy_paths():
        info = measure(path)
        if info:
            legacy.append({"label": label, "path": str(path), **info})

    ok = drive_ok and all(g["ok"] for g in groups) and all(p["on_g_drive"] for p in paths)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "root": str(ROOT),
        "root_drive": drive,
        "drive_ok": drive_ok,
        "drive_note": drive_note,
        "groups": groups,
        "project_paths": paths,
        "legacy_c_drive": legacy,
        "legacy_note": "read-only measurement of historical caches; nothing was removed",
        "ok": ok,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"OmniHarness disk boundary - {report['checked_at']}")
        print(f"root: {ROOT} [{drive_note}] [{'OK' if drive_ok else 'FAIL'}]")
        for group in groups:
            marker = "OK" if group["ok"] else "FAIL"
            print(f"  {group['name']}: [{marker}] expected {group['expected']}")
            for issue in group["issues"]:
                print(f"    - {issue}")
        for path in paths:
            print(f"  {path['path']}: exists={path['exists']} on_g={path['on_g_drive']}")
        if legacy:
            print("C-drive legacy caches (measured only, not cleaned):")
            for item in legacy:
                mib = item["bytes"] / (1024 * 1024)
                print(f"  {item['label']}: {item['files']} files, {mib:.1f} MiB")
        else:
            print("C-drive legacy caches: none detected")
        print(f"report: {REPORT_PATH}")
        print(f"result: {'OK' if ok else 'FAIL'}")
    if args.strict and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
