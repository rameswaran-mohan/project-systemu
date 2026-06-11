"""Advisory static risk scan for generated tool code (Wave 2.1).

Replaces the Gate-2 substring scan ("eval(" in code) — which getattr/string
concatenation defeated trivially — with an AST walk.  Still ADVISORY by
nature: a static scan cannot prove code safe (runtime-constructed names,
encoded payloads, and import-time side effects all evade static analysis).
Surfaces MUST present findings as "advisory static scan", never as a
security verdict; the real boundary is the subprocess sandbox (Wave 2.2)
and the operator's Gate-3 enable.

``scan_code`` returns human-readable labels (one per finding kind, deduped),
matching the shape the Gate-2 dialog already renders.
"""
from __future__ import annotations

import ast
from typing import List

# Bare-name calls that execute arbitrary code.
_DANGEROUS_CALLS = {
    "eval":       "eval() — arbitrary code execution",
    "exec":       "exec() — arbitrary code execution",
    "compile":    "compile() — code-object construction",
    "__import__": "__import__() — dynamic import bypass",
}

# attr-qualified calls (module.attr) that touch the system.
_DANGEROUS_ATTRS = {
    ("os", "system"):     "os.system() call",
    ("os", "popen"):      "os.popen() call",
    ("os", "execv"):      "os.execv() call",
    ("os", "execve"):     "os.execve() call",
    ("os", "remove"):     "os.remove() — file deletion",
    ("os", "unlink"):     "os.unlink() — file deletion",
    ("shutil", "rmtree"): "recursive directory deletion (shutil.rmtree)",
    ("ctypes", "CDLL"):   "ctypes.CDLL() — native code loading",
}

# String payloads worth flagging wherever they appear.
_STRING_PAYLOADS = {
    "rm -rf":      "rm -rf in a string literal",
    "drop table":  "SQL DROP TABLE",
    "delete from": "SQL DELETE FROM",
}

# Substring fallback for unparseable code (the pre-W2.1 patterns).
_SUBSTRING_FALLBACK = {
    "shell=True":    "subprocess shell injection (shell=True)",
    "os.system":     "os.system() call",
    "os.popen":      "os.popen() call",
    "eval(":         "eval() — arbitrary code execution",
    "exec(":         "exec() — arbitrary code execution",
    "__import__":    "__import__() — dynamic import bypass",
    "shutil.rmtree": "recursive directory deletion (shutil.rmtree)",
    " rm -rf":       "rm -rf in a string literal",
    "DROP TABLE":    "SQL DROP TABLE",
    "DELETE FROM":   "SQL DELETE FROM",
}


class _RiskVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: List[str] = []

    def _add(self, label: str) -> None:
        if label not in self.findings:
            self.findings.append(label)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Bare-name calls: eval(x), exec(x), __import__('os'), compile(...)
        if isinstance(func, ast.Name):
            if func.id in _DANGEROUS_CALLS:
                self._add(_DANGEROUS_CALLS[func.id])
            if func.id == "getattr":
                self._check_getattr(node)
        # module.attr calls: os.system(...), shutil.rmtree(...)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            label = _DANGEROUS_ATTRS.get((func.value.id, func.attr))
            if label:
                self._add(label)
            # subprocess.<anything>(..., shell=True)
            if func.value.id == "subprocess":
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        self._add("subprocess shell injection (shell=True)")
        self.generic_visit(node)

    def _check_getattr(self, node: ast.Call) -> None:
        """getattr on builtins, or with a non-literal / concatenated name —
        the classic obfuscation that defeats substring scans."""
        if not node.args:
            return
        target = node.args[0]
        target_is_builtins = (
            (isinstance(target, ast.Name) and target.id in ("builtins", "__builtins__"))
        )
        name_arg = node.args[1] if len(node.args) > 1 else None
        name_is_static = isinstance(name_arg, ast.Constant)
        if target_is_builtins or not name_is_static:
            self._add(
                "dynamic attribute lookup (getattr with computed name / on "
                "builtins) — possible obfuscated call"
            )

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            low = node.value.lower()
            for needle, label in _STRING_PAYLOADS.items():
                if needle in low:
                    self._add(label)
        self.generic_visit(node)


def scan_code(code: str) -> List[str]:
    """Advisory scan of generated tool code; returns human-readable labels.

    AST-based (call-name resolution, shell=True keywords, getattr
    obfuscation, string payloads); falls back to the legacy substring scan —
    with an explicit "unparseable" marker — when the code does not parse.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        findings = [label for pattern, label in _SUBSTRING_FALLBACK.items()
                    if pattern in code]
        findings.append(
            "code is unparseable (SyntaxError) — substring fallback only, "
            "review manually"
        )
        return findings
    visitor = _RiskVisitor()
    visitor.visit(tree)
    return visitor.findings
