"""ENV-PROVISIONED credentials in the known-value fence, found by DECLARED KEY, made
fail-closed-for-real by a WITNESSED-completeness roster reader (DEC-27).

THE BYPASS. ``CredentialResolver.resolve`` falls through keyring → ``os.environ``, so a
credential provisioned the ``.env`` way resolves normally (source ``"env"``) while
``CredentialStore.list_names()`` — a registry maintained only by ``CredentialStore.set``
— never learns it exists. The corpus measured EMPTY with the credential live, so
``ask_promotion._value_is_secret`` returned ``False`` and the value was free to be
promoted into a durable fact read verbatim into a system prompt on every later run. The
fix ENUMERATES the declaration (``Tool.requires_credentials``) instead of pattern-matching
names — no pattern over ``elicitation._SECRET_NAME_TOKENS`` even catches a bare
``<VENDOR>_KEY``.

THE COMPLETENESS BUG THIS PACKET CLOSES. Enumerating the declaration means reading the
tool roster, and ``Vault.load_index`` reads through ``_read_json``, which SWALLOWS
``JSONDecodeError``/``OSError`` and returns ``[]``. So a truncated index, a ``tools`` that
is a FILE, an index that is a dict, and — the layer three prior fixes never reproduced —
a VANISHED vault root all arrived as the same empty list as a vault that genuinely
registers no tools, and the fail-closed fence emitted its HEALTHY "no secrets" answer from
a failure. The fix is ``Vault.load_tool_index_strict``: it cannot represent "unreadable"
as ``[]`` — it ``os.stat``s the root (a missing root RAISES), ``os.scandir``s the tools
dir (a genuinely absent dir, parent already verified, is the ONE branch that returns
``[]``), then reads/parses/shape-checks and RAISES ``VaultUnreadable`` at whichever stage
fails. ``Path.exists()`` appears NOWHERE — it swallows ENOENT/ENOTDIR itself, which is
exactly how a vanished root read as present-and-empty.

THE EXIT CRITERION (DEC-27). The acceptance tests target the INVARIANT — "a live env
credential is never promoted while the roster is unreadable" — uniformly across EVERY
failure mode. None of them asks WHICH layer failed: a fence that had to would be inferring
completeness rather than witnessing it. Only the reader's own UNIT tests name a stage.
"""
import importlib
import logging
import shutil
from pathlib import Path

import pytest

import systemu.runtime.ask_promotion as ap
from systemu.core.models import CredentialRequirement, Tool, ToolType
from systemu.messaging.gateway import mask_outbound
from systemu.runtime.credentials.known_values import (
    MIN_KNOWN_SECRET_LEN,
    KnownSecret,
    _build_corpus,
    _declared_credential_keys,
    _status_with_reason,
    known_secret_status,
    redact_known_secrets,
)
from systemu.runtime.elicitation import _SECRET_NAME_TOKENS
from systemu.storage.file_vault import FileVault
from systemu.vault.vault import Vault, VaultUnreadable

#: A SHAPELESS value: no shipped shape rule can see it (pinned below). Using one keeps
#: these tests honest — a shape-detectable value would pass even with the fence removed.
SECRET = "correcthorsebatterystaple"

#: The name shape the token match MISSES. Bare ``_KEY``, no secret-ish token anywhere.
BARE_KEY = "SENDGRID_KEY"


def _vault(tmp_path, *, keys=(BARE_KEY,), auth_type="api_key"):
    """A REAL Vault with a REAL Tool declaring ``keys``. Never a shim."""
    v = Vault(root=tmp_path / "v")
    v.save_tool(Tool(
        id="t_send", name="send_mail", description="d", tool_type=ToolType.API_CALL,
        requires_credentials=[CredentialRequirement(key=k, label="L", auth_type=auth_type)
                              for k in keys]))
    return v


