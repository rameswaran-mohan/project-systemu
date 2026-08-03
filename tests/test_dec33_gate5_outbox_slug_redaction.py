"""U-12 Outbox slug — DEC-34c: derive it from a NON-PROMPT source.

Lineage, so it is not silently erased:

  * DEC-33 GATE-5 named the real gap: the Outbox directory NAME is a real
    on-disk artifact — a trust-boundary crossing exactly like the receipt
    body — and it never went through :func:`~systemu.runtime.outbox.redact`.
    ``safe_component``'s allowlist (``A-Za-z0-9_.-``) preserves the ENTIRE
    alphabet a credential is made of; nothing masked anything.

  * Round 2 (commit ``80cbdbc0``) "fixed" it by routing the prompt through
    ``redact(prompt, max_len=MAX_SLUG_CHARS)`` before ``safe_component``.
    **REFUTED.** Measured directly against this tree (see
    ``_reconstruct_round2_slug`` below, which rebuilds round 2's exact
    expression so the claim is demonstrated, not asserted):

      - The 8 realistic, CREDENTIAL-FREE prompts fixtured below as
        ``_ONCE_COLLAPSING_PROMPTS`` all collapsed to the SAME constant slug
        (pinned, not merely claimed — see
        ``test_the_8_prompts_really_did_collapse_under_round_2s_approach``).
        They are a sample, not an exhaustive count: a broader informal sweep
        turned up more than a dozen further examples of the same failure
        mode, only 8 of which are kept here as fixtures.
        ``ask_promotion._value_is_secret`` treats the WHOLE value as a
        credential the instant ``mask_outbound`` changes anything ANYWHERE
        inside it, and an ordinary sentence is enough — "Compare Basic and
        Bearer authentication for the API design doc" matches ``_BEARER_RE``
        (``Bearer authentication`` reads as a bearer-scheme token), "Client
        secret: rotate it next week" matches the kv-secret pattern
        (``secret:`` followed by a word). Two unrelated tasks that both
        happened to phrase their prompt this way would be indistinguishable
        except for ``_unique_dir``'s numeric suffix.
      - Credential SHAPES ``mask_outbound`` does not know — a pasted PEM/SSH
        key, an underscore-style provider secret (``sk_…`` with UNDERSCORES,
        which the shipped ``sk-`` shape-rule requires a DASH to match and
        therefore misses), a Google API key, a raw AWS secret key, a bare-hex
        checksum, a shapeless passphrase — sailed through BOTH fences
        unchanged and landed, allowlist-sanitized but fully legible, in the
        folder name.

  * DEC-34c: you cannot make attacker-controlled free text safe FOR A
    FILENAME by filtering it — redact-then-sanitize is the wrong operation
    for an identifier, no matter how good the filter gets. The fix is
    architectural: the slug is a PURE FUNCTION of ``task_id`` (a
    :class:`~systemu.runtime.outbox.TrustedIdentity`) plus the date already
    carried by ``stamp``. ``prompt`` is not consulted for the slug, or for
    any artifact basename, AT ALL.

  * Round 3 (commit ``d350c99e``) shipped the architecture above and moved
    the artifact basename off ``src.name`` onto the loop ordinal too — but
    was REFUTED on two counts, round 4 (this module's newest tests):

      - It over-applied AC-1 to the artifact's SUFFIX, not just its STEM:
        the copy was named ``artifact-<ordinal>`` with NO extension at all,
        destroying every consumer that keys off a file's TYPE (a watcher
        script, a double-click). The suffix is type information, not
        identity — fixed with a fixed, code-defined ALLOWLIST of suffixes
        (``outbox._ARTIFACT_EXTENSIONS`` / ``_artifact_extension``), which
        satisfies AC-1 exactly as well as dropping it did (an attacker still
        cannot land an arbitrary byte in the name) while preserving what
        downstream consumers need. See
        ``tests/test_rutl1_intake_and_outbox.py``'s
        ``test_a_recognised_extension_survives_end_to_end`` and
        ``test_an_unrecognised_suffix_yields_no_suffix_never_a_passthrough``.
      - ``render_receipt``'s "copied from" line fed the WHOLE original path
        to ``redact()`` as one compound string, so two DIFFERENT artifacts
        collapsed to the IDENTICAL rendered line whenever the run's own
        directory happened to be shape-matching (a 40+ hex run id is
        enough, and carries no credential) — round 2's own refutation
        shape, reintroduced one layer down. Fixed by ``outbox._esc_path``,
        which redacts the parent directory and the basename as two
        independent values. See the "ROUND 4 (AC-2)" section below.

This module pins the ARCHITECTURE, not a detector's coverage:

  * AC-1 — no byte of the prompt (or any other untrusted free-text field —
    an artifact's original STEM) reaches a filename, folder name, or
    artifact basename. Proven for the 8 prompts that demonstrably collapsed
    under round 2, and for every credential shape round 2 missed — NOT
    because the new code recognises them (it never looks at ``prompt`` for
    this purpose), but because prompt text is never a path input. Round 4:
    the artifact's ALLOWLISTED extension is not covered by this claim and is
    not meant to be — see the round-3 refutation note above.
  * AC-2 — same ``task_id`` reproduces its slug; different ``task_id``
    values produce different slugs; a genuine same-day-same-``task_id``
    collision still gets ``_unique_dir``'s numeric suffix. (Round 4 adds a
    SECOND, unrelated AC-2 — the receipt's per-artifact identity — see below;
    the name collides only because round 3 numbered its own ACs 1-4 and
    round 4 found a new finding worth the same letter's weight.)
  * AC-3 — BOTH ``safe_component`` call sites (the folder slug in
    ``write_outbox``, and the per-artifact basename in ``_copy_artifacts``)
    derive from a ``TrustedIdentity``, never from ``prompt``/``src.name``.
    ``safe_component`` refuses a BARE (unwrapped) ``str`` with a
    ``TypeError`` — pinned here end-to-end through ``write_outbox`` (the
    unit-level interface pin lives in ``test_rutl1_intake_and_outbox``).
    Round 4 correction: that ``TypeError`` is narrower than early docs
    claimed — it catches an unwrapped value, not a carelessly-WRAPPED one
    (``TrustedIdentity(prompt)`` would satisfy it); see
    :func:`~systemu.runtime.outbox.safe_component`'s docstring for the
    corrected claim and ``test_both_safe_component_call_sites_pass_a_TrustedIdentity_by_source``
    for the source-shape tripwire that is the real (narrower) guarantee.
  * AC-4 — content surfaces (``receipt.html``, ``FAILED-*.txt`` BODY) still
    redact the prompt exactly as before; only the FILENAME stopped depending
    on that redaction.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tools.redaction_fixtures import secret_for, truncation_blind_secret_for

_OLD_MARKER = "[redacted - looked like a credential]"


def _run_dir_names(tmp_path: Path) -> set:
    outbox = tmp_path / "Outbox"
    return {p.name for p in outbox.iterdir()} if outbox.is_dir() else set()


def _reconstruct_round2_slug(prompt: str) -> str:
    """Rebuilds round 2's exact (refuted) expression against TODAY's
    ``redact``/``safe_component`` so the grounding claims below are measured,
    not merely narrated. Not a call into ``write_outbox`` — round 2's defect
    lived entirely in this one expression, which DEC-34c deleted from
    ``write_outbox`` and replaced with a ``task_id``-only slug."""
    from systemu.runtime.outbox import (MAX_SLUG_CHARS, TrustedIdentity,
                                        redact, safe_component)
    # The wrap here is ONLY to drive the (now-gated) sanitizer for this
    # historical reconstruction — it is not a claim that redact()'s output is
    # a TrustedIdentity going forward; see the module docstring.
    return safe_component(
        TrustedIdentity(redact(prompt, max_len=MAX_SLUG_CHARS)),
        fallback="task")


# ── grounding: round 2's approach really did collapse / really did leak ─────

# Each prompt below is CREDENTIAL-FREE — no live token, key, or password
# appears anywhere in the text.
_ONCE_COLLAPSING_PROMPTS = [
    "Set token: pending IT approval before Friday",
    "Client secret: rotate it next week per the security review",
    "Auth: waiting on the vendor to reset our access",
    "Compare Basic and Bearer authentication for the API design doc",
    "Check our IAM docs reference AKIAIOSFODNN7EXAMPLE correctly",
    "Ask ops whether Authorization: is still required on staging",
    "Cookie: banner copy needs a legal review before launch",
    "Set private_key=stored offline, remind me to check the backup",
]


def test_the_8_prompts_really_did_collapse_under_round_2s_approach():
    """Grounding, not narration: reconstruct round 2's expression and show
    all 8 land on the exact same constant. If this ever stops being true
    (the shipped detectors changed), the fixtures above need refreshing —
    the tests below do NOT depend on this staying true, only this one does."""
    slugs = {_reconstruct_round2_slug(p) for p in _ONCE_COLLAPSING_PROMPTS}
    assert slugs == {"redacted_-_looked_like_a_credential"}, slugs


@pytest.mark.parametrize("prompt", _ONCE_COLLAPSING_PROMPTS)
def test_each_of_the_8_prompts_individually_collapses_under_round_2s_approach(
        prompt):
    """Round 4 correction: the set-equality check above is a real pin (any
    divergence changes the set's size), but it reports failures as an
    unexplained set diff rather than naming WHICH prompt changed behavior.
    Parametrizing per-prompt, with an explicit expected value on every one
    of the 8, means a future divergence in just one of them is reported by
    that prompt's own pytest id instead of folding into one aggregate
    failure — "a universal-property assertion with no breaking fixtures is
    not a pin" (DEC-32)."""
    assert _reconstruct_round2_slug(prompt) == "redacted_-_looked_like_a_credential"


# ── AC-1 / AC-2 — the once-collapsing prompts, under the real fix ───────────

@pytest.mark.parametrize("prompt", _ONCE_COLLAPSING_PROMPTS)
def test_a_once_collapsing_prompt_no_longer_touches_the_slug_at_all(
        tmp_path, prompt):
    """None of these are read for the slug any more, so — unlike round 2 —
    they cannot collapse: the slug depends on ``task_id``, not on whether
    ``redact()`` happens to think the prompt looks like a credential."""
    from systemu.runtime import outbox

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="fixed-task-id", prompt=prompt, status="success",
        files_produced=[]))
    assert "redacted" not in run_dir.name
    assert "credential" not in run_dir.name


