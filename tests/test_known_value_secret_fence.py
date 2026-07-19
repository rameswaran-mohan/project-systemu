"""The SHAPELESS-SECRET gap in the outbound / promotion secret fences.

Both shipped fences are SHAPE fences (a name token list, and a value-shape detector).
Neither can recognise a secret that has no shape. These pins hold the reproduction, the
measured reason the shape rules were NOT widened, and the structural fix.
"""
import re
from pathlib import Path

import pytest

from systemu.messaging.gateway import mask_outbound
from systemu.runtime.credentials.known_values import (
    MIN_KNOWN_SECRET_LEN,
    contains_known_secret,
    redact_known_secrets,
)
from systemu.runtime.credentials.store import CredentialStore
from systemu.runtime.elicitation import _SECRET_NAME_TOKENS, is_secret_field
import systemu.runtime.ask_promotion as ap


class _Vault:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def vault(tmp_path):
    return _Vault(tmp_path / "v")


# ── 1. the gap, reproduced ────────────────────────────────────────────────────

SHAPELESS = ["hunter2", "correcthorsebatterystaple", "swordfish", "Tr0ub4dor&3",
             "deadbeef" * 4]


@pytest.mark.parametrize("secret", SHAPELESS)
def test_a_shapeless_secret_defeats_BOTH_shape_fences(secret):
    """The reproduction. Neither shipped fence sees any of these."""
    assert mask_outbound(secret) == secret, "the value fence unexpectedly fired"
    assert ap._value_is_secret(secret) is False, "the value fence unexpectedly fired"
    assert is_secret_field({"name": "service_endpoint"}) is False


def test_the_long_hex_rule_boundary_is_exactly_40():
    """Measured, not asserted: 39 hex chars pass, 40 are caught. This is the boundary
    the 40→32 proposal wanted to move — see the next test for why it was not moved."""
    for n in (31, 32, 39):
        h = ("deadbeef" * 10)[:n]
        assert mask_outbound(h) == h, "%d hex chars were caught" % n
        assert ap._value_is_secret(h) is False
    for n in (40, 41):
        h = ("deadbeef" * 10)[:n]
        assert mask_outbound(h) != h, "%d hex chars were NOT caught" % n
        assert ap._value_is_secret(h) is True


def test_the_long_hex_threshold_was_NOT_lowered_to_32():
    """A REGRESSION pin, not a description. ``external_verifier.mint_idempotency_key``
    returns ``secrets.token_hex(16)`` — exactly 32 hex chars — a deliberately
    NON-secret operational identifier on the money-move read-back path. Lowering the
    hex rule to 32 would redact the key the read-back is matched on, breaking the
    confirm it exists to make. It also doubled the measured false-positive rate on an
    ordinary-value corpus (2/41 → 4/41), newly flagging a dashless UUID and an MD5.

    If a future change lowers the threshold, this fails and points at the reason."""
    from systemu.runtime.external_verifier import mint_idempotency_key
    key = mint_idempotency_key()
    assert re.fullmatch(r"[0-9a-f]{32}", key), "the nonce shape changed; re-measure"
    assert mask_outbound(key) == key, (
        "a 32-hex idempotency key is now masked — the money-move read-back "
        "matches on this value and will break")


# ── 2. the structural fix ─────────────────────────────────────────────────────

@pytest.mark.parametrize("secret", ["hunter2xx", "correcthorsebatterystaple",
                                    "swordfish123", "deadbeef" * 4])
def test_a_stored_credential_VALUE_is_refused_however_shapeless(vault, secret):
    """The fix: identity, not resemblance."""
    CredentialStore(base_dir=vault.root).set("acme_login", secret)

    assert contains_known_secret(secret, vault) is True
    assert ap._value_is_secret(secret, vault) is True, "the promotion fence let it pass"
    assert mask_outbound(secret, vault) != secret, "the outbound mask let it pass"


def test_the_fence_is_inert_without_the_stored_value(vault):
    """The same string is ordinary until the operator actually stores it — proving the
    catch comes from the store and not from some accidental shape match."""
    assert ap._value_is_secret("hunter2xx", vault) is False
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    assert ap._value_is_secret("hunter2xx", vault) is True


