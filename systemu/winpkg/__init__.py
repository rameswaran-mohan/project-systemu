"""E2 — Windows packaging runtime (SPEC §14 E2).

The **runtime half** of the E2 installer. What is WIRED -- each item below has
a caller in what ships:

  * **The three installer inputs.** :mod:`.build_step`
    (``python -m systemu.winpkg.build_step``, run before ISCC) produces the
    embeddable CPython, the offline wheelhouse and ``systemu-launcher.exe`` that
    the ``[Files]`` section of ``packaging/windows/systemu.iss`` consumes.
  * **The launcher.** :mod:`.launcher` is what ``systemu-launcher.exe`` runs: it
    starts the daemon if it is not up and opens the dashboard, which sends a
    fresh install to ``/welcome``. The global hotkey is reported absent.
  * **The vault pin.** The launcher exports ``SYSTEMU_VAULT_DIR`` as the
    install's sibling ``{app}\\vault``
    (:func:`.launcher.point_runtime_at_the_sibling_vault`); the installer marks
    that directory ``uninsneveruninstall``. :mod:`.layout` is the one layout
    all of the above resolve.
  * **The install stamp.** ``[Run]`` step 2 of the installer calls
    ``python -m systemu.winpkg.cli stamp-installed``.

What is NOT wired -- importable and tested, but nothing that ships calls it:

  * :mod:`.first_run` -- the provider-key entry / verify / hand-off wizard -- is
    NOT USED. The installed first run is the dashboard's ``/welcome`` page,
    which asks for the key in ``.env``; nothing hands a key to this module.
  * :mod:`.lifecycle` -- ``perform_upgrade`` / ``perform_uninstall`` have no
    caller; uninstall is the installer script's own ``[UninstallDelete]``.
  * ``cli stamp-first-task`` -- nothing invokes it, so the time to the first
    completed task is not recorded automatically.

``tests/test_winpkg_docstring_names_only_what_is_wired.py`` checks both lists
against the tree: wiring one of the modules above fails that test until this
text moves it.

Why this lives under ``systemu/`` and not the ``packaging/windows/`` tree the
spec names: ``[tool.setuptools.packages.find]`` includes only ``sharing_on*``
and ``systemu*``, so a top-level ``packaging/`` package would NOT ship in the
wheel — and the installer invokes this code *after* installing the wheel.
``packaging/windows/`` holds the build-time artifacts (the Inno Setup script);
it is deliberately NOT an importable package, both because it is not shipped
and because a top-level ``packaging`` module would shadow PyPA's ``packaging``
on any path where the repo root is importable.

NAMING: this package is deliberately NOT called ``installer``. The repo already
has two unrelated things by that name — ``systemu/runtime/dependency_installer.py``
(pip self-heal for forged tools) and ``tests/test_installer.py`` (covers the
developer-facing ``install.py``). Neither has anything to do with E2.
"""

from .layout import InstallLayout, resolve_layout          # noqa: F401
from .first_run import (                                   # noqa: F401
    FirstRunResult,
    ProviderKeyReceipt,
    ProviderKeyRejected,
    record_provider_key,
    decide_handoff,
    run_first_run,
)
from .lifecycle import (                                   # noqa: F401
    UnsafeLayout,
    UpgradeFailed,
    UninstallReport,
    perform_upgrade,
    perform_uninstall,
)
from .metrics import FirstRunMetrics                        # noqa: F401