def _keys(vault):
    """The declared key set for a HEALTHY vault, asserting it reported itself COMPLETE
    (and gave no failure reason). Folding the ``complete``/``reason`` assertion in is
    strictly stronger than comparing key sets alone: a regression that reports INCOMPLETE
    on a healthy vault can no longer hide behind a key-set comparison that still passes."""
    keys, complete, reason = _declared_credential_keys(vault)
    assert complete is True, "a healthy vault reported an INCOMPLETE enumeration"
    assert reason is None, "a healthy vault reported a failure reason: %r" % reason
    return keys


# ── the roster failures the fence must survive ────────────────────────────────
# Each is a REAL on-disk break, applied the way a real vault gets corrupted — NONE
# monkeypatches ``load_index`` (which cannot raise on the file backend, so patching it to
# raise exercises a branch no corruption reaches and proves nothing). The battery includes
# the 4th layer three prior fixes never reproduced: a VANISHED ROOT.

def _c_truncated(v):
    (Path(v.root) / "tools" / "index.json").write_text('[{"id": "t_s', encoding="utf-8")

def _c_index_is_directory(v):
    p = Path(v.root) / "tools" / "index.json"
    p.unlink()
    p.mkdir()

def _c_index_is_a_dict(v):
    (Path(v.root) / "tools" / "index.json").write_text(
        '{"t_send": {"id": "t_send"}}', encoding="utf-8")

def _c_bare_scalar(v):
    (Path(v.root) / "tools" / "index.json").write_text("7", encoding="utf-8")

def _c_list_of_strings(v):
    (Path(v.root) / "tools" / "index.json").write_text('["t_send"]', encoding="utf-8")

def _c_list_of_idless_dicts(v):
    (Path(v.root) / "tools" / "index.json").write_text('[{"name": "send"}]', encoding="utf-8")

def _c_list_with_null(v):
    (Path(v.root) / "tools" / "index.json").write_text('[null]', encoding="utf-8")

def _c_list_of_lists(v):
    (Path(v.root) / "tools" / "index.json").write_text('[["t_send"]]', encoding="utf-8")

def _c_tools_is_a_file(v):
    shutil.rmtree(Path(v.root) / "tools")
    (Path(v.root) / "tools").write_text("x", encoding="utf-8")

def _c_root_gone(v):
    shutil.rmtree(Path(v.root))

#: name → (break, expected reader stage). The stage is used ONLY by the reader's own
#: unit test; the fence-level tests below never consult it.
ROSTER_FAILURES = {
    "truncated_json":               (_c_truncated,            "parse"),
    "index_is_a_directory":         (_c_index_is_directory,   "read"),
    "index_is_a_dict":              (_c_index_is_a_dict,      "shape"),
    "index_is_a_bare_scalar":       (_c_bare_scalar,          "shape"),
    "list_of_bare_strings":         (_c_list_of_strings,      "shape"),
    "list_of_idless_dicts":         (_c_list_of_idless_dicts, "shape"),
    "list_with_a_null":             (_c_list_with_null,       "shape"),
    "list_of_lists":                (_c_list_of_lists,        "shape"),
    "tools_dir_replaced_by_a_file": (_c_tools_is_a_file,      "scan"),
    "vault_root_missing_entirely":  (_c_root_gone,            "root"),  # THE 4th layer
}


def _break(v, how):
    ROSTER_FAILURES[how][0](v)


# ── 1. the bypass and its invisibility to the shape fences ────────────────────

BARE_NAMES = [
    "SENDGRID_KEY", "STRIPE_KEY", "OPENAI_KEY", "SHODAN_KEY", "ALPHAVANTAGE_KEY",
    "GEMINI_KEY", "GOOGLE_MAPS_KEY", "SUPABASE_ANON_KEY", "WEATHER_KEY", "OWM_KEY",
    "X_KEY", "K_KEY",
]


@pytest.mark.parametrize("name", BARE_NAMES)
def test_a_bare_KEY_name_matches_NO_secret_name_token(name):
    """No pattern over ``_SECRET_NAME_TOKENS`` sees these — which is why a NAME rule
    cannot fence them and the DECLARATION must be enumerated instead."""
    low = name.lower()
    assert not any(tok in low for tok in _SECRET_NAME_TOKENS), name


