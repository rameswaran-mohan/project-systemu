"""DEC-31 — redaction is the FIRST transformation on a value crossing a trust
boundary: before truncation, slicing, escaping, ``%r``, ``str()``.

The defect this pins is not hypothetical and not a near-miss. Two independent
reproductions on this tree:

1. ``outbox.redact`` — a 230-char JWT returns
   ``[redacted - looked like a credential]``. The SAME token sliced to 64 chars
   first (``redact(jwt[:64])``) returns 64 CLEAR characters: the pattern needs
   all three segments, and the slice removes two of them.

2. The tool-success auto-audit path — ``_truncate_audit_params`` clipped each
   param value to 200 chars with no masking at all, and the clipped value was
   written to the durable audit row. A 392-char JWT under a NEUTRAL key became
   200 clear characters on disk whose base64 decoded to
   ``{"sub":...,"role":"admin","apikey":"SECRET-VALUE-DO-NOT-LOG",...}``.
   The ledger's ``mask_and_digest_params`` then masked the already-truncated
   husk and found nothing to mask.

Every fixture below is sized by :mod:`tools.redaction_fixtures` against the LIVE
cap rather than by a hand-picked constant. That is the second half of DEC-31:
the pin that was supposed to catch (1) passed *because* its 45-char fixture fit
under the 64-char slice, so it exercised the redactor and never the truncation.
"""
from __future__ import annotations

import base64

import pytest

from tools.redaction_fixtures import (KINDS, TRUNCATION_BLINDABLE_KINDS,
                                      RedactionFixtureError, caps_on,
                                      largest_cap, secret_for,
                                      secret_longer_than,
                                      truncation_blind_secret,
                                      truncation_blind_secret_for)

_MARKER = "[redacted - looked like a credential]"


# ── the reproduction, as a standing pin ──────────────────────────────────────

def test_slicing_before_redacting_is_what_leaks():
    """Characterisation of the DEFECT, so the fix below is measured against a
    demonstrated hazard rather than an asserted one."""
    from systemu.runtime.outbox import redact

    jwt = truncation_blind_secret(64, kind="jwt")
    assert redact(jwt) == _MARKER, "the full value IS recognised"
    # redaction-lint: ok — this IS the forbidden order, on purpose. The test
    # exists to demonstrate that it leaks; writing it the safe way would delete
    # the evidence the fix is measured against.
    leaked = redact(jwt[:64])
    assert _MARKER not in leaked, "sliced first, the redactor sees nothing"
    assert leaked.startswith("eyJ"), leaked


def test_the_explicit_zero_lower_bound_spelling_leaks_identically():
    """``v[0:64]`` is BYTE-IDENTICAL to ``v[:64]`` — same leak, one extra
    character. This is what ``tools/lint_redaction_order.py``'s AST matcher
    had to learn to recognise: ``ast.Slice(lower=None)`` and
    ``ast.Slice(lower=Constant(0))`` produce the same string at runtime, so a
    matcher that only checked ``lower is None`` missed half the shape while
    the runtime hazard — demonstrated here — was identical either way.
    """
    from systemu.runtime.outbox import redact

    jwt = truncation_blind_secret(64, kind="jwt")
    # redaction-lint: ok — demonstrating the explicit-zero spelling of the
    # same forbidden order; writing it the safe way would delete the
    # evidence.
    leaked = redact(jwt[0:64])
    assert _MARKER not in leaked, "sliced first, the redactor sees nothing"
    assert leaked.startswith("eyJ"), leaked
    # redaction-lint: ok — same demonstration, proving the two SPELLINGS of
    # the slice are the same DEFECT rather than two different ones.
    assert leaked == redact(jwt[:64]), "the two slice spellings must leak identically"


# ── outbox.redact(max_len=...) — the cap applies AFTER masking ───────────────

def test_max_len_defaults_to_no_cap_so_existing_callers_are_unchanged():
    from systemu.runtime.outbox import redact

    for value in ("ordinary prose", "", None, "a" * 5000, "x"):
        assert redact(value) == redact(value, max_len=None), repr(value)


