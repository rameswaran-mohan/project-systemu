"""P7 -- the outbound User-Agent advertised a version this build has not been
for twenty-two releases.

THE DEFECT
    Three places mint the identity systemu puts on the wire, and all three
    carried a frozen literal:

        systemu/runtime/web_access.py            "systemu/0.9.8 ..."   (SENT)
        sharing_on/config.py  (x2)               "systemu/0.9.8 ..."
        systemu/vault/tools/implementations/fetch_json.py "systemu/0.9 ..."

    The first is attached to every outbound request the keyless web stack
    makes; the third is the seed tool the agent reaches for whenever it fetches
    a JSON endpoint. This build is 0.10.30. A User-Agent exists so the operator
    of a third-party service can tell WHICH BUILD is calling them -- Nominatim's
    usage policy requires exactly that -- so a frozen literal is not cosmetic:
    it is a false statement made to every server the agent touches, and it makes
    a rate-limit complaint or a bug report untraceable to a release.

    It is the same class as `sharing_on.__version__` drifting to "0.9.59" while
    the distribution said 0.10.21 (see `systemu/__init__.py`): a second literal
    for a fact that already has a source of truth goes stale silently.

THE RULING (and why NOT importlib.metadata first)
    The version is ``systemu.__version__`` -- the package's own literal.
    `pyproject.toml` declares ``dynamic = ["version"]`` from that attribute, so
    the wheel's metadata is DERIVED from it at build time and the two cannot
    drift in a real install. ``importlib.metadata.version("systemu")`` reads a
    BUILD ARTIFACT that goes stale the moment the literal changes without a
    re-install: measured on this very checkout it answered 0.10.25 while
    ``__version__`` was 0.10.30. So: ``__version__`` first, metadata only as a
    fallback where the import is impossible (the sandboxed seed tool).

THE PROPERTY PINNED HERE
    Each of the three identities equals
    ``systemu/<systemu.__version__> (+https://pypi.org/project/systemu)``.

    Pinned so it cannot pass by coincidence: `test_the_helper_reads_the_version_
    live` moves ``systemu.__version__`` to a value no release has ever had and
    the string moves with it, and `test_the_seed_tool_derives_its_version_too`
    does the same through a FRESH load of the sandboxed seed. Equality against
    today's version alone would still pass if someone re-typed today's number as
    a literal; these two would not.

    `test_no_outbound_identity_carries_a_version_literal` is the structural
    backstop: re-typing ANY version into any of the three files turns it red.

NO NETWORK IS TOUCHED. Every capture replaces the single egress call
(``requests.get`` / ``urllib`` ``_urlopen``) with a recorder, named at its use
site; nothing is stubbed in product code.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import re

import pytest


#: The public project page. NEVER a source-forge URL -- a forge URL carries an
#: account name as a path segment, which is what `test_e2e29_outbound_user_
#: agent_names_no_person.py` exists to keep off the wire.
_PROJECT_URL = "https://pypi.org/project/systemu"

#: A version string no release has ever carried, so a literal cannot match it.
_PROBE_VERSION = "0.0.0-probe-p7"


def _expected(version=None) -> str:
    import systemu

    return "systemu/{} (+{})".format(
        systemu.__version__ if version is None else version, _PROJECT_URL)


# --------------------------------------------------------------------------- #
# 0. the fact the config lane leans on
# --------------------------------------------------------------------------- #

def test_the_two_top_level_packages_share_one_version():
    """`sharing_on` and `systemu` are two packages of ONE distribution, and
    `sharing_on/__init__.py` re-exports systemu's literal rather than declaring
    its own. Pinned because `sharing_on.config` is one of the three sites, and
    an identity minted there must name the same build as one minted in
    `systemu.runtime`."""
    import sharing_on
    import systemu

    assert sharing_on.__version__ == systemu.__version__
    assert sharing_on.__version__ is systemu.__version__, (
        "sharing_on has grown its own version literal; the two can now drift")


# --------------------------------------------------------------------------- #
# 1. the shared helper -- DERIVED, not re-typed
# --------------------------------------------------------------------------- #

def test_the_helper_builds_the_public_identity():
    from systemu.runtime.user_agent import user_agent

    assert user_agent() == _expected()


def test_the_helper_reads_the_version_live(monkeypatch):
    """THE NON-TAUTOLOGY PIN. Equality against today's version would still pass
    if someone re-typed today's number as a literal. This moves the source of
    truth to a value no release has ever had; a literal cannot follow it."""
    import systemu
    from systemu.runtime.user_agent import user_agent

    monkeypatch.setattr(systemu, "__version__", _PROBE_VERSION)
    assert user_agent() == _expected(_PROBE_VERSION)


# --------------------------------------------------------------------------- #
# 2. site one -- the header the web stack SENDS
# --------------------------------------------------------------------------- #

def test_the_web_access_user_agent_is_the_package_version():
    import systemu.runtime.web_access as wa

    assert wa.USER_AGENT == _expected()


def _captured_outbound_header(monkeypatch, *, post: bool) -> str:
    """The User-Agent on a Request PRODUCTION built, for a GET or a POST.

    The header is read off the ``urllib.request.Request`` the builder made, not
    off the module constant it reads from -- so this pins what leaves the
    process, not what a constant says. The single egress call is replaced by a
    recorder and the admissibility gate is forced open (this is about the
    header, not about the gate); nothing leaves the process.
    """
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
        seen["ua"] = req.get_header("User-agent") or ""
        return _Resp()

    monkeypatch.setattr(wa, "_ssrf_admissible", lambda url: True)
    monkeypatch.setattr(wa, "_urlopen", _capture)

    if post:
        wa._http_post("https://example.invalid/api", b"{}")
    else:
        wa._http_get("https://example.invalid/api")

    assert "ua" in seen, "production never reached the egress call; nothing was pinned"
    return seen["ua"]


@pytest.mark.parametrize("post", [False, True], ids=["get", "post"])
def test_the_header_on_the_wire_names_this_build(monkeypatch, post):
    """REACHABILITY. The version property is stated on the real request builder,
    because the constant is only interesting insofar as it is attached."""
    assert _captured_outbound_header(monkeypatch, post=post) == _expected()


# --------------------------------------------------------------------------- #
# 3. site two -- the geocoder identity, at BOTH places the default is minted
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label", ["field default", "from_env default"])
def test_the_geocoder_user_agent_default_is_the_package_version(label):
    """Nominatim's usage policy requires an identifying User-Agent; identifying
    a build that shipped twenty-two releases ago does not satisfy it."""
    from sharing_on.config import Config

    cfg = Config() if label == "field default" else Config.from_env()
    assert cfg.nominatim_user_agent == _expected(), label


def test_the_geocoder_user_agent_honours_an_operator_override(monkeypatch):
    """The derivation replaces a literal DEFAULT, not the env knob. Pinned so a
    later tightening cannot quietly remove the operator's override."""
    from sharing_on.config import Config

    monkeypatch.setenv("SYSTEMU_NOMINATIM_UA", "my-integration/1.2 (+mailto:x)")
    assert Config.from_env().nominatim_user_agent == "my-integration/1.2 (+mailto:x)"