def test_the_8_once_collapsing_prompts_now_produce_8_distinct_slugs(tmp_path):
    """The direct AC-2 claim: 8 different TASKS (distinct task_id), even
    carrying the exact prompts that used to collapse to one constant, land
    in 8 DIFFERENT folders — with no dependence on any of them being
    redaction-order-lucky."""
    from systemu.runtime import outbox

    names = []
    for i, prompt in enumerate(_ONCE_COLLAPSING_PROMPTS):
        run_dir = Path(outbox.write_outbox(
            tmp_path, task_id=f"task-{i}", prompt=prompt, status="success",
            files_produced=[]))
        names.append(run_dir.name)

    assert len(set(names)) == len(names), names
    assert not any("redacted" in n and "credential" in n for n in names), names


def test_the_8_once_collapsing_prompts_land_on_8_DISTINCT_EXPECTED_slugs(
        tmp_path):
    """Not just "distinct" — DISTINCT AND PREDICTED: the slug is a pure,
    known function of ``task_id`` (``task-0`` .. ``task-7``, already inside
    ``safe_component``'s allowlist, so untouched by it), independent of
    which of the 8 once-collapsing prompts rides along. Fixing the EXPECTED
    value per prompt is what makes this a pin rather than a "no collision"
    smoke check — a slug computed from a hash of the prompt, or from
    anything else that happened not to collide today, would fail this."""
    from systemu.runtime import outbox

    stamp = datetime(2026, 7, 31, 9, 0, 0)
    expected = [f"2026-07-31-task-{i}" for i in range(len(_ONCE_COLLAPSING_PROMPTS))]
    assert len(set(expected)) == len(expected), "fixture bug: expected values collide"

    actual = []
    for i, prompt in enumerate(_ONCE_COLLAPSING_PROMPTS):
        vault = tmp_path / f"v{i}"
        run_dir = Path(outbox.write_outbox(
            vault, task_id=f"task-{i}", prompt=prompt, status="success",
            files_produced=[], now=stamp))
        actual.append(run_dir.name)

    assert actual == expected, list(zip(_ONCE_COLLAPSING_PROMPTS, actual, expected))


