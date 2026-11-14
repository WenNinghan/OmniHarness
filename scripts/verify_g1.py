#!/usr/bin/env python3
"""G1 acceptance: repeatable native PostgreSQL link check, entirely on G: drive.

Starts the bundled PostgreSQL 17 instance if needed, then runs the PostgreSQL
integration tests and the LangGraph checkpoint interruption probe in separate
processes. Writes a redacted JSON report. Docker/Compose is deliberately out
of scope and is always reported as untested.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".omniharness"
BIN = RUNTIME / "pg17" / "pgsql" / "bin"
DATA = RUNTIME / "pg17-test-data"
LOG = RUNTIME / "pg17-test.log"
SECRET = RUNTIME / "secrets" / "pg17-test-password"
PROBE = RUNTIME / "pg_checkpoint_probe.py"
REPORT = RUNTIME / "g1-report.json"
HOST, PORT, DATABASE, USER = "127.0.0.1", "55432", "omniharness_test", "omni_test"


def binary(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    return str(BIN / f"{name}{suffix}")


def execute(label, args, env, steps, timeout=240, capture=True):
    # pg_ctl spawns a background postgres process that inherits our pipes;
    # capturing those would wait forever for EOF. Use DEVNULL for that step.
    output = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        done = subprocess.run([str(a) for a in args], cwd=str(ROOT), env=env,
                              stdout=output, stderr=output, text=True, timeout=timeout)
        code = done.returncode
        out = done.stdout if capture and done.stdout else ""
        err = done.stderr if capture and done.stderr else ""
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", f"timed out after {timeout}s"
    steps.append({
        "name": label,
        "command": " ".join(str(a) for a in args),
        "returncode": code,
        "stdout_tail": out[-1200:],
        "stderr_tail": err[-800:],
    })
    print(f"[{label}] exit={code}")
    for line in out.strip().splitlines()[-8:]:
        print("  " + line)
    if code != 0 and err.strip():
        for line in err.strip().splitlines()[-5:]:
            print("  ! " + line)
    return code


def main() -> int:
    if not SECRET.exists():
        print(f"missing PostgreSQL test password file: {SECRET}")
        return 1
    password = SECRET.read_text(encoding="ascii").strip()
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["OMNI_TEST_POSTGRES_URL"] = (
        f"postgresql+psycopg://{USER}:{password}@{HOST}:{PORT}/{DATABASE}"
    )
    steps: list[dict] = []
    ok = True

    ready = execute("pg_isready", [binary("pg_isready"), "-h", HOST, "-p", PORT],
                    env, steps, timeout=20)
    if ready != 0:
        ok = execute("pg_ctl start", [binary("pg_ctl"), "-D", str(DATA), "-l", str(LOG),
                                      "-o", f"-h {HOST} -p {PORT}", "-w", "start"],
                     env, steps, timeout=90, capture=False) == 0 and ok
        ready = execute("pg_isready after start", [binary("pg_isready"), "-h", HOST, "-p", PORT],
                        env, steps, timeout=20)
    ok = (ready == 0) and ok

    if ok:
        probe = subprocess.run(
            [binary("psql"), "-h", HOST, "-p", PORT, "-U", USER, "-d", "postgres", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{DATABASE}'"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30,
        )
        exists = probe.returncode == 0 and probe.stdout.strip() == "1"
        steps.append({"name": "database check", "command": "psql SELECT pg_database",
                      "returncode": probe.returncode, "stdout_tail": f"exists={exists}",
                      "stderr_tail": ""})
        print(f"[database check] {DATABASE} exists={exists}")
        if not exists:
            ok = execute("createdb", [binary("createdb"), "-h", HOST, "-p", PORT,
                                      "-U", USER, DATABASE], env, steps, timeout=60) == 0 and ok
    else:
        steps.append({"name": "verification skipped", "command": "", "returncode": None,
                      "stdout_tail": "PostgreSQL unavailable", "stderr_tail": ""})

    if ok:
        # An isolated basetemp avoids pytest's bulk cleanup of stale temp roots,
        # which the sandbox blocks once a garbage directory exceeds 50 files.
        isolated_temp = RUNTIME / "cache" / "tmp" / f"pytest-g1-{int(time.time())}"
        ok = execute("pytest postgres integration",
                     [sys.executable, "-m", "pytest", "-q", "tests/test_postgres.py",
                      f"--basetemp={isolated_temp}"],
                     env, steps, timeout=300) == 0 and ok
        ok = execute("checkpoint probe start", [sys.executable, str(PROBE), "start"],
                     env, steps, timeout=120) == 0 and ok
        ok = execute("checkpoint probe resume", [sys.executable, str(PROBE), "resume"],
                     env, steps, timeout=120) == 0 and ok

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "G1 native PostgreSQL link, G-drive only",
        "postgres": {"host": HOST, "port": PORT, "database": DATABASE, "data_dir": str(DATA)},
        "steps": steps,
        "ok": bool(ok),
        "container_chain": (
            "NOT TESTED: Docker Engine, Compose services, image build and Windows bind "
            "mounts remain unverified; this check only covers native PostgreSQL."
        ),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"G1 result: {'OK' if ok else 'FAIL'}")
    print(report["container_chain"])
    print(f"report: {REPORT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