# --------------------------------------------------------------------------- #
# 4. site three -- the sandboxed seed tool
# --------------------------------------------------------------------------- #

def _seed_path() -> pathlib.Path:
    import systemu

    return (pathlib.Path(systemu.__file__).parent / "vault" / "tools"
            / "implementations" / "fetch_json.py")


def _load_seed(name="fetch_json_p7_uut"):
    """A FRESH module object each call, loaded from the seed file by path -- the
    way the sandbox reaches it, and so a version read at import time is re-read
    rather than cached from an earlier test."""
    spec = importlib.util.spec_from_file_location(name, _seed_path())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True}


def _seed_default_ua(monkeypatch, mod=None) -> str:
    """The UA the seed ACTUALLY defaults, read by RUNNING it -- not by regex.
    ``requests.get`` (the third-party network client, never product code) is
    replaced by a recorder."""
    import requests

    captured = {}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["headers"] = dict(headers or {})
        return _Resp()

    monkeypatch.setattr(requests, "get", _fake_get)
    out = (mod or _load_seed()).run(url="https://example.invalid/api")
    assert out["success"] is True, out
    return captured["headers"].get("User-Agent", "")


def test_the_seed_tool_user_agent_is_the_package_version(monkeypatch):
    assert _seed_default_ua(monkeypatch) == _expected()


