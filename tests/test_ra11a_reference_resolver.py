# tests/test_ra11a_reference_resolver.py
import time
from systemu.runtime.reference_resolver import resolve_reference, ReferenceVerdict


class _Granted:
    def __init__(self, ok=True): self._ok = ok
    def is_within_granted(self, p): return self._ok


def _situation(files):
    now = time.time()
    salient = [{"path": f"/root/{n}", "name": n, "ext": "." + n.rsplit(".", 1)[-1],
                "size": 10, "mtime": now - age} for (n, age) in files]
    return {"roots": [{"path": "/root", "salient": salient}]}


def test_clear_single_match_is_high_confidence():
    sit = _situation([("resume.pdf", 100), ("taxes_2019.xlsx", 99999)])
    v = resolve_reference("please summarize my resume", situation=sit, granted=_Granted())
    assert v.state == "resolvable"
    assert v.referent == "/root/resume.pdf"
    assert v.confidence >= 0.80          # clear winner

def test_ambiguous_two_way_never_reaches_auto_accept():
    sit = _situation([("report_q1.docx", 100), ("report_q2.docx", 120)])
    v = resolve_reference("open the report", situation=sit, granted=_Granted())
    assert v.state == "resolvable"
    assert v.confidence < 0.80           # capped below T_high — never auto-accepted
    assert v.candidate_count >= 2

def test_no_match_is_missing():
    sit = _situation([("photo.jpg", 100)])
    v = resolve_reference("send the invoice", situation=sit, granted=_Granted())
    assert v.state == "missing"
    assert v.referent is None

def test_out_of_root_candidate_is_dropped():
    sit = _situation([("resume.pdf", 100)])
    v = resolve_reference("my resume", situation=sit, granted=_Granted(ok=False))
    assert v.state == "missing"          # confinement rejected the only candidate

def test_synonym_extension_boosts_the_right_file():
    sit = _situation([("q3.xlsx", 100), ("notes.txt", 50)])
    v = resolve_reference("update the sheet", situation=sit, granted=_Granted())
    assert v.referent == "/root/q3.xlsx"   # 'sheet' -> .xlsx ext hint wins

def test_recency_breaks_a_name_tie_toward_newer():
    sit = _situation([("draft.docx", 100000), ("draft.docx", 10)])
    # same name, different mtime -> newer wins (still resolvable, may be ambiguous)
    v = resolve_reference("the draft", situation=sit, granted=_Granted())
    assert v.state == "resolvable"

def test_fail_safe_on_garbage_situation():
    v = resolve_reference("x", situation={"roots": "not-a-list"}, granted=_Granted())
    assert isinstance(v, ReferenceVerdict) and v.state == "missing"   # never raises


def test_granted_none_fails_closed_drops_all_refs():
    """Security: without a GrantedRoots authority the confinement re-gate cannot
    confirm confinement — it must FAIL CLOSED (drop every candidate), never
    fail-open and accept an unconfined path."""
    sit = _situation([("resume.pdf", 100)])
    v = resolve_reference("my resume", situation=sit, granted=None)
    assert v.state == "missing"
