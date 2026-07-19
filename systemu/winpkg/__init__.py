"""E2 — Windows packaging runtime (SPEC §14 E2).

The **runtime half** of the E2 installer: everything the installed app must be
able to do on its own — resolve its install layout, run the first-run wizard,
upgrade/uninstall itself, and time the first completed task.

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