def test_the_value_is_invisible_to_BOTH_shape_fences():
    """If the shape rules saw ``SECRET`` these pins would pass with the fence removed."""
    assert mask_outbound(SECRET) == SECRET
    assert ap._value_is_secret(SECRET) is False  # no vault → shape rules only


# ── 2. THE INVARIANT — driven through the REAL promoter, uniform over EVERY failure ──

@pytest.mark.parametrize("how", sorted(ROSTER_FAILURES))
def test_a_live_env_credential_is_NEVER_promoted_when_the_roster_is_unreadable(
        tmp_path, monkeypatch, how):
    """THE exit criterion. A credential live in ``os.environ`` under a DECLARED key, and
    the tool roster broken every way it can break — truncation, a directory, a dict, a
    bare scalar, four shapes of non-header list, a ``tools`` FILE, and a VANISHED ROOT.
    ONE assertion for all of them: the promoter writes nothing. The test never asks which
    layer failed — that uniformity is the point.

    Driven through ``promote_answered_asks`` itself (the real binder, the real snapshot
    producer) with a SHAPELESS value, so a refusal can only come from the could-not-build
    path, not a shape hit. Snapshots are built while the roster is still readable — the
    binder reads tools."""
    from test_glearn_s3_promotion import (  # bare: no tests/__init__.py
        _assert_realistic, _dctx, _inventory_snaps)
    from systemu.runtime import user_profile as up

    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    snaps = _assert_realistic(_inventory_snaps(v, "service_endpoint", SECRET))
    _break(v, how)

    assert ap.promote_answered_asks(v, _dctx(snaps), {"service_endpoint": SECRET}) == 0, (
        "a live env credential was PROMOTED while the roster was unreadable")
    assert not up.get_facts(v)
    root = Path(str(v.root))
    if root.exists():   # a vanished root has no files to scan — the refusal already held
        for p in root.rglob("*"):
            if p.is_file():
                assert SECRET not in p.read_text(encoding="utf-8", errors="ignore"), (
                    "the credential survived into %s" % p.name)


@pytest.mark.parametrize("how", sorted(ROSTER_FAILURES))
def test_an_unreadable_roster_is_UNKNOWN_and_the_fence_fails_CLOSED(
        tmp_path, monkeypatch, how):
    """The tri-state, at fence level, uniform over every failure: the enumeration reports
    INCOMPLETE (never raises), the tri-state is UNKNOWN (never NO_MATCH — the healthy
    answer is unreachable from a failure), and ``_value_is_secret`` refuses."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    _break(v, how)

    keys, complete, reason = _declared_credential_keys(v)   # must NOT raise
    assert complete is False
    assert reason  # a diagnostic was produced
    assert known_secret_status(SECRET, v) is KnownSecret.UNKNOWN
    assert ap._value_is_secret(SECRET, v) is True


@pytest.mark.parametrize("how", sorted(ROSTER_FAILURES))
def test_the_outbound_mask_still_fails_OPEN_when_the_roster_is_unreadable(
        tmp_path, monkeypatch, how):
    """The asymmetry the fix must PRESERVE: the same could-not-build condition that
    refuses a promotion leaves the outbound mask passing text through — masking must never
    break a push. Only the SHAPE half runs (fail open = status quo)."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    _break(v, how)

    txt = "connect with %s now" % SECRET
    assert redact_known_secrets(txt, v) == txt          # the shapeless known value egresses
    assert mask_outbound(txt, v) == txt
    assert "Bearer abcdefghijkl" not in mask_outbound("Bearer abcdefghijkl", v)  # shape untouched


# ── 3. the ALLOW side — a legitimately empty vault must not be fenced shut ─────

