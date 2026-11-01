"""The MCP OAuth handoff card shows a URL's host, never a password in its userinfo.

``surface_oauth_url_card`` drops the query/fragment of the authorize URL for the
card body and the log line, but used to keep the rest verbatim -- so a
``https://user:pass@host/...`` URL put the password on the card and in the
transcript. The remaining URL now goes through the shared span primitive
(``gateway.mask_secret_spans``). The FULL url still rides the descriptor's
``inspect`` / context (the operator must be able to open it); only the
human-readable body and the log are asserted here."""
from __future__ import annotations

import logging

import pytest

from systemu.interface import harness_review
import systemu.interface.command.inbox as inbox_mod

_PASSWORD = "Sup3rS3cretPassw0rd9xQ"


def _build_card(tmp_path, monkeypatch, caplog, url):
    posted = {}

    def fake_enqueue(self, descriptor, *, gate_type, body="", context_extras=None):
        posted["body"] = body
        return "dec_test"

    monkeypatch.setattr(inbox_mod.InboxQueue, "enqueue", fake_enqueue)

    class _V:
        root = str(tmp_path)

    with caplog.at_level(logging.INFO):
        harness_review.surface_oauth_url_card(
            "server_abc", url, execution_id="exec_1", vault=_V())
    log_blob = "\n".join(r.getMessage() for r in caplog.records)
    return posted["body"], log_blob


@pytest.mark.parametrize("url", [
    f"https://operator:{_PASSWORD}@auth.example.com/authorize?client_id=x",
    f"https://operator:{_PASSWORD}@auth.example.com:8443/oauth/authorize",
    f"https://:{_PASSWORD}@auth.example.com/authorize#frag",
])
def test_card_body_and_log_carry_no_userinfo_password(tmp_path, monkeypatch, caplog, url):
    body, log_blob = _build_card(tmp_path, monkeypatch, caplog, url)
    assert _PASSWORD not in body and _PASSWORD not in log_blob
    # the host survives (the operator still sees where they are going) ...
    assert "auth.example.com" in body and "auth.example.com" in log_blob
    # ... and the query/fragment are still dropped
    assert "client_id=x" not in body and "frag" not in body


def test_a_plain_authorize_url_is_shown_unchanged_but_for_its_query(tmp_path, monkeypatch, caplog):
    body, _ = _build_card(tmp_path, monkeypatch, caplog,
                          "https://auth.example.com/authorize?client_id=x")
    assert "Link: https://auth.example.com/authorize?" in body
    assert "client_id=x" not in body


def test_a_url_the_mask_fails_on_is_not_shown(tmp_path, monkeypatch, caplog):
    """Fail closed: if the primitive throws, the card shows the placeholder, not
    the unmasked URL."""
    from systemu.messaging import gateway

    def _boom(_text):
        raise RuntimeError("mask rule failed")

    monkeypatch.setattr(gateway, "mask_secret_spans", _boom)
    url = f"https://operator:{_PASSWORD}@auth.example.com/authorize"
    body, log_blob = _build_card(tmp_path, monkeypatch, caplog, url)
    assert "(url redacted)" in body and "(url redacted)" in log_blob
    assert _PASSWORD not in body and _PASSWORD not in log_blob
