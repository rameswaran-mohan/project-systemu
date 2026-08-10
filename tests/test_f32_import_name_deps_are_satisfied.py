"""F32 — a dependency you can already import must not be reported missing.

Found by exercising the product's own differentiator end to end: a task needing
a capability that does not exist, so the Reverse-Harness asks, the operator
approves, and the forge writes the tool. The forged tool declared

    TOOL_META = {..., "dependencies": ["qrcode", "PIL"]}

`PIL` is the IMPORT name. The distribution is `pillow`, it ships in the core
install, and `import PIL` works. But `_is_satisfied` asked only
`importlib.metadata.version("PIL")`, which raises PackageNotFoundError, so the
tool was blocked pending an install of `PIL` -- a name pip cannot resolve on
modern PyPI. The tool could never be enabled, and the operator's only offered
remedy could never succeed.

This is not a one-package quirk. The forge is an LLM writing dependency lists,
and the well-known import/distribution splits are exactly the popular packages:
PIL/pillow, cv2/opencv-python, sklearn/scikit-learn, yaml/pyyaml,
bs4/beautifulsoup4, dateutil/python-dateutil. Any forged tool touching one of
them hits the same wall, in the headline feature.

THE PROPERTY. What a tool actually needs is to `import X`. Whether a
distribution of that exact name exists is a separate question and not the one
that matters at the gate. So satisfaction is: a metadata hit OR an importable
top-level module. Strictly wider than before, and it cannot mask a genuinely
absent package -- if neither holds, it is still missing.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_cache():
    from systemu.runtime import dependency_installer as di
    di.reset_cache_for_tests()
    yield
    di.reset_cache_for_tests()


def test_the_premise_pil_is_importable_but_has_no_distribution():
    """Guard the fixture: if this ever changes, the test below is vacuous."""
    import importlib.util
    from importlib import metadata

    assert importlib.util.find_spec("PIL") is not None, "pillow must be installed"
    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version("PIL")
    assert metadata.version("pillow")


def test_an_importable_module_counts_as_satisfied():
    """The concrete regression: a forged tool declaring PIL must not be blocked."""
    from systemu.runtime.dependency_installer import _is_satisfied

    assert _is_satisfied("PIL"), (
        "PIL is importable (pillow provides it) but was reported missing, so a "
        "forged tool declaring it is blocked behind `pip install PIL`, which "
        "cannot resolve"
    )


@pytest.mark.parametrize("dist", ["pillow", "click", "pytest"])
def test_distribution_names_still_resolve(dist):
    """The metadata path must keep working -- this widens, it does not replace."""
    from systemu.runtime.dependency_installer import _is_satisfied

    assert _is_satisfied(dist)


@pytest.mark.parametrize("absent", [
    "definitely_not_a_real_package_xyzzy",
    "qrcode_not_installed_here_either_zzz",
])
def test_a_genuinely_absent_package_is_still_missing(absent):
    """The check must not become permissive. If neither metadata nor import
    finds it, it is missing -- otherwise the gate would wave everything through."""
    from systemu.runtime.dependency_installer import _is_satisfied

    assert not _is_satisfied(absent)


def test_a_version_pinned_spec_still_resolves_by_name():
    """Declarations carry specifiers; the import fallback must use the bare
    top-level name, not the raw spec string."""
    from systemu.runtime.dependency_installer import _is_satisfied

    assert _is_satisfied("pillow>=10.0")


def test_a_dotted_or_extras_spec_does_not_crash_the_check():
    from systemu.runtime.dependency_installer import _is_satisfied

    for spec in ("PIL", "pillow[extra]", "pillow ; python_version>='3.9'"):
        assert _is_satisfied(spec), spec