def test_a_verified_root_with_NO_tools_dir_is_a_COMPLETE_empty_answer(tmp_path, monkeypatch):
    """The other half of the witness. A vault whose root is verified but has no ``tools``
    dir is a real, COMPLETE "registers no tools" answer (``FileNotFoundError`` from
    ``os.scandir`` on a child of a positively-stat'd parent) — the ONLY absence-means-empty
    branch. It is NOT fenced shut: an ordinary value is NO_MATCH, not UNKNOWN, even with a
    credential live in the environment (nothing declares it, so it is not harvested)."""
    v = _vault(tmp_path, keys=())
    shutil.rmtree(Path(v.root) / "tools")
    monkeypatch.setenv(BARE_KEY, SECRET)

    digests, complete, reason = _build_corpus(v)
    assert not digests and complete is True and reason is None
    assert known_secret_status(SECRET, v) is KnownSecret.NO_MATCH
    assert ap._value_is_secret("out/draft.md", v) is False


def test_the_REAL_promoter_still_promotes_an_ORDINARY_answer(tmp_path, monkeypatch):
    """The negative half of the invariant: fencing must not become a blanket refusal that
    silently disables the slice. A healthy vault still promotes an ordinary answer."""
    from test_glearn_s3_promotion import (  # bare: no tests/__init__.py
        _assert_realistic, _dctx, _real_snaps)

    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    snaps = _assert_realistic(
        _real_snaps(v, {"service_endpoint": {"type": "string"}},
                    candidate_value="out/draft.md"))
    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"service_endpoint": "out/draft.md"}) == 1


def test_a_COMPLETE_empty_corpus_is_NO_MATCH_not_UNKNOWN(tmp_path):
    """A vault that genuinely holds no credential (nothing declared, nothing in env,
    nothing stored) builds a corpus that is empty AND COMPLETE — only a BUILD FAILURE
    yields UNKNOWN. This is the distinction the whole fix rests on."""
    v = _vault(tmp_path, keys=())
    digests, complete, reason = _build_corpus(v)
    assert not digests and complete is True and reason is None
    assert known_secret_status("out/draft.md", v) is KnownSecret.NO_MATCH


# ── 4. the env-declared refusal, and the declared-key enumeration ─────────────

