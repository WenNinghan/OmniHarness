"""Run independent M4-A fixtures: prepare, up, probe, down. No production writes."""

import argparse
import json

from m4_sandbox_checks import down, probe
from m4_sandbox_setup import prepare, up


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "up", "probe", "auth", "fault", "down"))
    args = parser.parse_args()
    if args.action == "probe":
        result = probe()
    elif args.action == "auth":
        from m4_sandbox_boundary_checks import check

        result = check()
    elif args.action == "fault":
        from m4_sandbox_fault_checks import check

        result = check()
    else:
        result = {"prepare": prepare, "up": up, "down": down}[args.action]()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get("status") == "BLOCKED")


if __name__ == "__main__":
    raise SystemExit(main())