def test_the_slug_does_not_depend_on_which_prompt_is_supplied(tmp_path):
    """The architectural claim, directly: holding ``task_id`` and the
    timestamp fixed, the slug is IDENTICAL no matter which prompt — one of
    the 8 once-collapsing ones, or an ordinary one — is supplied. Each
    prompt gets its own vault subdirectory so ``_unique_dir``'s collision
    suffix cannot be mistaken for the prompt actually mattering."""
    from systemu.runtime import outbox

    stamp = datetime(2026, 7, 31, 9, 0, 0)
    prompts = _ONCE_COLLAPSING_PROMPTS + ["an entirely ordinary, unrelated prompt"]
    slugs = set()
    for i, prompt in enumerate(prompts):
        vault = tmp_path / f"v{i}"
        run_dir = Path(outbox.write_outbox(
            vault, task_id="same-task-id", prompt=prompt, status="success",
            files_produced=[], now=stamp))
        slugs.add(run_dir.name)
    assert len(slugs) == 1, slugs


# ── AC-1 — credential SHAPES round 2 missed, now unrepresentable ────────────

_PEM_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
    "ZWQyNTUxOQAAACBPQ29tZS9zb21lK3JhbmRvbS9sb29raW5nL2Jhc2U2NA==\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

#: name -> (prompt, a substring that must never appear anywhere on disk).
_MISSED_SHAPES = {
    "pem_ssh_private_key": (
        f"deploy with this key:\n{_PEM_KEY}", "BEGIN OPENSSH PRIVATE KEY"),
    # NOTE: this value is DELIBERATELY not a realistic live key. It keeps the
    # underscore-key prefix and the length that make it a credential-SHAPED
    # prompt --
    # which is all these two tests need, since neither depends on the shape
    # being detected -- while remaining unmistakably an example, so a secret
    # scanner does not flag the repository. The prefix is deliberately
    # ``sk_demo_`` and NOT ``sk_live_``/``sk_test_``: GitHub push protection
    # matches the real Stripe prefixes on shape alone and rejected two
    # earlier spellings of this fixture. What these tests need is an
    # underscore-bearing, allowlist-safe token of realistic length -- which
    # this is. Do not "improve" it back into something that looks real.
    "stripe_style_underscore_key": (
        "set STRIPE_KEY=sk_demo_EXAMPLEDONOTUSE00EXAMPLEDONOTUSE00 in the env",
        "sk_demo_EXAMPLE"),
    "google_api_key": (
        "the maps key is AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBW0 for the demo",
        "AIzaSyD-9tSrke72"),
    "aws_secret_access_key": (
        "AWS secret is wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY for the CLI",
        "wJalrXUtnFEMI"),
    "bare_hex_32": (
        "the checksum should equal 9e107d9d372bb6826bd81d3542a419d6 exactly",
        "9e107d9d372bb6826bd81d3542a419d6"),
    "shapeless_passphrase": (
        "the wifi password is correcthorsebatterystaple written on the wall",
        "correcthorsebatterystaple"),
}