def test_a_secret_EMBEDDED_in_prose_is_redacted_not_just_an_exact_match(vault):
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")

    out = mask_outbound("Connecting with hunter2xx now", vault)
    assert "hunter2xx" not in out
    assert "***" in out and out.startswith("Connecting with ")

    # …and behind a structural delimiter (query string / neutral param name).
    q = mask_outbound("GET /report?handle=hunter2xx&page=1", vault)
    assert "hunter2xx" not in q and "page=1" in q


def test_a_trailing_period_or_quote_does_not_defeat_the_match(vault):
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    for text in ('The value is hunter2xx.', 'Use "hunter2xx" here', "(hunter2xx)"):
        assert "hunter2xx" not in mask_outbound(text, vault), text


# ── 3. no false positives, by construction ────────────────────────────────────

ORDINARY = ["out/draft.md", "ops@acme.com", "acme-crm", "Asia/Kolkata",
            "https://api.acme.com/v1/reports", "production", "2026-07-20",
            "966eda5f", "v0.10.21", "Report_Q3_2026_Final", "InvoiceNo98765432",
            "parseHTTPResponse2xx", "550e8400-e29b-41d4-a716-446655440000"]


@pytest.mark.parametrize("value", ORDINARY)
def test_ordinary_values_are_untouched_even_with_a_populated_store(vault, value):
    """The negative control. A fence that flags ``production`` or a commit sha is a
    fence that gets disabled."""
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")

    assert contains_known_secret(value, vault) is False
    assert redact_known_secrets(value, vault) == value
    assert ap._value_is_secret(value, vault) is False


def test_the_shipped_prose_negative_control_still_holds(vault):
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    text = "Approve the deploy to staging? It touches 3 files."
    assert mask_outbound(text, vault) == text


def test_a_SHORT_credential_does_not_participate(vault):
    """A documented hole with a reason: a 4-char PIN would match inside ordinary prose
    and redact the output into uselessness. Short credentials keep the shape fences
    only. Pinned so the threshold cannot drift silently."""
    assert MIN_KNOWN_SECRET_LEN == 8
    CredentialStore(base_dir=vault.root).set("pin", "1234")
    assert contains_known_secret("1234", vault) is False
    assert mask_outbound("the code is 1234 ok", vault) == "the code is 1234 ok"


def test_a_credential_that_is_a_SUBSTRING_of_a_longer_token_does_not_redact_it(vault):
    """Matching is whole-token. ``hunter2xx`` stored must not blank out
    ``hunter2xxlarge-instance``, or one unlucky credential poisons ordinary output.

    BOTH entry points are pinned. A mutation that weakened only ``contains_known_
    secret`` to a raw substring scan survived the redaction-only version of this test
    — and that is the function the PROMOTION fence calls, so the surviving mutant
    silently refused ordinary answers and disabled the slice."""
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    text = "scale to hunter2xxlarge-instance today"

    assert mask_outbound(text, vault) == text
    assert contains_known_secret(text, vault) is False
    assert contains_known_secret("hunter2xxlarge-instance", vault) is False
    assert ap._value_is_secret("hunter2xxlarge-instance", vault) is False


# ── 4. END-TO-END through the real promotion path ─────────────────────────────

def test_a_stored_credential_is_refused_by_the_REAL_promotion_path(tmp_path):
    """The WIRING pin. Every test above calls ``_value_is_secret`` directly, so a
    mutation that simply stopped threading ``vault`` at the call site left all of them
    green while the fix was dead in production — it survived the first mutation round.
    This drives ``promote_answered_asks`` itself, through the real binder and the real
    snapshot producer, and checks the artifacts on disk.
    """
    from test_glearn_s3_promotion import (  # bare: no tests/__init__.py
        _Vault as S3Vault, _assert_realistic, _dctx, _real_snaps, _inventory_snaps)
    from systemu.runtime import user_profile as up

    SECRET = "correcthorsebatterystaple"
    v = S3Vault(tmp_path)
    CredentialStore(base_dir=v.root).set("acme_login", SECRET)

    snaps = _assert_realistic(
        _inventory_snaps(v, "service_endpoint", SECRET))

    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"service_endpoint": SECRET}) == 0, (
        "the operator's stored credential was PROMOTED — it will be read verbatim "
        "into a system prompt on every later run")
    assert not up.get_facts(v)

    # …and nothing anywhere under the vault carries the plaintext.
    for p in Path(str(v.root)).rglob("*"):
        if p.is_file():
            assert SECRET not in p.read_text(encoding="utf-8", errors="ignore"), (
                "the credential survived into %s" % p.name)


