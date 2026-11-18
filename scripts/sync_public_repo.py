"""Audited allowlist synchronization and privacy preflight for public Git checkouts.

Synchronizes an explicit file allowlist from a working directory into an isolated
clean Git repository while preserving target line endings, verifying Python AST
equivalence, checking forbidden tree paths, and scanning added diff lines for
credential or machine-local patterns.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

FORBIDDEN_TREE_PREFIXES = (
    ".ai-governance/",
    ".omniharness/",
    ".codex-attachments/",
    ".workbuddy/",
    "docs/历史档案/",
    "docs/工作区/",
)

PRIVACY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows_user_path", re.compile(r"[A-Za-z]:\\\\?Users\\\\?", re.IGNORECASE)),
    ("local_drive_root", re.compile(r"\b[DFG]:\\\\", re.IGNORECASE)),
    ("private_runtime_dir", re.compile(r"\.omniharness/(?:secrets|data|pg17|m4-production|production-native|benchmark)")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("private_key_pem", re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")),
    (
        "non_fixture_postgres_dsn",
        re.compile(r"postgresql(?:\+psycopg)?://(?!postgres(?::omniharness-ci-only)?@127\.0\.0\.1:5432/omniharness_test)[^\s\"']+"),
    ),
)


def verify_python_ast_equivalence(source_text: str, target_text: str, label: str = "<file>") -> None:
    """Raise ``ValueError`` if two Python files do not parse to identical ASTs."""
    left = ast.dump(ast.parse(source_text, filename=label), include_attributes=False)
    right = ast.dump(ast.parse(target_text, filename=label), include_attributes=False)
    if left != right:
        raise ValueError(f"Python AST mismatch for {label}")


def scan_added_diff_lines(diff_text: str) -> list[dict[str, object]]:
    """Scan unified diff added lines (excluding ``+++`` file headers) for privacy leaks."""
    findings: list[dict[str, object]] = []
    current_file = "<unknown>"
    added_index = 0
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        added_index += 1
        content = raw_line[1:]
        for name, pattern in PRIVACY_PATTERNS:
            if pattern.search(content):
                findings.append({
                    "file": current_file,
                    "added_line_index": added_index,
                    "rule": name,
                })
    return findings


def check_forbidden_tree_paths(tracked_paths: list[str]) -> list[str]:
    """Return any tracked paths that fall under private governance/runtime prefixes."""
    violations: list[str] = []
    for item in tracked_paths:
        normalized = item.replace("\\", "/").lstrip("/")
        if normalized == ".env" or any(normalized.startswith(prefix) for prefix in FORBIDDEN_TREE_PREFIXES):
            violations.append(normalized)
    return violations


def sync_allowlist(
    *,
    source_root: Path,
    target_repo: Path,
    allowlist: list[str],
) -> list[dict[str, object]]:
    """Copy allowlisted relative paths preserving existing target EOL style and checking AST."""
    manifest: list[dict[str, object]] = []
    for rel in allowlist:
        normalized = rel.replace("\\", "/").lstrip("/")
        if check_forbidden_tree_paths([normalized]):
            raise ValueError(f"Refusing to sync forbidden path: {normalized}")
        src_path = (source_root / normalized).resolve()
        dst_path = (target_repo / normalized).resolve()
        if source_root.resolve() not in src_path.parents:
            raise ValueError(f"Source path escapes root: {normalized}")
        if target_repo.resolve() not in dst_path.parents:
            raise ValueError(f"Target path escapes repository: {normalized}")
        if not src_path.is_file():
            raise FileNotFoundError(f"Allowlisted source file missing: {normalized}")

        use_crlf = dst_path.exists() and (b"\r\n" in dst_path.read_bytes())
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        lf_bytes = src_path.read_bytes().replace(b"\r\n", b"\n")
        out_bytes = lf_bytes.replace(b"\n", b"\r\n") if use_crlf else lf_bytes
        dst_path.write_bytes(out_bytes)

        if normalized.endswith(".py"):
            verify_python_ast_equivalence(
                src_path.read_text(encoding="utf-8"),
                dst_path.read_text(encoding="utf-8"),
                label=normalized,
            )

        manifest.append({
            "path": normalized,
            "sha256": hashlib.sha256(out_bytes).hexdigest(),
            "bytes": len(out_bytes),
        })
    return manifest


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("."), help="Working source root")
    parser.add_argument("--target-repo", type=Path, required=True, help="Clean Git publication checkout")
    parser.add_argument("--allowlist-file", type=Path, required=True, help="JSON array of relative file paths")
    parser.add_argument("--report-out", type=Path, required=True, help="Output JSON preflight report path")
    parser.add_argument("--base-ref", type=str, default=None, help="Optional base commit/ref to checkout with --branch")
    parser.add_argument("--branch", type=str, default=None, help="Optional target branch to create/reset before sync")
    args = parser.parse_args()

    source_root = args.source.resolve()
    target_repo = args.target_repo.resolve()
    allowlist = json.loads(args.allowlist_file.read_text(encoding="utf-8"))
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(x, str) and x for x in allowlist):
        raise ValueError("Allowlist JSON must be a non-empty array of relative path strings")

    if args.branch and args.base_ref:
        run_git(target_repo, "reset", "--hard", args.base_ref)
        run_git(target_repo, "clean", "-fd")
        run_git(target_repo, "checkout", "-B", args.branch, args.base_ref)

    manifest = sync_allowlist(source_root=source_root, target_repo=target_repo, allowlist=allowlist)
    run_git(target_repo, "add", "--", *allowlist)

    tracked = [line for line in run_git(target_repo, "ls-files").splitlines() if line.strip()]
    tree_violations = check_forbidden_tree_paths(tracked)
    if tree_violations:
        raise RuntimeError(f"Forbidden paths tracked in target repo: {tree_violations}")

    diff_text = run_git(target_repo, "diff", "--cached", "--unified=0")
    added_lines = sum(
        1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    privacy_findings = scan_added_diff_lines(diff_text)
    if privacy_findings:
        raise RuntimeError(f"Privacy preflight blocked publication: {privacy_findings}")

    report = {
        "branch": run_git(target_repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "base_head": run_git(target_repo, "rev-parse", "HEAD").strip(),
        "allowlisted_files": manifest,
        "added_lines_scanned": added_lines,
        "tree_violations": tree_violations,
        "privacy_findings": privacy_findings,
        "ast_verified": True,
    }
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"PUBLIC_SYNC_PREFLIGHT_OK files={len(manifest)} added_lines={added_lines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