@pytest.mark.parametrize("name,pair", sorted(_MISSED_SHAPES.items()))
def test_round_2s_approach_really_did_leak_this_shape(name, pair):
    """Grounding for the parametrized pin below: reconstruct round 2's
    expression and show the identifying substring survives whole. If a
    future change to the shipped detectors ever catches one of these, that
    is a welcome improvement to ``redact()`` — but it must not become the
    reason the NEXT test (which does not rely on detection at all) is
    trusted any less."""
    prompt, needle = pair
    leaked = _reconstruct_round2_slug(prompt)
    assert needle.replace(" ", "_") in leaked or needle in leaked, (name, leaked)


@pytest.mark.parametrize("name,pair", sorted(_MISSED_SHAPES.items()))
def test_a_credential_shape_round_2_missed_cannot_reach_the_slug_or_an_artifact_name(
        tmp_path, name, pair):
    """The real fix, proven per-shape: neither the folder slug NOR a copied
    artifact's basename carries so much as a fragment of the credential —
    not because this shape is now recognised, but because ``prompt`` (and a
    copied file's original name) are never read for either one.

    Round 4 correction: this test's name always claimed "or an artifact
    name", but ``_run_dir_names`` only lists ``Outbox/``'s IMMEDIATE
    children — the run FOLDER, never a file one level deeper inside it — so
    every parametrized case here was silently checking the slug twice and
    the artifact basename never. ``on_disk`` below now also globs INSIDE
    ``run_dir`` so the artifact-basename half of this pin's own name is
    actually exercised.
    """
    from systemu.runtime import outbox

    prompt, needle = pair
    art = tmp_path / f"{needle[:20]}.txt"
    art.write_text("artifact body", encoding="utf-8")

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=prompt, status="success",
        files_produced=[str(art)]))

    # the run FOLDER name (the slug) ...
    on_disk = _run_dir_names(tmp_path)
    # ... AND every file actually written INSIDE it (the artifact basename,
    # receipt.html, .done) — the gap this correction closes.
    inside_run_dir = {p.name for p in run_dir.iterdir()}
    assert inside_run_dir, "sanity: the run dir must not be empty"
    haystacks = [run_dir.name] + list(on_disk) + list(inside_run_dir)
    for h in haystacks:
        assert needle not in h, (name, h)
        assert needle.replace(" ", "_") not in h, (name, h)
    # and the artifact really was copied under the ordinal name, not skipped
    assert any(n.startswith("artifact-") for n in inside_run_dir), inside_run_dir