def test_a_secret_longer_than_max_len_is_REDACTED_not_sliced():
    """The whole point. ``redact(v, max_len=64)`` must never behave like
    ``redact(v[:64])``."""
    from systemu.runtime.outbox import redact

    jwt = truncation_blind_secret(64, kind="jwt")
    assert redact(jwt, max_len=64) == _MARKER
    # redaction-lint: ok — the forbidden form is the CONTROL in this comparison;
    # the assertion is precisely that the two are not equivalent.
    assert redact(jwt, max_len=64) != redact(jwt[:64])


def test_an_embedded_secret_is_masked_before_the_cap_can_hide_it():
    """A secret in the middle of prose, past the cap. Masking first removes it;
    capping first would emit the leading prose and silently drop the evidence
    that anything was redacted at all."""
    from systemu.runtime.outbox import redact

    jwt = truncation_blind_secret(64, kind="jwt")
    prose = "deploy log line number one, token follows: " + jwt
    out = redact(prose, max_len=64)
    assert len(out) <= 64
    # NOT `jwt[:32] not in out`: that assertion was blind by construction. With
    # the cap applied first, only ~21 characters of the token survive, so a
    # 32-char needle is absent from a line that is leaking — the same
    # fits-under-the-cap mistake DEC-31 is about, reproduced in the pin itself.
    # Verified by mutation: capping before masking leaves "eyJ" in the output.
    assert "eyJ" not in out, out
    # ...and redaction has to be VISIBLE, not merely "the needle missed".
    assert _MARKER in out or "***" in out, out


def test_the_result_never_exceeds_max_len():
    from systemu.runtime.outbox import redact

    for n in (1, 2, 5, 20, 37, 200):
        for value in ("prose " * 400, secret_longer_than(300, kind="jwt"), "hi"):
            assert len(redact(value, max_len=n)) <= n, (n, value[:20])


def test_a_cap_too_small_for_the_marker_truncates_the_MARKER_not_the_secret():
    """``max_len`` smaller than the marker is a caller mistake, but it must not
    resolve by emitting secret material. The marker is a constant; a prefix of a
    constant discloses nothing."""
    from systemu.runtime.outbox import redact

    jwt = secret_longer_than(300, kind="jwt")
    out = redact(jwt, max_len=6)
    assert len(out) <= 6
    assert out == _MARKER[:5] + "…", out
    assert "eyJ" not in out


def test_max_len_zero_yields_empty_not_the_whole_value():
    from systemu.runtime.outbox import redact

    assert redact("prose", max_len=0) == ""
    assert redact(secret_longer_than(100), max_len=0) == ""


def test_a_negative_max_len_raises_instead_of_slicing_from_the_END():
    """``out[:-5]`` is silently valid Python and chops the tail. A cap that
    quietly means the opposite of what it says is how this class of bug gets
    reintroduced."""
    from systemu.runtime.outbox import redact

    with pytest.raises(ValueError):
        redact("prose", max_len=-1)


def test_truncation_is_marked_so_a_capped_value_is_not_read_as_complete():
    from systemu.runtime.outbox import redact

    out = redact("word " * 200, max_len=40)
    assert out.endswith("…"), out
    assert len(out) == 40


def test_a_value_shorter_than_max_len_is_returned_whole_without_a_marker():
    from systemu.runtime.outbox import redact

    assert redact("short prose", max_len=100) == "short prose"


# ── the migrated call site: the tool-success auto-audit row ──────────────────

def test_the_audit_row_masks_BEFORE_it_truncates():
    """The live leak, and the pin that actually distinguishes the two ORDERS.

    ``truncation_blind_secret_for`` (not merely ``secret_for``) is load-bearing
    here. A fixture that is only *longer than the cap* can still be recognised
    after truncation, and then the assertion holds under both orders. Measured:
    with a 236-char JWT against the 200-char cap, the true order-flip mutation
    SURVIVED this pin. The verified fixture kills it.
    """
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    jwt = truncation_blind_secret_for("audit_params", kind="jwt")
    assert len(jwt) > largest_cap("audit_params")

    out = _truncate_audit_params({"body": jwt})
    written = out["body"]
    assert "eyJ" not in written, f"clear JWT survived into the audit row: {written[:80]}"
    assert "[REDACTED]" in written, written