def test_the_REAL_promotion_path_still_promotes_an_ORDINARY_answer(tmp_path):
    """The negative half of the wiring pin: threading the vault must not turn the
    fence into a blanket refusal that silently disables the slice."""
    from test_glearn_s3_promotion import (  # bare: no tests/__init__.py
        _Vault as S3Vault, _assert_realistic, _dctx, _real_snaps)

    v = S3Vault(tmp_path)
    CredentialStore(base_dir=v.root).set("acme_login", "correcthorsebatterystaple")

    snaps = _assert_realistic(
        _real_snaps(v, {"service_endpoint": {"type": "string"}},
                    candidate_value="out/draft.md"))
    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"service_endpoint": "out/draft.md"}) == 1


# ── 5. the safety contract ────────────────────────────────────────────────────

def test_the_check_is_OPT_IN_so_existing_call_sites_keep_pure_behaviour(vault):
    """Without a vault the outbound mask stays pure/stateless and does no store IO."""
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    assert mask_outbound("Connecting with hunter2xx") == "Connecting with hunter2xx"
    assert ap._value_is_secret("hunter2xx") is False


def test_no_credential_VALUE_is_ever_logged(vault, caplog):
    """Compare, don't record. Nothing the fence emits may carry the value."""
    import logging
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    caplog.set_level(logging.DEBUG)

    contains_known_secret("Connecting with hunter2xx", vault)
    redact_known_secrets("Connecting with hunter2xx", vault)
    ap._value_is_secret("hunter2xx", vault)

    assert "hunter2xx" not in caplog.text


def test_the_corpus_holds_KEYED_DIGESTS_never_plaintext(vault):
    """The in-memory corpus must be unusable if dumped, and must use the EXISTING
    keyed helper (per-vault HMAC) rather than a new digest scheme."""
    from systemu.runtime.credentials.known_values import _corpus_digests
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")

    digests = _corpus_digests(vault)
    assert digests, "no corpus was built"
    for d in digests:
        assert "hunter2xx" not in d
        assert d.startswith("hmac256"), "not the existing keyed helper: %r" % d


def test_the_digest_is_scoped_to_ITS_vault(vault, tmp_path):
    """A digest signed by one vault's key must not match another's, or the corpus
    would be a cross-vault oracle."""
    from systemu.runtime.credentials.known_values import _corpus_digests
    other = _Vault(tmp_path / "other")
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    CredentialStore(base_dir=other.root).set("acme_login", "hunter2xx")

    assert not (_corpus_digests(vault) & _corpus_digests(other))


def test_the_outbound_mask_fails_OPEN_and_the_promotion_fence_fails_CLOSED(vault,
                                                                          monkeypatch):
    """The deliberate asymmetry. A broken corpus must not break a push, but it must
    not let a credential be persisted either."""
    CredentialStore(base_dir=vault.root).set("acme_login", "hunter2xx")
    import systemu.runtime.credentials.known_values as kv

    monkeypatch.setattr(kv, "_corpus_digests",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mask_outbound("Deploy done.", vault) == "Deploy done."

    import systemu.messaging.gateway as gw
    monkeypatch.delattr(gw, "mask_outbound")
    assert ap._value_is_secret("anything at all", vault) is True


def test_the_name_token_list_stayed_APPEND_ONLY():
    """``_SECRET_NAME_TOKENS`` is imported by ``messaging.gateway`` and by
    ``replay_metrics._is_secret_path``; removing a token silently widens what gets
    written into a plaintext append-only audit corpus."""
    for tok in ("password", "passwd", "secret", "token", "api_key", "apikey",
                "access_key", "private_key", "client_secret", "credential", "auth",
                "card", "cvv", "ssn", "pin"):
        assert tok in _SECRET_NAME_TOKENS, "token %r was REMOVED" % tok