def test_a_pasted_PEM_key_cannot_reach_the_slug(tmp_path):
    """The shape named explicitly in DEC-34c, isolated as its own pin."""
    from systemu.runtime import outbox

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=f"deploy with this key:\n{_PEM_KEY}",
        status="success", files_produced=[]))
    assert "BEGIN" not in run_dir.name
    assert "OPENSSH" not in run_dir.name
    assert "PRIVATE" not in run_dir.name


def test_the_truncation_blind_JWT_still_cannot_reach_the_slug_end_to_end(tmp_path):
    """The load-bearing fixture from round 2's own suite, re-purposed: a JWT
    engineered to defeat detection ONCE ALREADY TRUNCATED to
    ``MAX_SLUG_CHARS``. Round 2 needed this fixture to distinguish "redact,
    then cap" from "cap, then never redact". The new design does not need to
    win that race at all — ``prompt`` never reaches the slug regardless of
    whether it is truncated, detected, or neither."""
    from systemu.runtime import outbox

    jwt = truncation_blind_secret_for("outbox_component", kind="jwt")
    cap = outbox.MAX_SLUG_CHARS
    assert len(jwt) > cap
    assert outbox.redact(jwt) == _OLD_MARKER  # sanity: detected whole

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=jwt, status="success", files_produced=[]))
    assert "eyJ" not in run_dir.name, run_dir.name
    assert jwt[:cap] not in run_dir.name, run_dir.name
    on_disk = _run_dir_names(tmp_path)
    assert not any("eyJ" in n for n in on_disk), on_disk


def test_the_secret_is_also_absent_from_the_FAILED_note_FILENAME(tmp_path):
    """``slug`` is reused for ``FAILED-<slug>.txt`` — a failed run must not
    leak the prompt through THAT filename either. The note's BODY still
    carries a (redacted) copy of the prompt — that is content, covered by
    AC-4 below, not this filename."""
    from systemu.runtime import outbox

    jwt = secret_for("outbox_component", kind="jwt")
    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=jwt, status="failure", files_produced=[]))

    notes = list(run_dir.glob("FAILED-*.txt"))
    assert len(notes) == 1, [p.name for p in run_dir.iterdir()]
    assert "eyJ" not in notes[0].name, notes[0].name


# ── AC-3 — both call sites, and the interface error, end to end ─────────────

def test_a_credential_shaped_ORIGINAL_ARTIFACT_NAME_cannot_reach_the_copy(
        tmp_path):
    """The second call site (AC-3): a task that saves its own output to a
    file named after a pasted credential must not have that name echoed
    into the Outbox — this is ``_copy_artifacts``, independent of the slug
    fix above (the prompt here is entirely ordinary)."""
    from systemu.runtime import outbox

    src_dir = tmp_path / "work"
    src_dir.mkdir()
    secret = "sk-liveKEY1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    evil = src_dir / f"{secret}.txt"
    evil.write_text("body", encoding="utf-8")

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt="an entirely ordinary prompt",
        status="success", files_produced=[str(evil)]))

    inside_run_dir = {p.name for p in run_dir.iterdir()}
    on_disk = _run_dir_names(tmp_path) | inside_run_dir
    assert not any(secret in n for n in on_disk), on_disk
    # round 4 (AC-1a): the STEM is still the bare ordinal, but a recognised
    # extension (".txt") now survives — see
    # test_a_recognised_extension_survives_end_to_end in
    # test_rutl1_intake_and_outbox.py for the dedicated per-shape pin.
    assert "artifact-1.txt" in inside_run_dir, inside_run_dir


def test_two_artifacts_from_credential_shaped_source_names_both_land_safely(
        tmp_path):
    """Ordinal naming composes with the existing collision-safety machinery:
    two differently-secret-named sources still both survive, distinctly."""
    from systemu.runtime import outbox

    d1, d2 = tmp_path / "one", tmp_path / "two"
    d1.mkdir(); d2.mkdir()
    (d1 / "AKIAABCDEFGHIJKLMNOP.csv").write_text("FIRST", encoding="utf-8")
    (d2 / "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345.csv").write_text(
        "SECOND", encoding="utf-8")

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt="p", status="success",
        files_produced=[str(d1 / "AKIAABCDEFGHIJKLMNOP.csv"),
                        str(d2 / "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345.csv")]))

    on_disk = {p.name for p in run_dir.iterdir()}
    assert not any("AKIA" in n or "ghp_" in n for n in on_disk), on_disk
    bodies = sorted((run_dir / n).read_text(encoding="utf-8")
                    for n in on_disk if n.startswith("artifact-"))
    assert bodies == ["FIRST", "SECOND"]