def test_an_env_provisioned_declared_credential_is_REFUSED(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    assert BARE_KEY in _keys(v)
    assert known_secret_status(SECRET, v) is KnownSecret.MATCH
    assert ap._value_is_secret(SECRET, v) is True


def test_the_same_env_credential_is_INVISIBLE_when_no_tool_declares_the_key(
        tmp_path, monkeypatch):
    """The deliberate boundary: a value in ``os.environ`` under a name NO tool declares is
    NOT harvested — and the corpus is COMPLETE about it, so the answer is NO_MATCH, not a
    fail-closed UNKNOWN."""
    v = _vault(tmp_path, keys=("OTHER_KEY",))
    monkeypatch.setenv(BARE_KEY, SECRET)
    assert BARE_KEY not in _keys(v)
    assert known_secret_status(SECRET, v) is KnownSecret.NO_MATCH
    assert ap._value_is_secret(SECRET, v) is False


def test_the_index_header_omits_requires_credentials_so_the_RECORD_is_read(tmp_path):
    """An ANCHOR pin. ``vault._tool_header`` omits ``requires_credentials``, so reaching
    for ``load_tool_index_strict`` alone would yield an EMPTY key set — the full record has
    to be read per tool. If a future header carries the field this breaks loudly."""
    v = _vault(tmp_path)
    headers = v.load_tool_index_strict()
    assert headers and all("requires_credentials" not in h for h in headers)
    assert [r.key for r in v.get_tool(headers[0]["id"]).requires_credentials] == [BARE_KEY]


def test_auth_type_none_declares_no_credential(tmp_path):
    """``resolver.resolve`` short-circuits ``auth_type == "none"`` and never reads the
    environment for it, so its name denotes no credential here."""
    v = _vault(tmp_path, auth_type="none")
    assert _keys(v) == set()


def test_an_UNDECLARED_environment_variable_is_NEVER_harvested(tmp_path, monkeypatch):
    v = _vault(tmp_path, keys=())
    monkeypatch.setenv("TOTALLY_UNDECLARED_XYZ", SECRET)
    assert known_secret_status(SECRET, v) is KnownSecret.NO_MATCH
    assert ap._value_is_secret(SECRET, v) is False


def test_a_declared_credential_SHORTER_than_the_floor_does_not_participate(
        tmp_path, monkeypatch):
    v = _vault(tmp_path)
    short = "abc123x"
    assert len(short) < MIN_KNOWN_SECRET_LEN
    monkeypatch.setenv(BARE_KEY, short)
    assert known_secret_status(short, v) is KnownSecret.NO_MATCH


# ── 5. completeness is WITNESSED — the incomplete paths, and the witnessed-empty ones ──

def test_an_UNREADABLE_tool_RECORD_makes_the_corpus_INCOMPLETE(tmp_path, monkeypatch):
    """The roster lists a tool but its record cannot be read — that tool may have declared
    the key holding the value, so the header lands in ``unresolved`` (never a bare
    ``continue``) and the enumeration is incomplete. Driven by making the REAL ``get_tool``
    raise, not by monkeypatching the fence."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    monkeypatch.setattr(v, "get_tool",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("record unreadable")))
    keys, complete, reason = _declared_credential_keys(v)
    assert complete is False and "unreadable" in reason
    assert known_secret_status(SECRET, v) is KnownSecret.UNKNOWN
    assert ap._value_is_secret(SECRET, v) is True


def test_a_header_with_no_tool_id_is_collected_not_silently_skipped(tmp_path):
    """The 'never a bare continue' guard. A header the roster reader yields with no
    ``id`` makes the enumeration incomplete — it might have named the key holding the
    value. Driven with a LAX reader double, since every shipped strict reader already
    guarantees a string id (so this is defence in depth, pinned rather than assumed)."""
    class _LaxVault:
        def __init__(self, root):
            self.root = root
        def load_index(self, entity):
            return []
        def load_tool_index_strict(self):
            return [{"name": "no id here"}]
        def get_tool(self, tid):  # pragma: no cover - must not be reached
            raise AssertionError("get_tool called on an unresolved header")

    keys, complete, reason = _declared_credential_keys(_LaxVault(tmp_path))
    assert keys == set() and complete is False and "unreadable" in reason


def test_an_unavailable_per_vault_HMAC_makes_the_corpus_INCOMPLETE(tmp_path, monkeypatch):
    """The OTHER corpus source's incompleteness signal: the per-vault HMAC underpins every
    digest, and ``value_ref`` returns None when no key can be derived (its own fail-closed
    signal). Drive the REAL upstream condition — an empty per-vault secret makes
    ``_ref_key`` raise — and confirm a participating value that cannot be digested is
    could-not-build, not a silently empty corpus."""
    import systemu.runtime.replay_metrics as rm
    import systemu.runtime.dashboard_auth as da

    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    rm._REF_KEY_CACHE.clear()
    monkeypatch.setattr(da, "session_secret", lambda *a, **k: "")
    try:
        digests, complete, reason = _build_corpus(v)
        assert not digests, "a value was digested without a key"
        assert complete is False and reason
        assert known_secret_status(SECRET, v) is KnownSecret.UNKNOWN
        assert ap._value_is_secret(SECRET, v) is True
    finally:
        rm._REF_KEY_CACHE.clear()


def test_a_store_only_shim_with_NO_tool_surface_is_WITNESSED_complete(tmp_path):
    """A vault exposing no ``load_index`` at all declares no credentials THROUGH tools — a
    real, COMPLETE empty answer whose witness is the ABSENCE of the tool API (nothing was
    swallowed to reach it). It is NOT fenced shut."""
    class _ShimVault:
        def __init__(self, root):
            self.root = root
    v = _ShimVault(tmp_path / "s")
    Path(v.root).mkdir(parents=True)
    keys, complete, reason = _declared_credential_keys(v)
    assert keys == set() and complete is True and reason is None
    assert known_secret_status("out/draft.md", v) is KnownSecret.NO_MATCH


def test_a_vault_WITH_load_index_but_no_strict_reader_is_UNVERIFIABLE(tmp_path):
    """A vault that HAS a tool surface but no strict reader cannot have its roster
    witnessed. Answering "complete" there would reinstate the collapse for any backend not
    taught the strict reader, so it is reported INCOMPLETE."""
    class _NoStrict:
        def __init__(self, root):
            self.root = root
        def load_index(self, entity):
            return []
    keys, complete, reason = _declared_credential_keys(_NoStrict(tmp_path))
    assert complete is False and reason == "tool roster reader unavailable"


def test_a_vault_whose_ATTRIBUTE_ACCESS_raises_is_INCOMPLETE_not_empty(tmp_path):
    """Even the attribute lookup is guarded: a lazily-connecting proxy whose ``load_index``
    is a property raising something other than AttributeError must not propagate out of the
    fence — it is INCOMPLETE."""
    class _Hostile:
        root = tmp_path
        @property
        def load_index(self):
            raise RuntimeError("lazy connect failed")
    keys, complete, reason = _declared_credential_keys(_Hostile())
    assert complete is False and reason


# ── 6. the reader's own contract — the ONE place a stage is named ─────────────

@pytest.mark.parametrize("how", sorted(ROSTER_FAILURES))
def test_the_strict_reader_RAISES_with_the_right_stage(tmp_path, how):
    """The source-level guarantee, asserted at the source. This UNIT test — of the
    reader's own contract — is the one place a stage is named; the fence-level tests above
    never do. ``Path.exists()`` appears NOWHERE in the reader, which is why a vanished ROOT
    raises ``stage="root"`` instead of reading its child as absent-and-empty."""
    break_fn, stage = ROSTER_FAILURES[how]
    v = _vault(tmp_path / how)
    assert v.load_tool_index_strict(), "healthy vault read empty — the pin is vacuous"
    break_fn(v)
    with pytest.raises(VaultUnreadable) as ei:
        v.load_tool_index_strict()
    assert ei.value.stage == stage
    assert ei.value.path is not None


def test_a_missing_root_is_NOT_an_empty_vault_which_is_why_exists_is_wrong(tmp_path):
    """The 4th layer, and the reason ``Path.exists()`` is banned from the reader: with the
    root gone, ``root/tools/index.json``.exists() returns False — a verified absence a
    naive strict reader would read as 'complete, empty' and ALLOW. The witnessed reader
    stat's the root first and RAISES."""
    v = _vault(tmp_path)
    idx = Path(v.root) / "tools" / "index.json"
    shutil.rmtree(Path(v.root))
    assert idx.exists() is False                    # what a .exists() reader would trust
    with pytest.raises(VaultUnreadable) as ei:
        v.load_tool_index_strict()
    assert ei.value.stage == "root"                 # but a missing root is never empty


def test_a_genuinely_absent_tools_dir_returns_empty_and_a_fresh_vault_scaffolds_one(
        tmp_path):
    """The absent-means-empty branch is legitimate ONLY because the parent root was
    positively stat-verified first. And a fresh Vault scaffolds ``tools/index.json`` as
    ``[]``, so the genuinely-empty answer comes back through the normal parse — no special
    case to collapse."""
    v = _vault(tmp_path)
    shutil.rmtree(Path(v.root) / "tools")
    assert v.load_tool_index_strict() == []

    fresh = Vault(root=tmp_path / "fresh")
    idx = Path(fresh.root) / "tools" / "index.json"
    assert idx.exists() and idx.read_text(encoding="utf-8").strip() == "[]"
    assert fresh.load_tool_index_strict() == []


def test_load_tool_index_strict_leaves_the_swallowing_reader_ALONE(tmp_path):
    """The strict path is ADDITIVE. ``load_index`` still swallows a corrupt index to ``[]``
    (its sixteen callers depend on that), and only the strict twin raises."""
    v = _vault(tmp_path)
    _c_truncated(v)
    assert v.load_index("tools") == []      # unchanged, still swallows
    assert v.list_tools() == []             # and its callers still work
    with pytest.raises(VaultUnreadable):
        v.load_tool_index_strict()


def test_load_index_and_the_strict_reader_share_one_layout_constant(tmp_path):
    """The strict reader must NOT re-derive the path — a fence-private copy that drifts
    reads a stale path, sees a verified-absent file, and concludes complete+empty. Both
    readers key off ``Vault._INDEX_FILES``; pin that the strict reader's target IS what
    that constant names, so a layout change cannot silently strand one of them."""
    v = _vault(tmp_path)
    assert Vault._INDEX_FILES["tools"] == "tools/index.json"
    # break exactly the file that constant names; the strict reader must notice.
    (Path(v.root) / Vault._INDEX_FILES["tools"]).write_text("7", encoding="utf-8")
    with pytest.raises(VaultUnreadable):
        v.load_tool_index_strict()


# ── 7. every shipped backend, and the concrete facade AppState holds ──────────

@pytest.mark.parametrize("cls_path", [
    "systemu.vault.vault:Vault",
    "systemu.storage.file_vault:FileVault",
    "systemu.storage.parallel_vault:ParallelVault",
    "systemu.storage.sqlite.vault:SqliteVault",
])
def test_every_shipped_vault_implements_the_strict_roster_reader(cls_path):
    """A vault missing the strict reader would silently fail CLOSED on every promotion
    (the fence treats it as unverifiable) — safe, but a bricked slice. Every shipped
    backend must implement it."""
    mod, name = cls_path.split(":")
    cls = getattr(importlib.import_module(mod), name)
    assert callable(getattr(cls, "load_tool_index_strict", None)), cls_path


def test_the_enumeration_works_through_the_concrete_FileVault(tmp_path, monkeypatch):
    """The vault ``AppState`` actually holds is a ``FileVault`` facade — pin that it
    forwards the strict reader, both healthy and broken, end to end."""
    inner = _vault(tmp_path)
    fv = FileVault(inner)
    monkeypatch.setenv(BARE_KEY, SECRET)

    assert BARE_KEY in _keys(fv)
    assert ap._value_is_secret(SECRET, fv) is True

    _c_truncated(inner)
    assert known_secret_status(SECRET, fv) is KnownSecret.UNKNOWN
    assert ap._value_is_secret(SECRET, fv) is True


def test_the_sqlite_backend_reads_its_roster_strictly(tmp_path, monkeypatch):
    """On the SQL backend the completeness witness is the query itself (``load_index``
    propagates rather than swallowing). A healthy roster enumerates the declared key; the
    reader returns a shape-checked list of header dicts."""
    from systemu.storage.sqlite.vault import SqliteVault
    v = SqliteVault(f"sqlite:///{tmp_path / 'v.db'}")
    v.save_tool(Tool(
        id="t_send", name="send_mail", description="d", tool_type=ToolType.API_CALL,
        requires_credentials=[CredentialRequirement(key=BARE_KEY, label="L")]))
    rows = v.load_tool_index_strict()
    assert isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows)
    monkeypatch.setenv(BARE_KEY, SECRET)
    assert BARE_KEY in _keys(v)
    assert ap._value_is_secret(SECRET, v) is True


