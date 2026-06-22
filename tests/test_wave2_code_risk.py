"""Wave 2.1 — the Gate-2 risk scan must be AST-based and honestly framed.

The old scan was naive substring matching ("eval(" in code) — trivially
bypassed by getattr(builtins, 'ev'+'al') and string concatenation — yet it
was presented at Gate 2 with the authority of a security check.  The new
``systemu.runtime.code_risk.scan_code`` walks the AST (resolving call names,
shell=True keywords, dynamic-attribute obfuscation) and falls back to the
substring scan only for unparseable code.  It is labelled ADVISORY.
"""
from systemu.runtime.code_risk import scan_code


def _labels(code: str) -> str:
    return " | ".join(scan_code(code)).lower()


class TestDirectCalls:
    def test_eval_call(self):
        assert "eval" in _labels("x = eval(user_input)")

    def test_exec_call(self):
        assert "exec" in _labels("exec(payload)")

    def test_dunder_import(self):
        assert "__import__" in _labels("m = __import__('os')")

    def test_os_system(self):
        assert "os.system" in _labels("import os\nos.system('ls')")

    def test_os_popen(self):
        assert "os.popen" in _labels("import os\nos.popen('whoami')")

    def test_subprocess_shell_true(self):
        assert "shell" in _labels(
            "import subprocess\nsubprocess.run(cmd, shell=True)")

    def test_shutil_rmtree(self):
        assert "rmtree" in _labels("import shutil\nshutil.rmtree(path)")


class TestObfuscationCatches:
    def test_getattr_builtins_concat(self):
        # THE bypass that defeated the substring scan.
        code = "import builtins\nf = getattr(builtins, 'ev' + 'al')\nf(x)"
        out = _labels(code)
        assert "dynamic" in out or "getattr" in out

    def test_getattr_dunder_builtins(self):
        code = "f = getattr(__builtins__, name)\nf(x)"
        out = _labels(code)
        assert "dynamic" in out or "getattr" in out


class TestStringPayloads:
    def test_rm_rf_in_string(self):
        assert "rm -rf" in _labels("cmd = 'rm -rf /tmp/x'")

    def test_sql_drop_table(self):
        assert "drop table" in _labels('q = "DROP TABLE users"')


class TestCleanCodeAndFallback:
    def test_clean_code_no_findings(self):
        code = (
            "import json\n"
            "TOOL_META = {'name': 't'}\n"
            "def run(params):\n"
            "    return {'ok': json.dumps(params)}\n"
        )
        assert scan_code(code) == []

    def test_eval_as_substring_of_name_not_flagged(self):
        # The substring scan flagged 'evaluate(' because it contains 'eval('.
        # Hmm — actually "evaluate(" does NOT contain "eval(" as substring
        # ('evaluate(' has 'eval' then 'uate'). Use a real false-positive:
        # a comment mentioning eval must not be flagged by the AST scan.
        code = "# never use eval() here\nx = 1\n"
        assert scan_code(code) == []

    def test_syntax_error_falls_back_to_substring(self):
        broken = "def run(:\n    eval(x)"
        out = _labels(broken)
        assert "eval" in out          # caught by fallback
        assert "unparseable" in out   # and honestly labelled as such
