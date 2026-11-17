#!/usr/bin/env python3
"""Score one submission on a named split and print the metrics (code only, no LLM judge).

    python grading/judge.py <submission.zip> [heldout|dev]
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    split = sys.argv[2] if len(sys.argv) > 2 else "heldout"
    os.environ["DC_ROOF_SPLIT"] = split
    from grading.grade import grade
    m = grade(sys.argv[1], split)
    print(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=1, default=float))
    if m.get("_fail_detail"):
        print("\nfailures:", json.dumps(m["_fail_detail"], indent=1, default=str))
    if m.get("_scen_detail"):
        print("\nper scenario:", json.dumps(m["_scen_detail"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