# ── 8. the "compare, don't record" contract, and the operator-visible diagnostic ──

def test_the_UNKNOWN_refusal_logs_the_stage_and_path_operator_visibly(
        tmp_path, monkeypatch, caplog):
    """DEC-27: an incomplete-corpus refusal must be operator-visible WITH the failure's
    stage/path, so a silent over-refusal is diagnosable. The path is the vault's own
    layout — never a value or key."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    _c_truncated(v)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert ap._value_is_secret(SECRET, v) is True
    warns = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING and "INCOMPLETE" in r.getMessage()]
    assert len(warns) == 1, warns
    assert "stage=parse" in warns[0] and "index.json" in warns[0]
    assert SECRET not in warns[0] and BARE_KEY not in warns[0]


def test_the_promoter_capped_line_stays_GENERIC_never_the_value_or_stage(
        tmp_path, monkeypatch, caplog):
    """The promoter's INFO refusal list must not distinguish a could-not-build refusal
    from a shape or known-value hit, and must never carry the value or the stage — "this
    string is a credential / the roster failed at parse" are both facts about a value the
    fence just refused to record. The stage/path rides the SEPARATE WARNING above."""
    from test_glearn_s3_promotion import (  # bare: no tests/__init__.py
        _assert_realistic, _dctx, _inventory_snaps)

    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    snaps = _assert_realistic(_inventory_snaps(v, "service_endpoint", SECRET))
    _c_truncated(v)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert ap.promote_answered_asks(v, _dctx(snaps), {"service_endpoint": SECRET}) == 0
    capped = [r.getMessage() for r in caplog.records if "capped/refused" in r.getMessage()]
    assert len(capped) == 1, capped
    assert "(answer looks like a credential)" in capped[0]
    assert SECRET not in capped[0]
    assert "stage=" not in capped[0]


def test_no_credential_VALUE_or_KEY_NAME_is_ever_logged(tmp_path, monkeypatch, caplog):
    """Compare, don't record — including on the failure paths this packet adds."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    caplog.set_level(logging.DEBUG)

    known_secret_status(SECRET, v)
    redact_known_secrets("connect with %s now" % SECRET, v)
    ap._value_is_secret(SECRET, v)
    _c_truncated(v)                    # exercise the incomplete / warning path too
    known_secret_status(SECRET, v)
    ap._value_is_secret(SECRET, v)

    assert SECRET not in caplog.text
    assert BARE_KEY not in caplog.text


