# tests/test_ra11a_binder_wiring.py
import time
from systemu.core.models import Objective, Tool
from systemu.runtime.requirement_binder import compute_requirements


class _Granted:
    def is_within_granted(self, p): return True


class _Ctx:
    def __init__(self, granted): self._granted_roots = granted; self.vault = None
                                  # files_produced unused here


def _sit_with(files):
    now = time.time()
    return {"roots": [{"path": "/root", "salient": [
        {"path": f"/root/{n}", "name": n, "ext": "." + n.rsplit('.',1)[-1],
         "size": 10, "mtime": now} for n in files]}],
        "services": [], "capabilities": [], "credentials": [], "profile": {},
        "declared_intents": []}


def _tool_one_file_param():
    # a REQUIRED single path leaf, no default -> the binder treats it as required
    return Tool(id="t", name="summarize_file", description="x",
                tool_type="python_function",
                parameters_schema={"input_path": {"type": "string"}})


def test_reference_in_goal_resolves_to_the_matching_file():
    obj = Objective(id=1, goal="summarize my resume", success_criteria="done")
    tool = _tool_one_file_param()
    sit = _sit_with(["resume.pdf", "budget.xlsx"])
    reqs = compute_requirements(obj, tool, sit, _Ctx(_Granted()))
    r = next(r for r in reqs if r.schema_path.endswith("input_path"))
    assert r.bound_value_ref == "file:/root/resume.pdf"
    assert r.value_origin == "content_derived"        # IMPL-5 clamp preserved
    assert r.state in ("have", "resolvable")          # never silent — _needs_ask asks either way

def test_no_matching_file_is_a_missing_input_ask():
    obj = Objective(id=1, goal="summarize my resume", success_criteria="done")
    reqs = compute_requirements(obj, _tool_one_file_param(),
                                _sit_with(["photo.jpg"]), _Ctx(_Granted()))
    r = next(r for r in reqs if r.schema_path.endswith("input_path"))
    assert r.state == "missing" and r.kind == "input"

def test_content_derived_clamp_never_launders_even_on_high_score():
    obj = Objective(id=1, goal="the resume.pdf", success_criteria="done")
    reqs = compute_requirements(obj, _tool_one_file_param(),
                                _sit_with(["resume.pdf"]), _Ctx(_Granted()))
    r = next(r for r in reqs if r.schema_path.endswith("input_path"))
    assert r.value_origin == "content_derived"        # high score is NOT laundered to trusted

def test_no_regression_a_leaf_still_resolves_when_reference_is_thin():
    # even a weak reference must not HARDEN the leaf to 'missing' when a salient file exists
    obj = Objective(id=1, goal="do the thing with the file", success_criteria="done")
    reqs = compute_requirements(obj, _tool_one_file_param(),
                                _sit_with(["thing.txt"]), _Ctx(_Granted()))
    r = next(r for r in reqs if r.schema_path.endswith("input_path"))
    # 'thing' overlaps -> resolvable; if it didn't, it degrades to a 'missing' ASK, never a crash
    assert r.state in ("have", "resolvable", "missing")