def test_the_leaked_prefix_no_longer_decodes_to_the_claims():
    """Names the actual disclosure rather than only asserting a marker: the
    surviving base64 used to decode to the role and the api key."""
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    jwt = truncation_blind_secret_for("audit_params", kind="jwt")
    written = _truncate_audit_params({"body": jwt})["body"]

    decoded = ""
    for seg in written.split("."):
        s = seg
        while s:
            try:
                decoded += base64.urlsafe_b64decode(
                    s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
                break
            except Exception:
                s = s[:-1]
    assert "SECRET-VALUE-DO-NOT-LOG" not in decoded, decoded
    assert '"role":"admin"' not in decoded, decoded


@pytest.mark.parametrize("kind", KINDS)
def test_every_offered_secret_shape_is_caught_on_the_audit_path(kind):
    """A builder whose shape the redactor does not recognise would make every
    fixture built from it a vacuous pass."""
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    secret = secret_for("audit_params", kind=kind)
    written = _truncate_audit_params({"body": secret})["body"]
    assert "[REDACTED]" in written, (kind, written[:120])


def test_a_secret_NAMED_key_is_redacted_on_the_audit_row():
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    out = _truncate_audit_params({"api_key": "hunter2", "password": "swordfish"})
    assert "hunter2" not in str(out), out
    assert "swordfish" not in str(out), out


def test_the_audit_row_still_truncates_bulk_content():
    """The cap's original job — keeping a huge ``content=`` blob out of the
    audit JSONL — is unchanged."""
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    out = _truncate_audit_params(
        {"content": "z" * 5000, "n": 7, "flag": True, "x": None})
    assert len(out["content"]) <= 256
    assert out["content"].endswith("[truncated]")
    assert out["n"] == 7 and out["flag"] is True and out["x"] is None


def test_the_audit_row_fails_CLOSED_when_the_masker_is_unavailable(monkeypatch):
    """An import failure must not resolve by writing the clear value. The audit
    row is durable and the run must not break, so the row is still written —
    with the values dropped, not with the values in clear."""
    import builtins

    import systemu.runtime.shadow_runtime as sr

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if "external_verifier" in name:
            raise ImportError("masker unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    jwt = secret_for("audit_params", kind="jwt")
    out = sr._truncate_audit_params({"body": jwt})
    assert "eyJ" not in str(out), out
    assert out, "the row itself must survive — only the values are dropped"


def test_non_dict_params_still_yield_an_empty_dict():
    from systemu.runtime.shadow_runtime import _truncate_audit_params

    assert _truncate_audit_params("nope") == {}
    assert _truncate_audit_params(None) == {}


# ── the fixture-length rule itself ───────────────────────────────────────────

def test_the_helper_returns_a_secret_longer_than_every_live_cap_on_the_path():
    for path in ("audit_params", "outbox_component"):
        assert len(secret_for(path)) > largest_cap(path), path


def test_caps_are_read_LIVE_from_the_owning_module_not_copied(monkeypatch):
    """Raising the real cap must lengthen the fixture automatically. A literal
    copied into this module would keep producing the old, now-too-short value —
    and would fail SAFE-looking, because a short fixture goes green."""
    import systemu.runtime.shadow_runtime as sr

    before = largest_cap("audit_params")
    monkeypatch.setattr(sr, "_AUDIT_PARAM_VALUE_CAP", before + 4096)
    assert largest_cap("audit_params") == before + 4096
    assert len(secret_for("audit_params")) > before + 4096


def test_an_unknown_path_raises_rather_than_sizing_against_nothing():
    with pytest.raises(RedactionFixtureError):
        caps_on("no-such-path")
    with pytest.raises(RedactionFixtureError):
        secret_for("no-such-path")


def test_no_caps_raises():
    with pytest.raises(RedactionFixtureError):
        secret_longer_than()


def test_a_non_integer_or_negative_cap_raises():
    with pytest.raises(RedactionFixtureError):
        secret_longer_than("64")            # type: ignore[arg-type]
    with pytest.raises(RedactionFixtureError):
        secret_longer_than(-1)
    with pytest.raises(RedactionFixtureError):
        secret_longer_than(True)            # bool is an int subclass — reject


def test_an_unknown_kind_raises():
    with pytest.raises(RedactionFixtureError):
        secret_longer_than(64, kind="shapeless")


@pytest.mark.parametrize("kind", KINDS)
def test_every_builder_clears_a_large_cap(kind):
    for cap in (0, 1, 64, 200, 1024, 5000):
        assert len(secret_longer_than(cap, kind=kind)) > cap, (kind, cap)


@pytest.mark.parametrize("kind", KINDS)
def test_every_builder_produces_a_shape_the_outbox_redactor_recognises(kind):
    from systemu.runtime.outbox import redact

    assert redact(secret_longer_than(200, kind=kind)) == _MARKER, kind


# ── longer-than-the-cap is NECESSARY BUT NOT SUFFICIENT ──────────────────────

def test_merely_longer_than_the_cap_can_still_be_detected_after_truncation():
    """The trap, stated as a fact about this tree rather than a warning.

    ``secret_for("audit_params")`` returns a 236-char JWT for the 200-char cap.
    Slicing it at 200 leaves 18 characters of the signature segment, and the
    JWT pattern only requires 4 — so the truncated form is STILL recognised,
    and a pin built on it passes under both orders. This is the fixture-side
    version of DEC-31, and it is why the helper verifies rather than assumes.
    """
    from systemu.runtime.external_verifier import _scrub_value_shapes

    cap = largest_cap("audit_params")
    naive = secret_for("audit_params", kind="jwt")
    assert len(naive) > cap                                   # rule satisfied...
    # redaction-lint: ok — the forbidden order is the SUBJECT of this test. The
    # slice is here to show that a merely-longer-than-the-cap fixture survives
    # it and stays detectable, which is what makes such a fixture useless.
    assert "[REDACTED]" in _scrub_value_shapes(naive[:cap])    # ...and still blind


def test_the_verified_fixture_is_hidden_by_truncation():
    from systemu.runtime.external_verifier import _scrub_value_shapes
    from systemu.runtime.outbox import redact

    cap = largest_cap("audit_params")
    jwt = truncation_blind_secret_for("audit_params", kind="jwt")
    assert len(jwt) > cap
    # detected whole...
    assert "[REDACTED]" in _scrub_value_shapes(jwt)
    assert redact(jwt) == _MARKER
    # ...and invisible once sliced, which is what makes the ordering testable.
    # redaction-lint: ok — asserting that the slice BLINDS both detectors is the
    # whole point; writing it in the safe order would assert nothing.
    assert "[REDACTED]" not in _scrub_value_shapes(jwt[:cap])
    # redaction-lint: ok — same demonstration against the outbox detector.
    assert redact(jwt[:cap]) != _MARKER


@pytest.mark.parametrize("kind", [k for k in KINDS
                                  if k not in TRUNCATION_BLINDABLE_KINDS])
def test_prefix_matchable_shapes_REFUSE_to_pretend_they_can_pin_an_ordering(kind):
    """Measured: ``Bearer …``, ``sk-…`` and long hex all match on any long-enough
    PREFIX, so truncation never hides them and no fixture of that shape can tell
    the two orders apart. Returning one anyway would hand back a test that
    cannot fail — so the helper raises instead."""
    with pytest.raises(RedactionFixtureError, match="cannot hide it"):
        truncation_blind_secret(200, kind=kind)


def test_the_blindable_kinds_list_is_accurate_in_both_directions():
    for kind in TRUNCATION_BLINDABLE_KINDS:
        assert truncation_blind_secret(200, kind=kind)
    assert set(TRUNCATION_BLINDABLE_KINDS) <= set(KINDS)