def test_safe_component_rejects_the_ORIGINAL_prompt_string_directly():
    """The interface-error pin, restated at the boundary this module owns:
    ``write_outbox`` cannot accidentally regress to feeding ``safe_component``
    a bare prompt string — the function itself refuses it."""
    from systemu.runtime.outbox import safe_component

    with pytest.raises(TypeError):
        safe_component("deploy with sk-liveKEY1234567890 to prod")


def test_both_safe_component_call_sites_pass_a_TrustedIdentity_by_source():
    """Static confirmation that the FIX is where the ACs require it: both
    ACTUAL CALLS to ``safe_component`` (excluding its own ``def`` line and
    its error-message string, which also contain the substring) pass a
    ``TrustedIdentity(...)``, and neither wraps ``prompt``/``src.name``
    directly. Matched with ``re.DOTALL`` because one call site wraps the
    ``TrustedIdentity(...)`` onto the following line.

    HONESTLY SCOPED: this is a narrow, source-grep guard against reverting to
    the OLD literal shape (``safe_component(prompt`` / ``safe_component(
    src.name``) — it CANNOT prove the argument handed to ``TrustedIdentity(``
    is actually trustworthy (``TrustedIdentity(src.name)`` would satisfy this
    check while reopening the exact leak AC-3 closes). The real guarantee is
    behavioral: ``test_a_credential_shaped_ORIGINAL_ARTIFACT_NAME_cannot_reach_the_copy``
    and ``test_two_artifacts_from_credential_shaped_source_names_both_land_safely``
    exercise a REAL credential-shaped source filename end-to-end and were
    verified (by mutation, manually) to fail if the artifact call site is
    changed to wrap ``src.name``. This test is a cheap tripwire, not a
    substitute for those.
    """
    import inspect
    import re

    from systemu.runtime import outbox
    src = inspect.getsource(outbox)

    calls = [
        m.start() for m in re.finditer(r"(?<!def )safe_component\(", src)
        if src[m.start():m.start() + len("safe_component()")] != "safe_component()"
    ]
    assert len(calls) == 2, calls
    for start in calls:
        # a narrow window right after the call — NOT the whole source, which
        # would also match this module's own explanatory docstrings.
        window = src[start:start + 80]
        assert re.search(r"safe_component\(\s*TrustedIdentity\(", window,
                         re.DOTALL), window
        # the narrow extra tripwire noted in the docstring above: catches the
        # ONE additional careless-edit shape a plain grep for a wrap can see.
        assert "TrustedIdentity(prompt" not in window, window
        assert "TrustedIdentity(src." not in window, window
    assert "safe_component(prompt" not in src
    assert "safe_component(src.name" not in src


# ── AC-4 — content surfaces are UNCHANGED: still redact, order preserved ────

def test_the_receipt_body_still_redacts_a_secret_shaped_prompt(tmp_path):
    """AC-4: the slug stopped depending on ``redact()`` — the RECEIPT did
    not. ``render_receipt`` still runs every interpolated value through
    ``_esc`` (redact then escape), unchanged by this fix."""
    from systemu.runtime import outbox

    jwt = secret_for("outbox_component", kind="jwt")
    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=jwt, status="success", files_produced=[]))
    html = (run_dir / "receipt.html").read_text(encoding="utf-8")
    assert "eyJ" not in html, html


def test_the_failure_note_BODY_still_redacts_a_secret_shaped_prompt(tmp_path):
    from systemu.runtime import outbox

    jwt = secret_for("outbox_component", kind="jwt")
    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt=jwt, status="failure", files_produced=[]))
    notes = list(run_dir.glob("FAILED-*.txt"))
    body = notes[0].read_text(encoding="utf-8")
    assert "eyJ" not in body, body