def test_the_seed_tool_derives_its_version_too(monkeypatch):
    """THE NON-TAUTOLOGY PIN for the sandboxed copy. The seed cannot import the
    helper (it runs outside this process's package graph and is executed by
    path), so it carries its own three-step derivation -- and that derivation is
    proved the same way: move the source of truth, the string follows."""
    import systemu

    monkeypatch.setattr(systemu, "__version__", _PROBE_VERSION)
    ua = _seed_default_ua(monkeypatch, mod=_load_seed("fetch_json_p7_probe"))
    assert ua == _expected(_PROBE_VERSION)


def test_the_seed_tool_still_lets_the_caller_win(monkeypatch):
    """`setdefault` semantics, restated here because the derivation edits that
    exact line: a caller-supplied identity must still survive."""
    ua = None
    import requests

    captured = {}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["headers"] = dict(headers or {})
        return _Resp()

    monkeypatch.setattr(requests, "get", _fake_get)
    _load_seed().run(url="https://example.invalid/api",
                     headers={"User-Agent": "caller/9"})
    ua = captured["headers"]["User-Agent"]
    assert ua == "caller/9"


def test_the_seed_tool_survives_a_systemu_that_cannot_be_imported(monkeypatch):
    """The seed runs in the SANDBOX. If neither the package nor its installed
    metadata can be read, it must still send an identifying string rather than
    raise inside a tool call -- a fetch that dies on its own User-Agent is a
    worse outcome than one that identifies the project without a version."""
    import builtins

    real_import = builtins.__import__

    def _no_systemu(name, *a, **kw):
        if name == "systemu" or name.startswith("systemu."):
            raise ImportError("sandbox: systemu is not importable")
        if name == "importlib.metadata" or name == "importlib":
            raise ImportError("sandbox: no metadata")
        return real_import(name, *a, **kw)

    mod = _load_seed("fetch_json_p7_nosystemu")
    monkeypatch.setattr(builtins, "__import__", _no_systemu)
    ua = _seed_default_ua(monkeypatch, mod=mod)
    monkeypatch.undo()

    assert ua.startswith("systemu"), repr(ua)
    assert _PROJECT_URL in ua, repr(ua)


# --------------------------------------------------------------------------- #
# 5. the structural backstop
# --------------------------------------------------------------------------- #

#: Every file that mints an outbound identity. Named so a fourth site cannot be
#: added without appearing here.
_IDENTITY_SITES = (
    ("systemu", "runtime", "web_access.py"),
    ("sharing_on", "config.py"),
    ("systemu", "vault", "tools", "implementations", "fetch_json.py"),
)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def test_no_outbound_identity_carries_a_version_literal():
    """THE MUTATION CATCHER. Equality tests pass the moment someone re-types
    today's version; this does not. `systemu/<digit>` is the shape of a minted
    identity, and after this change not one of the three files may contain it.
    """
    offenders = []
    for parts in _IDENTITY_SITES:
        path = _repo_root().joinpath(*parts)
        assert path.exists(), path
        source = io.open(path, encoding="utf-8").read()
        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r"systemu/\d", line):
                offenders.append("{}:{}".format(path.name, i))

    assert not offenders, (
        "an outbound identity was re-typed as a version literal instead of "
        "derived from systemu.__version__; it will go stale on the next "
        "release exactly as 0.9.8 did: " + repr(offenders))


def test_the_three_identities_agree():
    """One build, one identity. Stated as a single assertion so a site that
    drifts is named by the failure rather than by three separate reds."""
    import systemu.runtime.web_access as wa
    from sharing_on.config import Config

    seen = {
        "web_access.USER_AGENT": wa.USER_AGENT,
        "config field default": Config().nominatim_user_agent,
        "config from_env default": Config.from_env().nominatim_user_agent,
    }
    assert set(seen.values()) == {_expected()}, seen
