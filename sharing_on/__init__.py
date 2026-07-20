"""sharing_on — Record computer activity, generate step-by-step instructions."""

# `sharing_on` and `systemu` are two top-level packages of ONE distribution
# (see pyproject `[tool.setuptools.packages.find] include`), so they share a
# version.  Re-exported, not re-declared: this literal had drifted to "0.9.59"
# alongside systemu's while pyproject said "0.10.21", which made `sharing_on
# --version` (cli.py's `version_option`) misreport the installed release.
#
# Deliberately NOT wrapped in try/except with a hard-coded fallback.  A fallback
# literal is a second source of truth that goes stale silently — the exact
# failure being fixed — and `systemu` is unconditionally present wherever
# `sharing_on` is installed.  `systemu/__init__.py` imports nothing, so this
# cannot cycle.
from systemu import __version__ as __version__
