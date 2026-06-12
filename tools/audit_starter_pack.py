"""W12-A1 — starter-pack conformance audit.

Loads every curated seed tool, validates its contract statically, and
EXECUTES the safely-executable ones through the real W6 subprocess runner
with synthetic params in a temp sandbox. Prints a verdict table and exits
non-zero only on contract violations (execution failures are findings,
not crashes — the table is the deliverable).

Usage:  python tools/audit_starter_pack.py
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMPL_DIR = REPO / "systemu" / "vault" / "tools" / "implementations"
RUNNER = REPO / "systemu" / "runtime" / "backend" / "tool_runner_script.py"

NETWORK_IMPORTS = {"requests", "httpx", "urllib", "socket", "playwright", "ddgs"}
DESKTOP_IMPORTS = {"pynput", "pyautogui", "mss", "plyer", "pyperclip",
                   "uiautomation", "PIL"}
LLM_IMPORTS = {"systemu", "openai"}  # impls importing the runtime/LLM stack


def classify(imports: set, name: str) -> str:
    if imports & LLM_IMPORTS:
        return "llm"
    if imports & NETWORK_IMPORTS or name.startswith(("web_", "fetch_", "api_", "download")):
        return "network"
    if imports & DESKTOP_IMPORTS or name in (
            "take_screenshot", "clipboard_read", "clipboard_write",
            "notify_desktop", "launch_application", "close_application",
            "keyboard_shortcut", "type_text", "image_resize"):
        return "desktop"
    if name in ("run_command", "run_cli_command"):
        return "shell"
    return "pure-local"


def synth_params(args: list, sandbox: Path, name: str) -> dict | None:
    """Synthetic params per arg-name heuristics; None = can't synthesize."""
    src_file = sandbox / "input.txt"
    src_file.write_text("hello world\nsecond line\n", encoding="utf-8")
    (sandbox / "adir").mkdir(exist_ok=True)
    values = {}
    for a in args:
        la = a.lower()
        if la in ("file_path", "path", "output_path", "dest", "destination",
                  "target_path", "output_file", "csv_path", "md_path"):
            values[a] = str(sandbox / f"out_{name}.txt")
        elif la in ("source", "src", "source_path", "input_path", "archive_path"):
            values[a] = str(src_file)
        elif la in ("directory", "dir_path", "folder", "dir", "directory_path"):
            values[a] = str(sandbox)
        elif la in ("content", "text", "data", "body"):
            values[a] = "hello from the audit"
        elif la in ("json_string", "json_text", "json_data", "raw_json"):
            values[a] = '{"a": 1, "b": [2, 3]}'
        elif la in ("date_string", "date", "date_str"):
            values[a] = "2026-06-12"
        elif la in ("format", "output_format", "date_format", "fmt"):
            values[a] = "%Y-%m-%d"
        elif la in ("filename", "file_name", "name"):
            values[a] = f"out_{name}"
        elif la in ("rows", "records"):
            values[a] = [{"col": "v1"}, {"col": "v2"}]
        elif la in ("headers", "columns"):
            values[a] = ["col"]
        elif la in ("files", "file_paths", "paths"):
            values[a] = [str(src_file)]
        elif la == "extension":
            values[a] = ".py"
        else:
            return None  # unknown arg — skip rather than guess wrong
    return values


def main() -> int:
    idx = json.loads((REPO / "systemu/vault/tools/index.json").read_text(encoding="utf-8"))
    rows, contract_violations = [], 0
    with tempfile.TemporaryDirectory() as td:
        for t in sorted(idx, key=lambda x: x["name"]):
            name = t["name"]
            impl = IMPL_DIR / f"{name}.py"
            row = {"tool": name, "deps": ",".join(t.get("dependencies") or []) or "-"}
            if not impl.exists():
                row.update(contract="MISSING-IMPL", cls="?", exec_="-")
                contract_violations += 1
                rows.append(row)
                continue
            try:
                tree = ast.parse(impl.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                row.update(contract=f"SYNTAX-ERROR:{exc.lineno}", cls="?", exec_="-")
                contract_violations += 1
                rows.append(row)
                continue
            imports = set()
            run_args, run_defaults = None, 0
            has_meta = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
                elif isinstance(node, ast.FunctionDef) and node.name == "run":
                    run_args = [a.arg for a in node.args.args]
                    run_defaults = len(node.args.defaults)
                elif isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == "TOOL_META":
                            has_meta = True
            if run_args is None:
                row.update(contract="NO-run()", cls="?", exec_="-")
                contract_violations += 1
                rows.append(row)
                continue
            row["contract"] = "ok" if has_meta else "ok(no-META)"
            cls = classify(imports, name)
            row["cls"] = cls

            if cls != "pure-local":
                row["exec_"] = "(not executed keyless)"
                rows.append(row)
                continue
            required = run_args[: len(run_args) - run_defaults]
            sandbox = Path(td) / name
            sandbox.mkdir(exist_ok=True)
            params = synth_params(required, sandbox, name)
            if params is None:
                row["exec_"] = f"SKIP unknown args {required}"
                rows.append(row)
                continue
            proc = subprocess.run(
                [sys.executable, str(RUNNER), str(impl), "--params", json.dumps(params)],
                capture_output=True, text=True, timeout=60)
            out = (proc.stdout or "").strip().splitlines()
            verdict = "NO-OUTPUT"
            if out:
                try:
                    payload = json.loads(out[-1])
                    verdict = ("PASS" if payload.get("success") else
                               f"FAIL: {str(payload.get('error'))[:60]}")
                except Exception:
                    verdict = f"BAD-JSON: {out[-1][:60]}"
            row["exec_"] = verdict
            rows.append(row)

    w = max(len(r["tool"]) for r in rows) + 1
    print(f"{'tool':<{w}} {'class':<11} {'contract':<12} {'deps':<28} execution")
    for r in rows:
        print(f"{r['tool']:<{w}} {r['cls']:<11} {r['contract']:<12} {r['deps']:<28} {r['exec_']}")
    n_pass = sum(1 for r in rows if r["exec_"] == "PASS")
    n_fail = sum(1 for r in rows if r["exec_"].startswith(("FAIL", "NO-OUTPUT", "BAD-JSON")))
    print(f"\n{len(rows)} tools | contract violations: {contract_violations} | "
          f"executed PASS: {n_pass} | executed FAIL: {n_fail}")
    return 1 if contract_violations else 0


if __name__ == "__main__":
    sys.exit(main())
