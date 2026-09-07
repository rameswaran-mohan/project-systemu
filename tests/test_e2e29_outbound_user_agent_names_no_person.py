"""D1 (wider) -- the User-Agent systemu puts on the wire names no person.

FOUND BY THE HYGIENE FENCE, not by the report.  The reported defect was
sdist-only: private names sitting in test text.  Running
``tests/test_shippable_tree_names_no_private_place.py`` over the whole
shippable tree turned up three more, and they are not test text at all:

    systemu/runtime/web_access.py   the default ``USER_AGENT``
    sharing_on/config.py (x2)       the default ``nominatim_user_agent``

all three of the form ``systemu/<v> (+https://<source forge>/<account
name>/<repository>)``.  That string is in the WHEEL, not only the sdist, and
the first of the three is SENT -- ``_http_get`` and ``_http_post`` put it on
every outbound request the web stack makes, so the author's account name was
disclosed to every third-party server the agent touched.  That is a wider
surface than the reported defect, not a smaller one.

THE PROPERTY PINNED HERE
    The header the production request builder ACTUALLY attaches names the
    public project and nothing else: it equals the expected public string and
    carries no source-forge host.  Pinned on the REAL path -- the assertion
    reads the header off a ``urllib.request.Request`` that ``_http_get`` and
    ``_http_post`` built, not off the module constant they read it from.

NO NETWORK IS TOUCHED.  The single egress call is replaced by a capture, and
the admissibility gate is forced open so the builder is reached at all; both
substitutions are named at their use site.
"""
from __future__ import annotations

import pytest


#: What every outbound identity is allowed to say.  The public index page, not
#: a repository URL: a repository URL is where an account name lives.
_EXPECTED_WEB_ACCESS_UA = "systemu/0.9.8 (+https://pypi.org/project/systemu)"

#: Hosts whose URLs carry an account name as a path segment.  Naming one in an
#: outbound identity is what disclosed the author.
_SOURCE_FORGE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


def _captured_request_header(monkeypatch, *, post: bool) -> str:
    """The User-Agent on a Request that PRODUCTION built, for a GET or a POST."""
    import systemu.runtime.web_access as wa

    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _capture(req, *, timeout):
        # The header is read off the Request the production builder made; the
        # module constant is never consulted here.
        seen["ua"] = req.get_header("User-agent") or ""
        return _Resp()

    # The admissibility gate would refuse an unresolvable host before the
    # builder runs; this test is about the header, not about the gate, so the
    # gate is forced open and nothing leaves the process.
    monkeypatch.setattr(wa, "_ssrf_admissible", lambda url: True)
    monkeypatch.setattr(wa, "_urlopen", _capture)

    if post:
        wa._http_post("https://example.invalid/api", b"{}")
    else:
        wa._http_get("https://example.invalid/api")

    assert "ua" in seen, "production never reached the egress call; nothing was pinned"
    return seen["ua"]


@pytest.mark.parametrize("post", [False, True], ids=["get", "post"])
def test_the_outbound_user_agent_is_exactly_the_public_string(monkeypatch, post):
    ua = _captured_request_header(monkeypatch, post=post)
    assert ua == _EXPECTED_WEB_ACCESS_UA, (
        "this string is sent to every third-party server the web stack "
        "contacts, so it may name the public project and nothing else; "
        "expected " + repr(_EXPECTED_WEB_ACCESS_UA) + ", got " + repr(ua))


@pytest.mark.parametrize("post", [False, True], ids=["get", "post"])
def test_the_outbound_user_agent_names_no_source_forge(monkeypatch, post):
    """Stated separately from the equality: a future edit that changes the
    public string must still not be able to reintroduce an account-bearing
    URL, and this is the reason the equality above exists."""
    ua = _captured_request_header(monkeypatch, post=post).lower()
    named = [host for host in _SOURCE_FORGE_HOSTS if host in ua]
    assert not named, (
        "the outbound identity names a source forge, and a forge URL carries "
        "an account name: " + repr(named))


def test_the_geocoder_user_agent_default_names_no_source_forge():
    """The nominatim identity, at both places the default is minted.

    Stated on the CONFIG VALUE rather than on a captured request because this
    build ships no caller for it -- the field is a declared default an operator
    can read and an integrator can send. Pinning the value is therefore the
    whole of what can honestly be pinned today; when a caller lands it should
    be re-pinned on the captured header, the way the two tests above are.
    """
    from sharing_on.config import Config

    for label, cfg in (("field default", Config()),
                       ("from_env default", Config.from_env())):
        ua = str(cfg.nominatim_user_agent).lower()
        named = [host for host in _SOURCE_FORGE_HOSTS if host in ua]
        assert not named, (
            "the geocoder identity (" + label + ") names a source forge, and a "
            "forge URL carries an account name: " + repr(named))
        assert "systemu" in ua, (
            "the geocoder identity (" + label + ") must still identify the "
            "application: " + repr(ua))