def test_status_with_reason_gives_None_on_a_definite_answer(tmp_path, monkeypatch):
    """``reason`` is diagnostic-only and non-None ONLY for UNKNOWN — a MATCH or NO_MATCH is
    a definite answer and carries no failure story."""
    v = _vault(tmp_path)
    monkeypatch.setenv(BARE_KEY, SECRET)
    assert _status_with_reason(SECRET, v) == (KnownSecret.MATCH, None)
    assert _status_with_reason("out/draft.md", v) == (KnownSecret.NO_MATCH, None)
    _c_truncated(v)
    status, reason = _status_with_reason(SECRET, v)
    assert status is KnownSecret.UNKNOWN and reason


# ── 9. the regressions this packet must not touch ─────────────────────────────

def test_the_long_hex_threshold_stayed_40(tmp_path):
    """The shape rule this fence exists ALONGSIDE, not instead of. 39 hex pass, 40 caught;
    lowering it would redact ``mint_idempotency_key``'s 32-hex nonce on the money-move
    read-back path (measured, rejected)."""
    for n in (32, 39):
        h = ("deadbeef" * 10)[:n]
        assert mask_outbound(h) == h
    for n in (40, 41):
        h = ("deadbeef" * 10)[:n]
        assert mask_outbound(h) != h


def test_the_name_token_list_stayed_APPEND_ONLY():
    """``_SECRET_NAME_TOKENS`` is imported by ``messaging.gateway`` and
    ``replay_metrics._is_secret_path``; removing a token silently widens what is written
    into a plaintext append-only audit corpus."""
    for tok in ("password", "passwd", "secret", "token", "api_key", "apikey",
                "access_key", "private_key", "client_secret", "credential", "auth",
                "card", "cvv", "ssn", "pin"):
        assert tok in _SECRET_NAME_TOKENS, "token %r was REMOVED" % tok