# ── DEC-34c ROUND 4 (AC-2) — the receipt's per-artifact identity must not ───
# collapse for CREDENTIAL-FREE paths ─────────────────────────────────────────
#
# A lens on the round-3 build (this file's AC-1/AC-3 pins above) found a bug
# ONE LAYER DOWN from the slug fix: ``_copy_artifacts`` correctly stopped
# writing ``src.name`` to DISK (AC-1b) but ``render_receipt`` still fed the
# WHOLE original absolute path to ``_esc`` (redact-then-escape) as ONE
# compound string. ``_value_is_secret`` treats the WHOLE value as a
# credential the instant ``mask_outbound`` changes ANYTHING anywhere inside
# it — this is round 2's OWN refutation shape ("credential-free inputs
# collapse to one constant"), reintroduced here via the RUN'S OWN DIRECTORY
# rather than the prompt: a path through an ordinary run directory named
# with a 40+ hex-character id (credential-free — it identifies a run, it is
# not a secret) collapses to the single constant, so every artifact copied
# from that run renders the SAME "copied from" line and becomes mutually
# indistinguishable AND individually unidentifiable, with no credential
# anywhere in the scenario. Fixed by ``outbox._esc_path``: the parent
# directory and the basename are redacted as two INDEPENDENT values, so a
# hex-shaped directory no longer drags an ordinary filename down with it.

#: A realistic run-directory id: 40 hex characters, credential-free — it
#: names nothing secret, it is exactly the shape a content hash or a run id
#: legitimately has (and exactly what triggers ``mask_outbound``'s
#: ``[A-Fa-f0-9]{40,}`` long-hex rule).
_RUN_ID_DIRNAME = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

#: 8 explicit, DISTINCT, entirely ordinary artifact basenames sharing that
#: one run directory — none of them shape-matches anything on its own
#: (grounded below). Chosen to mirror the packet's own refuted example
#: (Q3-report.pdf / revenue-chart.png / notes.docx / raw-data.csv) plus 4
#: more, covering distinct extensions.
_ONCE_COLLAPSING_ARTIFACT_NAMES = [
    "Q3-report.pdf",
    "revenue-chart.png",
    "notes.docx",
    "raw-data.csv",
    "meeting-notes.txt",
    "summary.md",
    "dataset.json",
    "presentation.pptx",
]


def _run_id_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / _RUN_ID_DIRNAME / name


def test_the_8_artifact_paths_really_did_collapse_under_a_whole_path_redact(
        tmp_path):
    """Grounding, not narration (mirrors
    ``test_the_8_prompts_really_did_collapse_under_round_2s_approach`` above,
    one surface down): ``outbox.redact()`` itself is UNCHANGED by the round-4
    fix — only WHAT ``render_receipt`` hands to it changed — so calling it
    directly on the WHOLE original path (exactly what round 3's
    ``_esc(original)`` did) reproduces the measured bug: all 8 distinct,
    ordinary artifact paths land on the exact same constant. If this ever
    stops being true (the shipped detectors changed) the fixture above needs
    refreshing; the fixed-behavior tests below do NOT depend on this staying
    true, only this one (and its per-item companion) does."""
    from systemu.runtime import outbox

    paths = [str(_run_id_path(tmp_path, n))
             for n in _ONCE_COLLAPSING_ARTIFACT_NAMES]
    redacted = {outbox.redact(p) for p in paths}
    assert redacted == {_OLD_MARKER}, redacted


@pytest.mark.parametrize("name", _ONCE_COLLAPSING_ARTIFACT_NAMES)
def test_each_of_the_8_artifact_paths_individually_collapses_under_redact_alone(
        tmp_path, name):
    """Per-fixture companion to the grounding test above (DEC-32: a
    universal-property assertion with no breaking fixtures is not a pin) —
    every one of the 8 is independently pinned by its own pytest id, not
    just their aggregate union."""
    from systemu.runtime import outbox

    assert outbox.redact(str(_run_id_path(tmp_path, name))) == _OLD_MARKER


