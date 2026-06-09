"""AST lint: forbid raw 6-digit hex literals and inline ``.style(f"…")`` calls in
``systemu/interface/``. Re-theming must be done by editing design tokens only.

Usage:
    python -m tools.lint_ui_styles            # check, honoring the baseline
    python -m tools.lint_ui_styles --update-baseline
Exit code 1 if NEW (non-baselined) violations exist.
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

_HEX6 = re.compile(r"#[0-9a-fA-F]{6}\b")
_INTERFACE = Path("systemu/interface")
_BASELINE = Path("tools/ui_style_baseline.txt")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def key(self) -> str:
        return f"{self.path}:{self.message}"


def find_violations(source: str, path: str) -> List[Violation]:
    out: List[Violation] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _HEX6.search(node.value):
                out.append(Violation(path, node.lineno, "raw hex literal — use a design token"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "style" and node.args \
                and isinstance(node.args[0], ast.JoinedStr):
            out.append(Violation(path, node.lineno, "inline .style(f\"…\") — compose a primitive"))
    return out


def _scan_repo() -> List[Violation]:
    found: List[Violation] = []
    for py in _INTERFACE.rglob("*.py"):
        if "design" in py.parts:
            continue
        found.extend(find_violations(py.read_text(encoding="utf-8"), str(py).replace("\\", "/")))
    return found


def _counts(violations: List[Violation]) -> Dict[str, int]:
    """Aggregate occurrences into ``key -> count`` (stable against line churn)."""
    return dict(Counter(v.key() for v in violations))


def new_violations(current: Dict[str, int], baseline: Dict[str, int]) -> Dict[str, int]:
    """Return the keys whose current count exceeds the baselined count.

    A brand-new key (absent from the baseline) with count>0 is included;
    decreases (migration progress) and unchanged counts are not.
    """
    return {k: c for k, c in current.items() if c > baseline.get(k, 0)}


def _load_baseline() -> Dict[str, int]:
    if not _BASELINE.exists():
        return {}
    out: Dict[str, int] = {}
    for ln in _BASELINE.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        count, _, key = ln.partition("\t")
        out[key] = int(count)
    return out


def main(argv: List[str]) -> int:
    current = _counts(_scan_repo())
    if "--update-baseline" in argv:
        lines = sorted(f"{count}\t{key}" for key, count in current.items())
        _BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"baseline updated: {len(current)} keys, {sum(current.values())} total violations")
        return 0
    baseline = _load_baseline()
    new = new_violations(current, baseline)
    for key in sorted(new):
        print(f"{key}: {new[key]} (baselined {baseline.get(key, 0)})")
    if new:
        print(f"\n{sum(new.values())} NEW UI-style violation(s) across {len(new)} key(s) (over baseline).")
        return 1
    print(f"UI-style lint clean ({sum(current.values())} violations across {len(current)} keys baselined, 0 new).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
