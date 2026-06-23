"""Run the bounded-safety property suite and export results as JSON for the paper.

Pure verification (no real API calls): the safety properties are checked against
the deterministic arbiter + a monkeypatched judge.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main(out: str = "cgb_results/safety_properties.json") -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_cgb_safety_properties.py",
         "-v", "--tb=no", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True)
    lines = [l for l in proc.stdout.splitlines()
             if "::" in l and ("PASSED" in l or "FAILED" in l)]
    results = []
    for l in lines:
        name = l.split("::", 1)[1].split(" ")[0].strip()
        results.append({"property": name, "passed": "PASSED" in l})
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    n_pass = sum(r["passed"] for r in results)
    print(f"{n_pass}/{len(results)} properties hold -> {out}")
    return 0 if (results and all(r["passed"] for r in results)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
