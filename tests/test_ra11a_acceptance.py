# tests/test_ra11a_acceptance.py
import time
from systemu.runtime.reference_resolver import resolve_reference


class _GrantedReal:
    """Confinement backed by a real granted-root prefix check (IMPL-9 realpath compare)."""
    def __init__(self, root): self.root = root
    def is_within_granted(self, p):
        import os
        try:
            rp = os.path.realpath(p); rr = os.path.realpath(self.root)
            return os.path.commonpath([rp, rr]) == rr
        except Exception:
            return False


def _sit(files, root="/granted"):
    now = time.time()
    return {"roots": [{"path": root, "salient": [
        {"path": f"{root}/{n}", "name": n, "ext": "." + n.rsplit('.',1)[-1],
         "size": 1, "mtime": now} for n in files]}]}


def test_ac_a_absolute_path_outside_root_is_rejected():
    # a salient handle claiming an out-of-root path is dropped by the confinement gate
    sit = {"roots": [{"path": "/granted", "salient": [
        {"path": "/etc/passwd", "name": "passwd", "ext": "", "size": 1, "mtime": time.time()}]}]}
    v = resolve_reference("read passwd", situation=sit, granted=_GrantedReal("/granted"))
    assert v.state == "missing"

def test_ac_b_ambiguous_never_auto_accepts():
    sit = _sit(["report_a.pdf", "report_b.pdf"])
    v = resolve_reference("the report", situation=sit, granted=_GrantedReal("/granted"))
    assert v.confidence < 0.80

def test_ac_b_zero_candidates_asks_for_path():
    v = resolve_reference("the invoice", situation=_sit(["cat.jpg"]),
                          granted=_GrantedReal("/granted"))
    assert v.state == "missing"

def test_fail_safe_never_raises_on_bad_granted():
    class _Boom:
        def is_within_granted(self, p): raise RuntimeError("boom")
    v = resolve_reference("x file", situation=_sit(["x.txt"]), granted=_Boom())
    assert v.state == "missing"      # the per-candidate except drops it; no propagation