def test_the_8_once_collapsing_artifacts_render_8_DISTINCT_EXPECTED_lines_in_the_real_receipt(
        tmp_path):
    """THE FIX, end to end (DEC-32: disk bytes to the operator-visible
    surface — real files, written through the real ``write_outbox``, the
    real ``receipt.html`` parsed back off disk, not a helper exercised in
    isolation). All 8 once-collapsing artifacts are copied in ONE run — the
    actual refuted scenario: several deliverables from the same run, sharing
    the same hex-shaped run directory — and every rendered "copied from"
    line is asserted against an EXPLICIT, DISTINCT expected value, not
    merely "8 different strings" (which a hash or a counter could satisfy by
    accident): each expected value is pinned to contain exactly its OWN
    basename and the shared directory's redacted form, in order."""
    import os as _os
    import re
    from systemu.runtime import outbox

    run_src_dir = tmp_path / _RUN_ID_DIRNAME
    run_src_dir.mkdir()
    srcs = []
    for n in _ONCE_COLLAPSING_ARTIFACT_NAMES:
        p = run_src_dir / n
        p.write_text(f"body of {n}", encoding="utf-8")
        srcs.append(str(p))

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt="an entirely ordinary prompt",
        status="success", files_produced=srcs))

    html = (run_dir / "receipt.html").read_text(encoding="utf-8")
    rows = re.findall(r"copied from ([^<]*)</span>", html)
    assert len(rows) == len(_ONCE_COLLAPSING_ARTIFACT_NAMES), html

    expected = [f"{_OLD_MARKER}{_os.sep}{n}"
                for n in _ONCE_COLLAPSING_ARTIFACT_NAMES]
    assert rows == expected, list(
        zip(_ONCE_COLLAPSING_ARTIFACT_NAMES, rows, expected))

    # restated as the direct AC-2 claim: all pairwise DISTINCT, and the raw
    # run id never appears anywhere in the receipt (only its redacted form)
    assert len(set(rows)) == len(rows), rows
    assert _RUN_ID_DIRNAME not in html, html

    # and the on-disk copies themselves are also distinct and typed (AC-1a)
    # — "artifact-1" carries NO suffix: DEC-34c round 7 removed ".pdf" from
    # _ARTIFACT_EXTENSIONS (a landed artifact's own /OpenAction JS
    # measurably executes when opened by this machine's registered
    # handler), so "Q3-report.pdf" is now an excluded, bare-copied type —
    # same as any other unrecognised suffix, unrelated to the redaction
    # property this test actually exercises.
    on_disk = sorted(p.name for p in run_dir.iterdir()
                     if p.name.startswith("artifact-"))
    assert on_disk == [
        "artifact-1", "artifact-2.png", "artifact-3.docx",
        "artifact-4.csv", "artifact-5.txt", "artifact-6.md",
        "artifact-7.json", "artifact-8.pptx",
    ], on_disk


def test_a_genuinely_credential_shaped_artifact_basename_still_collapses_on_its_own(
        tmp_path):
    """AC-2's fix must not WEAKEN the case round 3 already closed: an
    artifact whose OWN basename is credential-shaped must still render
    redacted — ``_esc_path`` splitting the path does not change that, since
    the basename component is still run through the exact same
    ``redact()``, independent of whatever the directory is."""
    import re
    from systemu.runtime import outbox

    src_dir = tmp_path / "ordinary-directory-name"
    src_dir.mkdir()
    secret = "sk-liveKEY1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    evil = src_dir / f"{secret}.txt"
    evil.write_text("body", encoding="utf-8")

    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id="t", prompt="p", status="success",
        files_produced=[str(evil)]))
    html = (run_dir / "receipt.html").read_text(encoding="utf-8")
    assert secret not in html, html
    rows = re.findall(r"copied from ([^<]*)</span>", html)
    assert len(rows) == 1
    # the ORDINARY directory survives (it is not secret-shaped); only the
    # credential-shaped basename collapses
    assert "ordinary-directory-name" in rows[0], rows[0]
    assert _OLD_MARKER in rows[0], rows[0]


# ── the residual: task_id is trusted BY CONTRACT, stated plainly ────────────

def test_task_id_is_used_as_is_and_is_not_itself_redacted(tmp_path):
    """Documented boundary, not a silent gap: DEC-34c trusts ``task_id`` BY
    CONTRACT (every real call site hands it an internally-generated
    timestamp/session id — see ``direct_task.py`` / ``quick_task.py`` — never
    raw user text), and does not run it through ``redact()``. This is the
    deliberate flip side of closing the false-positive collapse: nothing
    filters ``task_id``, so if that contract is ever violated by a future
    caller, this function will not catch it either. The fix for that would
    be to keep ``task_id`` non-attacker-influenced upstream — filtering it
    here would be exactly the anti-pattern DEC-34c removed for the prompt."""
    from systemu.runtime import outbox

    jwt = secret_for("outbox_component", kind="jwt")
    run_dir = Path(outbox.write_outbox(
        tmp_path, task_id=jwt, prompt="", status="success", files_produced=[]))
    # sanitized (allowlist charset, capped) but NOT secrecy-redacted: the
    # identifying prefix of the (hypothetical, contract-violating) task_id
    # survives, which is the honest, tested shape of the residual.
    assert "eyJ" in run_dir.name, run_dir.name
