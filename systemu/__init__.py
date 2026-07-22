"""Systemu — the meta-agent factory layer for Sharing-On."""

# THE single source of truth for this distribution's version.
#
# `pyproject.toml` does NOT carry a literal version; it declares
# `dynamic = ["version"]` with `[tool.setuptools.dynamic] version = {attr =
# "systemu.__version__"}`, so the sdist/wheel metadata is DERIVED from this
# line at build time and the two cannot drift.  `sharing_on.__version__`
# re-exports this same object.  `tests/test_version_single_source.py` pins all
# three, plus the migrator behaviour that depends on it.
#
# WHY NOT `importlib.metadata.version("systemu")`: measured in this repo, that
# call returns three different WRONG answers depending on cwd — it raises
# PackageNotFoundError from a worktree, and reads a stale build-time snapshot
# (`systemu.egg-info`, and a `sharing_on-0.1.0.dist-info` in site-packages)
# otherwise.  Installed dist metadata is a build artifact that goes stale the
# moment this line changes without a re-install, which is the very failure mode
# being fixed.  A module literal is correct in a source checkout, an editable
# install, and a wheel alike.
#
# THIS VALUE IS LOAD-BEARING, NOT COSMETIC.  It gates, per version marker:
#   * `vault_migrator.run` — seed-tool add/update (`.seed_version`)
#   * `vault_migrator.backfill_effect_tags` (`.effect_tags_seed`) — this one is
#     keyed on `<version>+<_EFFECT_TAGS_GENERATION>`, NOT on the version alone,
#     so a fix to the DERIVATION RULES can re-derive without a release bump.
#     That matters because the live-tryout rule folds fixes into the current
#     version; anything gated on this string alone would never reach a deployed
#     vault.  Bump the generation there, not this, for a rules change.
#   * `first_gate_review.maybe_post_first_gate_review` (bulk review card)
#   * `tool_reconciler.recover_stale_dry_run_failures` (re-validate once/upgrade)
# Freezing it freezes all four: a vault whose marker already holds this string
# takes the fast path and NEVER receives seed changes.  It was pinned at
# "0.9.59" across 22 releases (through 0.10.21) and that is exactly what
# happened — proven by driving the real migrator against a real vault.
__version__ = "0.10.21"
