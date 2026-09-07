"""The one outbound identity systemu puts on the wire.

WHY THIS MODULE EXISTS
    Three places used to mint this string and all three carried a frozen
    literal: ``web_access.USER_AGENT`` said 0.9.8, ``config.nominatim_user_agent``
    said 0.9.8 twice, and the seed ``fetch_json`` tool said 0.9 -- while the
    distribution had moved on to 0.10.30.  A User-Agent exists so the operator
    of a third-party service can tell WHICH BUILD is calling them (Nominatim's
    usage policy requires exactly that), so a frozen literal is a false
    statement made to every server the agent touches.

    The two in-process sites now read this helper.  The seed tool cannot -- it
    is executed by path inside the sandbox, outside this package graph -- so it
    carries its own three-step derivation, and both are pinned by
    ``tests/test_dogfood28_p7_outbound_user_agent_is_the_package_version.py``.

WHY ``systemu.__version__`` AND NOT ``importlib.metadata``
    ``pyproject.toml`` declares ``dynamic = ["version"]`` from that attribute,
    so the wheel's metadata is DERIVED from it at build time and the two cannot
    drift in a real install.  ``importlib.metadata.version("systemu")`` reads a
    BUILD ARTIFACT that goes stale the moment the literal changes without a
    re-install -- measured on an editable checkout it answered 0.10.25 while
    ``__version__`` was 0.10.30, i.e. it reproduces the very staleness this
    module exists to remove.  See the long note in ``systemu/__init__.py``.

DELIBERATELY IMPORT-LIGHT.  ``sharing_on.config`` evaluates this at module
import to build a dataclass field default, so this module pulls in nothing but
``systemu`` itself (whose ``__init__`` imports nothing).  Reaching for
``web_access`` instead would have dragged ``ssl``/``urllib``/``net_safety``
into every config import.
"""
from __future__ import annotations

#: The public project page.  NEVER a source-forge URL: a forge URL carries an
#: account name as a path segment, which is how the author's account was
#: disclosed to every third-party server before v0.10.30.  Pinned by
#: ``tests/test_e2e29_outbound_user_agent_names_no_person.py``.
PROJECT_URL = "https://pypi.org/project/systemu"

#: What the identity says when the version cannot be read at all.  Unreachable
#: from inside this package (``systemu/__init__.py`` imports nothing, so the
#: attribute is always there); kept so the shape below has no branch that can
#: produce ``"systemu/ (+...)"``.
_UNVERSIONED = "systemu"


def package_version() -> str:
    """This distribution's version, or ``""`` if it genuinely cannot be read."""
    import systemu

    value = getattr(systemu, "__version__", None)
    # ``type(x) is str`` rather than ``isinstance``/truthiness: the only check
    # that cannot be answered by a __class__ or __bool__ of someone else's
    # choosing (DEC-36).  A non-str version is treated as absent, never
    # coerced into the header.
    return value if type(value) is str and value else ""


def user_agent() -> str:
    """``systemu/<version> (+<project url>)`` -- the string every outbound
    request identifies this build with."""
    version = package_version()
    if not version:
        return "{} (+{})".format(_UNVERSIONED, PROJECT_URL)
    return "systemu/{} (+{})".format(version, PROJECT_URL)
