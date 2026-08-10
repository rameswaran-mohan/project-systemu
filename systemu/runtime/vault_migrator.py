"""v0.8.22 — silently bring the vault's seed tools up to the installed
package's version. Auto-runs on daemon start. Idempotent via .seed_version.
Identity is by NAME (not by `forged_by_systemu` flag) — POC finding: user
vaults mis-flag all seed tools as forged, so a flag-based filter would
prevent legitimate seed updates from ever reaching the vault. That same
mis-flag is REPAIRED (sha-guarded) by `normalize_seed_forged_flags`, which
runs on every boot ahead of the fast path. Never raises; wrapped by daemon
try/except so a migration failure can't break boot.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.resources
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

_SEED_VERSION_FILENAME = ".seed_version"


def _installed_version() -> str:
    """Returns the installed systemu package version."""
    import systemu
    return getattr(systemu, "__version__", "0.0.0")


def _package_vault_root() -> Path:
    """Returns the installed package's vault dir.

    Uses importlib.resources so dev-source-tree cwds (where a local `systemu/`
    can shadow the site-packages install) don't fool us into reading from the
    wrong tree. POC finding §5.2 step 3.
    """
    with importlib.resources.as_file(
        importlib.resources.files("systemu").joinpath("vault")
    ) as p:
        return Path(p)


def _read_seed_version(vault_dir: Path) -> str:
    p = vault_dir / _SEED_VERSION_FILENAME
    if not p.exists():
        return "0.0.0"
    try:
        return p.read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def _write_seed_version_atomic(vault_dir: Path, version: str) -> None:
    """Atomic write — tempfile + os.replace so interrupted writes leave the
    file untouched. POC requirement §5.3."""
    fd, tmp = tempfile.mkstemp(dir=str(vault_dir), prefix=".seed_version.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(version)
        os.replace(tmp, str(vault_dir / _SEED_VERSION_FILENAME))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


_EFFECT_TAGS_SEED_FILENAME = ".effect_tags_seed"

# The DERIVATION GENERATION, independent of the release version.
#
# `.effect_tags_seed` gates the backfill on the INSTALLED VERSION, which makes a
# fix to the DERIVATION RULES invisible to every vault already sitting on the
# version that shipped the bug — the marker matches, the fast path returns, and
# the only re-deriver never runs. That is not hypothetical: this project's
# live-tryout rule deliberately folds fixes into the current version WITHOUT a
# bump, so "the next release repairs it" describes a release that never happens.
#
# Bumping this string makes every existing marker mismatch exactly ONCE, which
# re-derives every body under the current rules and then re-converges the
# headers. Bump it whenever the derivation changes in a way that makes an
# ALREADY-WRITTEN stamp wrong — a stamp that is merely absent is repaired by the
# `bodies_missing_tag_key` self-heal in `run` instead, which needs no bump at all.
#
# g2: the UNKNOWN floor in `backfill_effect_tags`. Bodies stamped by g1 under
#     `declared if declared else scanned` (and by the intermediate union-only
#     shape) can carry a benign self-declared class against a SILENT scan — a
#     stamp that is PRESENT and WRONG, so nothing that keys on absence can find
#     it. This bump is what makes that fix reach a deployed vault.
_EFFECT_TAGS_GENERATION = "g2"


def _effect_tags_marker_value(version) -> str:
    """The `.effect_tags_seed` payload: version AND derivation generation.

    Both halves matter. The version re-derives when the SOURCE may have changed
    (a release replaces implementations); the generation re-derives when the
    RULES changed against unchanged source. A marker carrying only the version
    cannot express the second, which is the whole defect this suffix closes."""
    return f"{version}+{_EFFECT_TAGS_GENERATION}"


def _write_text_atomic(path: Path, text: str) -> None:
    """tempfile + os.replace so an interrupted write leaves the target intact."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _declared_effect_tags(source) -> Set[str]:
    """R-A13b-2ii-b — a tool's SELF-DECLARED effect classes from its module-level
    ``TOOL_META = {..., "effect_tags": [...]}`` dict literal.

    AST-parse only — this NEVER imports the tool module (importing risks side effects +
    missing third-party deps; the registry itself defers import). We walk the MODULE-LEVEL
    body for a ``TOOL_META`` assignment whose value is a dict literal, then
    ``ast.literal_eval`` JUST the ``effect_tags`` value (robust to a future TOOL_META with
    non-literal values on other keys). Each entry is coerced to a KNOWN tag value; anything
    unknown / blank is skipped. NEVER raises → ``set()`` on any error.

    The backfill treats this set as ADDITIVE ONLY. It is UNIONED with the structural scan
    (so it cannot SUBTRACT a class the scanner found) and, when the scan came back SILENT,
    ``UNKNOWN`` is seeded alongside it (so it cannot MANUFACTURE a classification either).
    A declaration can therefore RAISE severity and nothing else — see the long comment at
    the union in ``backfill_effect_tags`` for the measurements behind both rules.
    """
    if not isinstance(source, str) or not source.strip():
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    try:
        from systemu.runtime.effect_tags import coerce, EffectTag
        unknown = EffectTag.UNKNOWN.value

        raw = None
        found = False
        for node in tree.body:  # MODULE-LEVEL statements only (a nested TOOL_META is ignored)
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not any(isinstance(t, ast.Name) and t.id == "TOOL_META" for t in targets):
                continue
            if not isinstance(value, ast.Dict):
                continue
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and k.value == "effect_tags":
                    try:
                        raw = ast.literal_eval(v)
                    except Exception:  # noqa: BLE001 — non-literal ⇒ unreadable, fall back
                        raw = None
                    found = True
            if found:
                break  # first module-level TOOL_META wins

        if raw is None:
            return set()
        if isinstance(raw, (str, bytes)):
            items = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            items = list(raw)
        else:
            return set()

        out: Set[str] = set()
        for item in items:
            val = coerce(item)
            if val and val != unknown:
                out.add(val)
        return out
    except Exception:  # noqa: BLE001 — declaration is best-effort; never break boot
        return set()


def backfill_effect_tags(vault_dir, *, version: str | None = None, logger_=None,
                         force: bool = False) -> Dict[str, Any]:
    """G0 — one-pass, idempotent backfill of ``Tool.effect_tags`` onto every vault
    tool body, classified deterministically from its implementation source
    (spec UNIFIED-v2 §5.7 / §9 G0).

    Version-gated via ``.effect_tags_seed`` (independent of the seed-tool fast
    path), so it runs once per version bump and is a no-op on every boot after.
    NEVER raises — a classification failure on one tool must not break boot; it
    leaves that tool's tags empty (⇒ UNKNOWN-at-gate) and moves on.

    The implementation is resolved by ``_resolve_vault_impl`` — the RUNTIME's
    anchoring (relative ⇒ vault root's PARENT) plus containment inside
    ``vault/tools/implementations/``. This previously anchored at the
    implementations dir, which does not resolve for ANY shipped seed body and so
    stamped ``[]`` on all of them; ``skipped_impl_path`` now counts the bodies
    refused for pointing outside, so that state is visible rather than silent.

    NOTE on the empty stamp: ``[]`` is fail-closed at the action SCORER
    (``action_governance._effective_tags`` maps an empty set to ``UNKNOWN``) but
    is NOT fail-closed in every consumer built on top of it — see
    ``messaging.decision_bridge.classify_resolution``, where a present-but-empty
    list satisfies the money/irreversible floor check. Correct tags therefore
    matter beyond classification quality.

    ``force=True`` bypasses the marker. ``run`` uses it for TWO distinct repairs,
    both of which happen on a boot where the marker is ALREADY current:

      * after its seed loop, which OVERWRITES tool bodies with the package's
        (which carry no ``effect_tags``) — the wipe would otherwise persist until
        the next version bump;
      * when ``converge_index_effect_tags`` reports ``bodies_missing_tag_key`` — a
        body that lost the key on some EARLIER boot, under a build that had
        neither repair. That is the state a vault damaged by the pre-fix migrator
        is left in, and no version-gated pass can reach it.

    Re-deriving rather than carrying the old tags across is the correct repair in
    both: the seed loop also replaced the IMPLEMENTATION, so the pre-migration
    classification describes code that no longer exists (the same reason
    ``_entry_keeping_disable`` refuses to carry the schema summaries across).

    ``force`` does NOT reach a stamp that is present but derived under an older
    RULE SET — nothing can detect that by inspection, because the wrong value
    looks exactly like a right one. ``_EFFECT_TAGS_GENERATION`` is what repairs
    that class, by making the marker itself mismatch once.

    This function stamps BODIES ONLY. Projecting a body's tags up onto the index
    header is ``converge_index_effect_tags``'s job and hers alone — one writer, so
    there is no second path to keep in agreement.
    """
    log = logger_ or logger
    vault_dir = Path(vault_dir)
    version = version or _installed_version()

    try:
        marker = vault_dir / _EFFECT_TAGS_SEED_FILENAME
        marker_value = _effect_tags_marker_value(version)
        # The marker carries the DERIVATION GENERATION as well as the version, so a
        # vault stamped by an older generation re-derives even though it is already
        # on the installed version. A pre-generation marker holds the bare version
        # string, which never equals `<version>+gN` — so it mismatches too, which is
        # exactly the one-shot repair a vault that never sees a bump depends on.
        if (not force and marker.exists()
                and marker.read_text(encoding="utf-8").strip() == marker_value):
            return {"fast_path": True, "effect_tags_seed": marker_value}

        idx_path = vault_dir / "tools" / "index.json"
        if not idx_path.exists():
            # no tool catalog yet — retry on a later boot (do NOT stamp)
            return {"skipped": True, "reason": "no tool index"}

        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            log.error("[EffectTagBackfill] cannot read tool index: %s", exc)
            return {"skipped": True, "reason": f"index unreadable: {exc}"}

        from systemu.runtime.effect_tags import (classify_source, EffectTag,
                                                 normalize_tagset)
        from systemu.runtime import effect_signals

        impl_dir = vault_dir / "tools" / "implementations"
        bodies_written = 0
        classified = 0
        unclassified = 0
        unclassified_names: list = []
        skipped_impl_path = 0
        errors: list = []
        for entry in entries:
            tid = entry.get("id")
            name = entry.get("name")
            if not tid:
                continue
            body_path = vault_dir / "tools" / f"tool_{tid}.json"
            if not body_path.exists():
                continue
            try:
                body = json.loads(body_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"read {tid}: {exc}")
                continue

            # Resolve the implementation the way the RUNTIME resolves it: a
            # relative `implementation_path` is anchored at the vault root's
            # PARENT, not at the implementations dir. That is how the value is
            # WRITTEN (`tool_forge`/`tool_recalibrator`:
            # `impl_path.relative_to(vault_dir.parent)`) and how it is READ back
            # (`tool_sandbox.execute_tool`: `vault_root.parent / path`), which is
            # why every shipped seed body carries
            # `vault/tools/implementations/<name>.py`. Anchoring that string at
            # `impl_dir` yielded `<vault>/tools/implementations/vault/tools/
            # implementations/<name>.py` — a path that never exists — so the
            # defensively-wrapped read silently produced EMPTY source and every
            # tool was stamped `[]` while the pass reported success.
            #
            # Shares `_resolve_vault_impl` with `normalize_seed_forged_flags`:
            # same anchoring, same refusal of anything landing outside
            # `vault/tools/implementations/` (via `..`, a symlink, or an absolute
            # path) by path-COMPONENT containment after `.resolve()`.
            declared = body.get(_IMPL_PATH_FIELD)
            blank = declared is None or (
                isinstance(declared, str) and not declared.strip())
            if blank and not name:
                # No declared path AND no name to fall back on: there is nothing
                # to resolve, which is not the same thing as a REFUSAL.
                impl_path = None
            else:
                impl_path = _resolve_vault_impl(vault_dir, impl_dir, name, declared)
                if impl_path is None:
                    # Declared, but it does not land inside the implementations
                    # dir. Counted so an operator can SEE it: a silently empty
                    # tag list is exactly the failure this fix removes.
                    skipped_impl_path += 1

            source = ""
            # is_file(), not exists(): a declared path may name the implementations
            # DIRECTORY itself, which passes containment — reading it would only
            # raise into the handler below.
            if impl_path is not None and impl_path.is_file():
                try:
                    source = impl_path.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"source {tid}: {exc}")

            # R-A13b-2ii — a tool's SELF-DECLARED effect_tags (TOOL_META) may only ever
            # ADD to the independently-derived classification. Two rules enforce that,
            # and they close DIFFERENT halves of the same hole:
            #
            #   (i)  UNION, never replace — a declaration cannot SUBTRACT a class the
            #        scanner found;
            #   (ii) the UNKNOWN FLOOR — a declaration cannot take the tagset from
            #        EMPTY to non-empty, i.e. it cannot manufacture a classification
            #        where the scanner produced none.
            #
            # Then the MONOTONIC money-move FLOOR: any independent money signal
            # (import/host/attr) ALWAYS adds MONEY_MOVE. All re-derived from source each
            # pass → idempotent.
            #
            # THIS IS A SECURITY BOUNDARY. `TOOL_META` lives in the tool BODY, so on a
            # forged or operator-substituted tool it is attacker-controlled — exactly the
            # input an independent classifier exists to not depend on.
            #
            # (i) MEASURED, on the real backfill: a body doing
            # `requests.post("https://.../pay", json=k)` (scan ⇒ ['net_mutate']) that
            # declares `["local_read"]` was stamped ['local_read'] under
            # `declared if declared else scanned` — and FOUR independent controls read
            # that stamp, all of which flipped to the permissive side because the class
            # they key on had been subtracted:
            #   action_governance.requires_isolation   True → False  (MUST_ISOLATE)
            #   action_governance.has_network_egress   True → False  (NET_EFFECTS)
            #   effect_tags.is_high_severity           True → False  (HIGH_SEVERITY)
            #   requirement_binder._effect_tags_are_dangerous
            #                                          True → False  (_STAMP_EFFECTS)
            # Under the union it is stamped ['local_read','net_mutate'] and all four
            # read True again.
            #
            # (ii) THE UNION ALONE DOES NOT CLOSE THIS. Measured on the real backfill +
            # the real gate, with the union in place: a body whose dangerous effect is
            # reached through a call the AST scan cannot follow —
            #     import helpers
            #     TOOL_META = {'effect_tags': ['local_read']}
            #     def run(**kw): return helpers.nuke(kw)
            # — scans to NOTHING, so `scanned | declared` == `{'local_read'}`, and there
            # is no scanner-found class for the union to protect. The stamp then went
            #     no declaration      → []              → evaluate_action REQUIRE_APPROVAL
            #     declares local_read → ['local_read']  → evaluate_action ALLOW
            # A non-empty benign list ALSO satisfies `_effective_tags`'s `local_only`
            # test, which suppresses the name verb-map escalation — so declaring a
            # benign class removed the second line of defence too. That is a tool
            # authoring its own privilege downgrade, and it is strictly BETTER than
            # declaring nothing, which is the wrong incentive for a self-report.
            #
            # The floor: when the scan is SILENT, UNKNOWN is seeded alongside whatever
            # the body declared. A self-report may ADD information; it may never REMOVE
            # the unclassified status a silent scan implies. `['local_read','unknown']`
            # and `[]` are then scored IDENTICALLY by `evaluate_action` (UNKNOWN is in
            # the set either way, so `local_only` is False and the two-band UNKNOWN rule
            # governs both) and by `requirement_binder._effect_tags_are_dangerous`
            # (UNKNOWN ⇒ True, exactly as `[]` ⇒ True). Declaring a HIGH-severity class
            # against a silent scan still RAISES — `['money_move','unknown']` reaches the
            # DENY floor where `[]` only reaches REQUIRE_APPROVAL — so the direction
            # stays monotonic.
            #
            # Measured blast radius on the shipped catalog: ZERO of the 41 packaged
            # implementations declare `TOOL_META["effect_tags"]` at all, so this floor
            # adds no friction to any first-party tool. It only ever fires on a body
            # that chose to self-report, which is the only body that could exploit it.
            #
            # This is the SAME trust boundary `capability_index.derive_index` draws for
            # MCP rows — but NOT the same posture, and the comments there say so: MCP
            # grants a server's self-report ZERO influence (hardcoded `[]`), whereas
            # here a declaration retains real, gate-moving influence in the ADDITIVE
            # direction. Converging further (dropping declarations entirely) is
            # possible; it is not what this code does.
            try:
                if source:
                    declared = _declared_effect_tags(source)          # self-report (2ii-b)
                    scanned = {t.value for t in classify_source(source)}
                    tagset = scanned | set(declared)                  # (i) may only ADD
                    if declared and not scanned:
                        # (ii) UNKNOWN FLOOR — a silent scan stays unclassified no
                        # matter what the body says about itself. Deliberately NOT
                        # `if not tagset`: an EMPTY stamp is already UNKNOWN at every
                        # consumer, and it is the empty→non-empty EDGE that moves the
                        # gate, so the floor keys on the edge itself.
                        tagset.add(EffectTag.UNKNOWN.value)
                    # MONOTONIC money FLOOR. NOW REDUNDANT WITH THE SCAN, and kept
                    # deliberately: `classify_source` consults the SAME curated
                    # `effect_signals` map, so once `scanned` is always unioned in it
                    # already carries `money_move` on every input where this predicate
                    # is True (no divergent input was found). It was load-bearing under
                    # `declared if declared else scanned`, which DISCARDED the scan
                    # whenever a declaration existed. One line of belt to the union's
                    # braces on the one guarantee that must never fail open.
                    # `TestADeclarationCannotSubtract` measures the redundancy rather
                    # than asserting either mechanism alone — deleting BOTH fails it,
                    # deleting either does not.
                    if effect_signals.any_money_move_signal(source):
                        tagset.add(EffectTag.MONEY_MOVE.value)
                    # F14 — NO_EFFECT is EXCLUSIVE. `classify_source` only ever emits
                    # it alone, but the union above can pair it with a declaration, and
                    # `['no_effect','net_mutate']` is a record that says two opposite
                    # things. `normalize_tagset` drops the verified-none witness
                    # whenever any real class is present — never the reverse, so the
                    # escalate-only direction survives.
                    tags = normalize_tagset(tagset)
                else:
                    tags = []
            except Exception as exc:  # noqa: BLE001
                errors.append(f"classify {tid}: {exc}")
                tags = []

            body["effect_tags"] = tags
            try:
                _write_text_atomic(body_path, json.dumps(body, indent=2) + "\n")
                bodies_written += 1
                # THE COUNTER THAT MATTERS. `stamped` (this counter's old name)
                # counted bodies WRITTEN — including every one written with `[]` —
                # and an operator read "stamped=41" as "41 tools are classified"
                # while 24 of them had ended with an EMPTY tag set. A tool is
                # CLASSIFIED iff its effects were actually determined: real classes,
                # or the `no_effect` witness. Empty is UNCLASSIFIED and is counted
                # as such, by that name.
                if tags:
                    classified += 1
                else:
                    unclassified += 1
                    unclassified_names.append(str(name or tid))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write {tid}: {exc}")

        try:
            _write_text_atomic(marker, marker_value)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"marker: {exc}")

        # THE OPERATOR-FACING LINE. It leads with `classified`, and `bodies_written`
        # says what it counts, because the previous line — `stamped=41 ... errors=0`
        # — was read as a clean bill of health on a pass that had left 24 of those
        # 41 tools with an empty tag set. A counter on a safety surface must mean
        # what a reader will take it to mean (DEC-27: completeness WITNESSED, never
        # inferred).
        log.info(
            "[EffectTagBackfill] marker=%s classified=%d unclassified=%d "
            "bodies_written=%d skipped_impl_path=%d errors=%d",
            marker_value, classified, unclassified, bodies_written,
            skipped_impl_path, len(errors))
        if unclassified:
            # NAME them. An unclassified tool still EXECUTES; the operator cannot act
            # on a bare count. Bounded so a large vault cannot flood the log.
            shown = sorted(unclassified_names)[:20]
            log.warning(
                "[EffectTagBackfill] %d tool(s) could NOT be classified and are "
                "UNKNOWN at every gate: %s%s", unclassified, ", ".join(shown),
                "" if unclassified <= len(shown) else f" (+{unclassified - len(shown)} more)")
        return {"fast_path": False, "effect_tags_seed": marker_value,
                "bodies_written": bodies_written, "classified": classified,
                "unclassified": unclassified,
                "unclassified_names": sorted(unclassified_names),
                "skipped_impl_path": skipped_impl_path, "errors": errors}
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[EffectTagBackfill] unexpected failure (non-fatal): %s", exc)
        return {"error": str(exc)}


_EFFECT_TAGS_FIELD = "effect_tags"


def converge_index_effect_tags(vault_dir, *, logger_=None) -> Dict[str, Any]:
    """Project each tool BODY's ``effect_tags`` onto its ``tools/index.json``
    header, so the field reaches every reader that goes through the header.

    WHY THIS IS A SEPARATE PASS AND NOT PART OF THE BACKFILL. Readers do not read
    tool bodies. ``vault.Vault.list_tools()`` returns ``load_index("tools")`` —
    the header list — and BOTH live consumers sit on top of that:
    ``capability_index.derive_index`` (``IndexRow.effect_tags``) and
    ``table_reconciler._project_tools`` (``usage={"effect_tags": ...}``). A pass
    that stamps only bodies is therefore invisible to both. The dashboard reaches
    the same list through the ``storage.file_vault.FileVault`` adapter, which
    forwards ``list_tools``/``load_index`` verbatim (it is a wrapper around a
    ``Vault``, NOT a second implementation).

    Doing it inside ``backfill_effect_tags`` was measured NOT to work, twice over:

      1. THE BACKFILL IS VERSION-GATED AND THE INDEX IS NOT. Its ``.effect_tags_seed``
         fast path returns before any work on every vault that has already booted
         once on the installed version — i.e. on every real deployed vault — so an
         index-write behind that gate never executes. This pass runs
         FAST-PATH-INDEPENDENTLY on every boot, the way
         ``normalize_seed_forged_flags`` does and for the same reason: a fix that
         only runs on a version bump has not shipped to anybody already on that
         version.
      2. ``run``'s SEED LOOP OVERWRITES THE INDEX AFTERWARDS. It replaces a matched
         entry with the PACKAGED header (``_entry_keeping_disable(pkg_entry, ...)``)
         and the packaged ``tools/index.json`` carries no ``effect_tags`` key at
         all, so a write performed before that loop is not merely stale — it is
         gone. Measured on a real vault via ``run()``: ``updated=1``,
         ``skipped_identical=40``, and the updated tool ended with the key ABSENT
         from both its body and its header, permanently, because the marker for
         this version had already been written. ``run`` therefore calls this pass
         AGAIN after the loop.

    Cost once converged: one index read plus one small JSON read per tool and NO
    write — the divergence check is what gates the write. That is strictly less
    work than the sibling ``normalize_seed_forged_flags`` already does per boot
    (it hashes two files per tool).

    THE RULE IS EXACT MIRRORING, including absence. A body with no ``effect_tags``
    key is NOT CLASSIFIED, and a header that keeps a value across that is
    advertising a classification derived from an implementation the migrator has
    already replaced — a stale ``['local_read']`` on a body that is now a shell
    tool is a silent DOWNGRADE. So an absent body field clears the header field.
    Absent reads as UNKNOWN and is fail-closed at both
    ``requirement_binder._effect_tags_are_dangerous`` (empty ⇒ demand external
    verification) and ``action_governance._effective_tags`` (empty ⇒ UNKNOWN).

    A body that cannot be READ is different — that is missing evidence, not
    evidence of absence — so it is skipped and counted rather than cleared.

    ``bodies_missing_tag_key`` — WHY A PURE MIRROR CANNOT REPAIR A DAMAGED VAULT.
    Mirroring an absent body field is fail-closed but it is NOT a repair: the
    header ends up as unclassified as the body, and the tool stays UNKNOWN at
    every gate forever. That is precisely the state the pre-fix migrator left
    behind — its seed loop overwrote bodies with the packaged ones (no
    ``effect_tags`` key) on a boot whose ``.effect_tags_seed`` was already
    current, so the ONLY re-deriver, ``backfill_effect_tags``, took its fast path
    on every subsequent boot and the damage was permanent.

    MEASURED on a vault damaged by the REAL pre-fix migrator (a subprocess boot of
    1b454da2), then booted FIVE times on the fixed build at the SAME version:
    ``run_command`` and ``file_write`` stayed ABSENT in body AND header, and the
    vault held 15/41 non-empty header tag lists against 17/41 on a freshly
    migrated one. Three gates each independently prevented the repair — the
    backfill's marker, this pass's mirror-only rule, and ``run``'s own fast path
    returning before the post-loop ``force=True`` re-derive.

    So this pass COUNTS the bodies it cannot classify and returns the count.
    ``run`` reads it and orders one forced re-derive. The SIBLING counter
    ``bodies_with_empty_tags`` is the honest UNCLASSIFIED total (key present and
    empty counts too) and is REPORTED, never acted on — see ``run``. The counting
    is deliberately
    kept here (this pass already reads every body, so it costs nothing) while the
    DECISION lives in ``run`` — this function still never derives and never
    writes a tag it did not read from a body. It converges in a single extra pass
    because ``backfill_effect_tags`` writes the key onto EVERY body it processes,
    including the ones it classifies to ``[]``, so the trigger self-clears.

    File layout only, like its two siblings. The sqlite backend needs no pass:
    ``storage.sqlite.vault._tool_header`` recomputes the header from the live
    ``ToolRow`` on every ``load_index()``.

    NEVER raises — this runs on the boot path.
    """
    log = logger_ or logger
    vault_dir = Path(vault_dir)
    errors: list = []

    try:
        idx_path = vault_dir / "tools" / "index.json"
        if not idx_path.exists():
            return {"skipped": True, "reason": "no tool index"}
        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            return {"skipped": True, "reason": f"index unreadable: {exc}"}
        if not isinstance(entries, list):
            return {"skipped": True, "reason": "index not a list"}

        converged = 0
        cleared = 0
        skipped_unreadable = 0
        # F14 — TWO counters, because the old single `unclassified_bodies` answered a
        # narrower question than its name. It counted bodies MISSING THE KEY, and on
        # a real boot every body HAD the key, holding `[]`. So it logged
        # `unclassified_bodies=0` on a vault where 24 of 41 tools were unclassified —
        # true, and read by an operator as the exact opposite of the truth.
        bodies_missing_tag_key = 0   # the repair TRIGGER: `run` forces a re-derive
        bodies_with_empty_tags = 0   # the HONEST count of unclassified tools

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tid = entry.get("id")
            if not tid:
                continue
            body_path = vault_dir / "tools" / f"tool_{tid}.json"
            try:
                if not body_path.is_file():
                    continue          # no body ⇒ no evidence either way
                body = json.loads(body_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 — unreadable is NOT "absent"
                skipped_unreadable += 1
                errors.append(f"read {tid}: {exc}")
                continue
            if not isinstance(body, dict):
                skipped_unreadable += 1
                continue

            if _EFFECT_TAGS_FIELD in body:
                tags = body.get(_EFFECT_TAGS_FIELD)
                if not isinstance(tags, list):
                    skipped_unreadable += 1
                    continue
                tags = list(tags)
                if not tags:
                    # The key is PRESENT and EMPTY. That is an unclassified tool —
                    # the state the old counter could not see — so it is counted
                    # here even though there is nothing to mirror or repair.
                    bodies_with_empty_tags += 1
                if entry.get(_EFFECT_TAGS_FIELD) != tags:
                    entry[_EFFECT_TAGS_FIELD] = tags
                    converged += 1
            else:
                # THE BODY IS UNCLASSIFIED **and has no key at all**, and this pass
                # cannot fix that — it MIRRORS, it never derives. Report it so `run`
                # can order a forced re-derive; see the docstring. A missing key is
                # also an unclassified tool, so it counts toward BOTH.
                bodies_missing_tag_key += 1
                bodies_with_empty_tags += 1
                if _EFFECT_TAGS_FIELD in entry:
                    # Body says "unclassified"; the header must not say otherwise.
                    del entry[_EFFECT_TAGS_FIELD]
                    cleared += 1

        if converged or cleared:
            try:
                _write_text_atomic(idx_path, json.dumps(entries, indent=2) + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"index_write: {exc}")
                converged = cleared = 0

        if converged or cleared or bodies_with_empty_tags or errors:
            log.info(
                "[EffectTagIndexConverge] converged=%d cleared=%d "
                "bodies_with_empty_tags=%d bodies_missing_tag_key=%d "
                "skipped_unreadable=%d errors=%d",
                converged, cleared, bodies_with_empty_tags,
                bodies_missing_tag_key, skipped_unreadable, len(errors))
        return {"converged": converged, "cleared": cleared,
                "bodies_missing_tag_key": bodies_missing_tag_key,
                "bodies_with_empty_tags": bodies_with_empty_tags,
                "skipped_unreadable": skipped_unreadable, "errors": errors}
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[EffectTagIndexConverge] unexpected failure (non-fatal): %s", exc)
        return {"error": str(exc)}


_FORGED_FIELD = "forged_by_systemu"
_IMPL_PATH_FIELD = "implementation_path"


def _path_under(candidate: Path, root: Path) -> bool:
    """Path-COMPONENT containment — ``…/implementations_evil/x.py`` is NOT under
    ``…/implementations``.

    The same containment rule the confinement layer applies to path facts, kept
    LOCAL: this runs on the boot path, and the module that already implements it
    is under a test-enforced no-reference invariant. It additionally RESOLVES
    both sides first, so a symlinked implementation cannot sit inside the
    directory while pointing outside it. A raw ``startswith`` would accept a
    prefix-named sibling — this repo has already shipped that bug once
    (``C:/Radiology/x`` counted as inside ``C:/R``). Never raises; an
    unresolvable path is simply not contained.
    """
    try:
        c = candidate.resolve()
        r = root.resolve()
        return c == r or r in c.parents
    except Exception:  # noqa: BLE001
        return False


def _resolve_vault_impl(vault_dir: Path, impl_dir: Path, name: str,
                        declared: Any) -> Path | None:
    """Which file does this vault tool ACTUALLY execute? Returns None to REFUSE.

    ``implementation_path`` is the field the runtime loads
    (``ToolSandbox.execute_tool``), and a relative value is anchored at the vault
    root's PARENT — not at the implementations dir. That is how the value is
    written (``tool_forge``: ``impl_path.relative_to(vault_dir.parent)``) and how
    it is read back (``tool_dry_run._resolve_impl_path``), which is why every
    shipped seed body carries ``vault/tools/implementations/<name>.py``. Anchoring
    that string at ``impl_dir`` instead would resolve to a path that does not
    exist and silently skip every seed.

    REFUSES (returns None) when the declared value is present but not a string,
    or when it resolves outside the vault's implementations dir — including via
    ``..`` segments, a symlink, or an absolute path. Falls back to ``{name}.py``
    only when the field is absent or blank; that is the same fallback
    ``backfill_effect_tags`` uses, and an absent field is not evidence of
    tampering. Never raises — a malformed body is a refusal, not a boot failure.
    """
    try:
        if declared is None or (isinstance(declared, str) and not declared.strip()):
            candidate = impl_dir / f"{name}.py"
        elif isinstance(declared, str):
            p = Path(declared)
            candidate = p if p.is_absolute() else (vault_dir.parent / p)
        else:
            return None      # non-string ⇒ malformed body; prove nothing about it
        if not _path_under(candidate, impl_dir):
            return None
        return candidate.resolve()
    except Exception:  # noqa: BLE001
        return None


def normalize_seed_forged_flags(vault_dir, *, logger_=None) -> Dict[str, Any]:
    """Re-label a vault's SEED tools with the package's ``forged_by_systemu``
    value — but ONLY where the vault's implementation is byte-identical to the
    shipped one.

    ``forged_by_systemu`` means "an LLM authored this body". Vaults seeded from
    the original prototype export carry ``true`` on 39 shipped seed tools that
    are in fact repo code. Two controls key on the flag and both misfire on that
    mis-flag: ``action_governance.forged_network_denied`` HARD-DENIES the seeded
    network tools (``fetch_html``/``fetch_json``/``api_call_get``/
    ``download_file``) as un-jailed forged egress, and
    ``tool_sandbox._command_gate_already_scored`` refuses ``run_command`` the
    command-gate carve-out. Both consumers' own docstrings state the intended
    posture for a built-in: gated, not denied.

    WHY A SHA GUARD, NOT A BLANKET FLIP: this RELAXES a deny, so "vetted" has to
    be VERIFIED rather than asserted. A name match alone proves nothing — a forge
    picks its own ``name`` and its own impl filename. The only evidence that a
    vault tool IS the repo's tool is that its bytes are the repo's bytes. So the
    flag moves only on ``sha256(vault impl) == sha256(package impl)``; an
    operator-edited or forge-substituted body keeps ``forged=true`` and stays
    denied/gated. Fail closed: anything we cannot positively clear is left alone.

    A sha guard only holds if it hashes the RIGHT FILE and writes to the RIGHT
    RECORD, so identity is resolved before any hashing:

      * AMBIGUOUS NAMES ARE REFUSED. ``vault._update_index`` upserts on ``id``,
        so two index entries may share a ``name`` — ``governor._materialise_forge``
        builds a Tool straight from the LLM-supplied ``spec["name"]`` with a
        fresh id and no existing-name check. A last-wins ``{name: entry}`` dict
        picked one of them, hashed ``implementations/{name}.py``, then wrote the
        flag to the SELECTED entry's body: "vetted" onto a record never hashed.
        Where more than one entry carries the name we now skip it entirely and
        count ``skipped_ambiguous`` — there is no evidence identifying the seed,
        and picking a winner IS the bug.

      * THE HASHED FILE IS THE ONE THAT EXECUTES. ``tool.implementation_path``
        is what ``ToolSandbox.execute_tool`` loads, so a body declaring
        ``payload.py`` while a pristine ``{name}.py`` sits beside it must not be
        cleared. The declared path is resolved the way the runtime resolves it
        (relative ⇒ anchored at the vault root's PARENT) and must land INSIDE
        ``vault/tools/implementations/`` by path-component containment after
        symlink resolution; anything else counts ``skipped_impl_path``.

    Runs on EVERY boot, BEFORE ``run``'s ``installed == vault_seed`` fast path —
    this fix ships without a version bump, so an existing vault's
    ``.seed_version`` already matches and anything behind that return would never
    execute in a real operator's vault. Mirrors ``backfill_effect_tags``'s
    fast-path-independent invocation. Idempotent: once converged every tool is a
    pair of sha compares (~41 per boot) and no write. NEVER raises — a
    normalization failure must not break boot.
    """
    log = logger_ or logger
    vault_dir = Path(vault_dir)
    errors: list = []

    try:
        try:
            pkg_vault = _package_vault_root()
            pkg_idx = json.loads(
                (pkg_vault / "tools" / "index.json").read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            return {"skipped": True, "reason": f"package index unreadable: {exc}"}

        vault_idx_path = vault_dir / "tools" / "index.json"
        if not vault_idx_path.exists():
            return {"skipped": True, "reason": "no vault tool index"}
        try:
            vault_idx = json.loads(vault_idx_path.read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            return {"skipped": True, "reason": f"vault index unreadable: {exc}"}

        # Identity by NAME — the migrator's own rule (see module docstring) — but
        # a name is NOT a key. `vault._update_index` upserts on `id`, so a vault
        # index can legitimately hold TWO entries under one name, and a last-wins
        # `{name: entry}` dict would silently pick one of them. Collect every
        # match instead and refuse the ambiguous ones below.
        vault_by_name: Dict[str, list] = {}
        for _e in vault_idx:
            _n = _e.get("name")
            if _n:
                vault_by_name.setdefault(_n, []).append(_e)
        pkg_impl_dir = pkg_vault / "tools" / "implementations"
        vault_impl_dir = vault_dir / "tools" / "implementations"

        normalized = 0
        index_normalized = 0
        skipped_modified = 0
        skipped_ambiguous = 0
        skipped_impl_path = 0

        for pkg_entry in pkg_idx:
            name = pkg_entry.get("name")
            pkg_tid = pkg_entry.get("id")
            if not name or not pkg_tid:
                continue
            matches = vault_by_name.get(name) or []
            if not matches:
                continue  # not installed here, or a user tool the package never shipped
            if len(matches) > 1:
                # AMBIGUOUS. Two records claim this name and nothing here is
                # evidence of which one is the seed — so there is no "right" one
                # to pick, only a guess. Guessing is precisely the defect: the
                # guard hashed `{name}.py` but wrote the flag to the SELECTED
                # entry's body, which let a forge that named itself after a seed
                # collect a "vetted" label for a body that was never hashed.
                # Refuse the whole name; fail closed (see this docstring's rule).
                skipped_ambiguous += 1
                continue
            vault_entry = matches[0]
            try:
                # The BODY comes first now: it declares which file EXECUTES, and
                # that is the file whose bytes have to match the package's. The
                # vault may hold this seed under its OWN id (identity is by name),
                # so resolve the body through the vault's index entry.
                vault_tid = vault_entry.get("id") or pkg_tid
                vault_body_path = vault_dir / "tools" / f"tool_{vault_tid}.json"
                if not vault_body_path.exists():
                    continue  # no body ⇒ nothing to verify and nothing to write
                vault_body = json.loads(vault_body_path.read_text(encoding="utf-8"))

                # Hash what RUNS, not what the name implies. Only the vault side
                # is attacker-influenced; the package index ships with the code,
                # so the package impl stays `{name}.py`.
                vault_impl = _resolve_vault_impl(
                    vault_dir, vault_impl_dir, name, vault_body.get(_IMPL_PATH_FIELD))
                if vault_impl is None:
                    skipped_impl_path += 1
                    continue

                pkg_impl = pkg_impl_dir / f"{name}.py"
                # is_file(), not exists(): a declared path may name the impl
                # DIRECTORY itself, which is inside the dir and so passes
                # containment — hashing it would just raise into the per-tool
                # handler. Not a file ⇒ no provenance to prove.
                if not (pkg_impl.is_file() and vault_impl.is_file()):
                    continue  # cannot prove provenance ⇒ leave the flag as-is
                if _file_sha256(vault_impl) != _file_sha256(pkg_impl):
                    # Operator- or forge-modified body: NOT the repo's code.
                    skipped_modified += 1
                    continue

                pkg_body_path = pkg_vault / "tools" / f"tool_{pkg_tid}.json"
                if not pkg_body_path.exists():
                    continue
                pkg_flag = bool(
                    json.loads(pkg_body_path.read_text(encoding="utf-8")).get(
                        _FORGED_FIELD, False))

                if bool(vault_body.get(_FORGED_FIELD, False)) != pkg_flag:
                    vault_body[_FORGED_FIELD] = pkg_flag  # ONLY this field
                    _write_text_atomic(
                        vault_body_path, json.dumps(vault_body, indent=2) + "\n")
                    normalized += 1

                # The index header is DERIVED from the body (vault._tool_header);
                # keep the two in agreement or the dashboard/table facets read a
                # stale label off the header.
                if bool(vault_entry.get(_FORGED_FIELD, False)) != pkg_flag:
                    vault_entry[_FORGED_FIELD] = pkg_flag
                    index_normalized += 1
            except Exception as exc:  # noqa: BLE001 — never raise per file
                errors.append(f"normalize {name}: {exc}")

        if index_normalized:
            try:
                _write_text_atomic(vault_idx_path, json.dumps(vault_idx, indent=2) + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"vault_index_write: {exc}")
                index_normalized = 0

        # Log the REFUSALS too: a seed left denied because its name is ambiguous
        # or its declared impl path points outside the vault is a state the
        # operator has to be able to see, or they just get an unexplained deny.
        if (normalized or index_normalized or skipped_ambiguous
                or skipped_impl_path or errors):
            log.info(
                "[SeedForgedNormalize] bodies=%d index=%d skipped_modified=%d "
                "skipped_ambiguous=%d skipped_impl_path=%d errors=%d",
                normalized, index_normalized, skipped_modified,
                skipped_ambiguous, skipped_impl_path, len(errors))
        return {
            "normalized": normalized,
            "index_normalized": index_normalized,
            "skipped_modified": skipped_modified,
            "skipped_ambiguous": skipped_ambiguous,
            "skipped_impl_path": skipped_impl_path,
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[SeedForgedNormalize] unexpected failure (non-fatal): %s", exc)
        return {"error": str(exc)}


_PRE_MIGRATION_DIRNAME = ".pre-migration"


def _diverges(dest: Path, source: Path) -> bool:
    """True when ``dest`` exists AND its bytes differ from ``source``'s — i.e. an
    overwrite would actually lose something.

    Keeps ``preserved`` meaning "you had local edits". Without it, every updated
    seed's body JSON is copied aside on every upgrade, so an operator who edited
    NOTHING still gets a non-zero count and a warning naming their vault. Across
    41 seeds that is a false alarm big enough to train people to ignore the real
    one, which would defeat the point of surfacing the count at all.

    Fail-SAFE direction: an existing-but-unhashable ``dest`` returns True, so the
    file is preserved anyway (and if preservation then fails, the overwrite is
    skipped). Never report "no divergence" from missing evidence.
    """
    try:
        if not dest.is_file():
            return False          # nothing there to lose
        if not source.is_file():
            return True           # replacing with something unreadable — keep a copy
        return _file_sha256(dest) != _file_sha256(source)
    except Exception:  # noqa: BLE001
        return True


def _safe_version_label(version: Any) -> str:
    """Filesystem-safe path segment from a version string.

    ``.seed_version`` is FILE CONTENT — corruptible, operator-writable, and read
    on the boot path — and this turns it into a PATH SEGMENT. Anything that could
    escape the backup root (``..``, ``/``, ``\\``, a drive letter, a NUL) is
    replaced rather than trusted, and a value that survives as empty / ``.`` /
    ``..`` degrades to ``unknown``. Length-capped so a garbage marker cannot
    blow the path limit.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(version))
    return (safe.strip(".")[:64] or "unknown")


_ENABLED_FIELD = "enabled"


def _operator_disabled(vault_dir: Path, vault_entry: Any, dest_body: Path) -> bool:
    """Has this vault got the tool explicitly switched OFF?

    ``enabled`` is Gate 3's input — ``tool_registry.execute`` does
    ``if not tool.enabled: raise ToolNotEnabledError`` and tells the operator to
    "flip the toggle ON" in the dashboard, so it is a deliberate operator
    control (the dry-run reconciler also auto-clears it after a failure). Every
    packaged seed ships ``enabled: true``, and the update branch copies the
    package body over the vault's, so without this the migration silently
    re-arms tools that were switched off.

    ONLY AN EXPLICIT ``False`` COUNTS. Absent is not evidence: a vault predating
    the field has no value, and ``Vault._backfill_tool_index_enabled`` writes
    ``False`` whenever it cannot find a body — reading "absent or falsey" as
    intent would latch tools off that nobody had touched.

    BOTH RECORDS ARE CONSULTED, and either one is enough. Identity here is by
    NAME, so the vault may hold the seed under its OWN id: its body is
    ``tool_<vault id>.json`` while the file the migration overwrites is
    ``tool_<package id>.json``. Checking only one of them would miss the disable
    exactly when the two ids disagree.

    UNREADABLE ⇒ DISABLED, deliberately. This is mutation S12's shape: ``_diverges``
    once returned ``False`` from its ``except``, so a file that EXISTS but cannot be
    hashed read as "nothing to lose" and was clobbered. Missing evidence must fall to
    the restrictive side. A tool wrongly left off costs one dashboard click and Gate 3
    names it in the error; a tool wrongly switched back on is a control revoked in
    silence. A body that is simply ABSENT is different — there is no tool there to
    have an opinion about — and reads as not-disabled.

    THE TWO HANDLERS POINT OPPOSITE WAYS ON PURPOSE; do not "make them
    consistent". The inner one is per-FILE and evidence-bearing: this tool's
    record exists and is unreadable, so this tool stays off — blast radius one.
    The outer one catches a structural failure of this function itself, which
    would hit every seed in the loop; resolving that to "disabled" would take a
    bug here and turn it into all 41 seed tools switched off at once, so it
    degrades to the pre-existing behaviour (the package's value wins) instead.
    Restrictive where the evidence is specific, status-quo where it is not.
    """
    def _says_off(p: Path) -> bool:
        try:
            if not p.is_file():
                return False          # no record ⇒ no opinion
        except Exception:  # noqa: BLE001 — an unstattable path is not evidence
            return False
        try:
            return json.loads(p.read_text(encoding="utf-8")).get(
                _ENABLED_FIELD) is False
        except Exception:  # noqa: BLE001 — present but unreadable ⇒ fail closed
            return True

    try:
        if isinstance(vault_entry, dict):
            if vault_entry.get(_ENABLED_FIELD) is False:
                return True
            vault_tid = vault_entry.get("id")
            if vault_tid and _says_off(
                    vault_dir / "tools" / f"tool_{vault_tid}.json"):
                return True
        return _says_off(dest_body)
    except Exception:  # noqa: BLE001 — never break boot; the copy still happens
        return False


def _entry_keeping_disable(pkg_entry: Dict[str, Any], disabled: bool) -> Dict[str, Any]:
    """The package's index header, with an operator DISABLE carried across.

    A COPY, not the package entry itself: ``pkg_entry`` is an element of the
    package index still being iterated, and mutating it in place would leak this
    vault's state into the authoritative catalog for the rest of the run.

    Only ``enabled`` is carried. The other five fields a live header has and the
    packaged one lacks — ``dry_run_status``, ``version``,
    ``parameters_schema_summary``, ``return_schema_summary``,
    ``implementation_path`` — are all DERIVED from the tool body by
    ``vault._tool_header`` and are re-derived by
    ``jobs._backfill_tool_headers_v061`` on the next boot (measured). Carrying
    two of them would be actively wrong: the schema summaries would describe the
    OLD schema while the body is now the package's, and ``implementation_path``
    is vault-declared — re-stamping it would carry a redirected path across a
    migration that just replaced ``{name}.py``.
    """
    entry = dict(pkg_entry)
    if disabled:
        entry[_ENABLED_FIELD] = False
    return entry


def _install_body(pkg_body: Path, dest_body: Path, disabled: bool) -> None:
    """Write the package's tool body, carrying an operator disable across.

    Patched IN MEMORY and written once, rather than ``copy2`` then re-stamp:
    a copy-then-patch leaves the tool momentarily enabled on disk, and a failure
    between the two would leave it enabled permanently. Raising instead lands in
    the caller's per-tool ``except``, which leaves the vault's existing (still
    disabled) body in place — the fail-closed direction, and the same posture
    ``_preserve_before_overwrite`` takes.
    """
    if not disabled:
        shutil.copy2(pkg_body, dest_body)
        return
    body = json.loads(pkg_body.read_text(encoding="utf-8"))
    body[_ENABLED_FIELD] = False
    _write_text_atomic(dest_body, json.dumps(body, indent=2) + "\n")


def _preserve_before_overwrite(target: Path, backup_root: Path, relname: str) -> None:
    """Copy ``target`` aside before the migrator overwrites it. RAISES on failure.

    Raising is the point. Every caller sits inside the per-tool ``try`` whose
    ``except`` records an error and moves on, and the ``shutil.copy2`` that
    destroys the file comes AFTER this call — so a failed preservation SKIPS the
    overwrite instead of proceeding with it. A stale seed tool (the status quo
    for the last 22 releases) is strictly better than unrecoverable loss of an
    operator's edits.

    WHY THIS EXISTS: ``run``'s update branch treats identity by NAME, so a seed
    tool the operator EDITED has the same name and different bytes — the exact
    shape that falls through to ``copy2`` and is clobbered with nothing retained.
    Forged tools are excluded by name, but an edited SEED never was. The version
    single-sourcing fix re-opens this gate after 22 releases, which is what turns
    a latent hazard into a first-boot event.

    WHY THE BACKUP LIVES OUTSIDE ``tools/implementations/``: files under that
    directory pass ``_resolve_vault_impl``'s containment check, so a tool body
    declaring ``implementation_path`` pointing at a preserved copy would EXECUTE
    it. Keeping backups at ``<vault>/.pre-migration/<from_version>/`` puts them
    where that containment REFUSES them, so preserved operator code can never be
    reached as an implementation.
    """
    dest = backup_root / relname
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Re-running the migrator at the same from-version must not overwrite the
        # backup with the already-migrated file. The FIRST copy is the true
        # pre-migration state; keep it.
        return
    shutil.copy2(target, dest)


# ─────────────────────────────────────────────────────────────────────────────
#  F26 — MANIFEST CONVERGENCE
# ─────────────────────────────────────────────────────────────────────────────

_MANIFEST_BASELINE_FILENAME = ".package_manifest_baseline.json"
_MANIFEST_BOOTSTRAP_DIRNAME = "manifest-bootstrap"

# THE FIELDS THE PACKAGE OWNS. Exactly the DESCRIPTIVE SPEC an author writes in the
# shipped catalog — nothing that describes what this vault has DONE with the tool.
#
# Derived from the packaged bodies rather than from the `Tool` model: all 41 carry
# the same 16 keys, and the other ten are identity (`id`, `name`), paths
# (`implementation_path`, `tool_md_path`), lifecycle (`status`, `enabled`,
# `version`) or provenance (`forged_by_systemu`, `created_at`, `updated_at`).
#
# WHAT IS DELIBERATELY NOT HERE, and why each one is somebody else's:
#   enabled            Gate 3's input. `tool_registry.execute` refuses a disabled
#                      tool; re-arming one is a revoked control, not a migration.
#                      `_operator_disabled` already exists to protect it.
#   status /
#   dry_run_status /
#   dry_run_evidence   Gate 3.5's verdict and the evidence behind it. Facts about
#                      THIS vault's run of the tool.
#   version /
#   evolution_history /
#   last_successful_params /
#   forge_reattempts /
#   forge_rejected     Per-vault evolution state.
#   trusted_inprocess  W2.2 operator opt-in.
#   effect_tags        Owned by `backfill_effect_tags`, which DERIVES it from the
#                      tool's source. One writer. The packaged bodies do not even
#                      carry the key, so "refreshing" it would mean clearing it.
#   forged_by_systemu  Owned by `normalize_seed_forged_flags`. One writer.
#   implementation_path / tool_md_path
#                      Paths. `implementation_path` selects WHAT EXECUTES, and the
#                      packaged value is a literal string from the build machine —
#                      re-stamping it is a redirect, not a description.
#   created_at / updated_at / id / name
#                      Identity and provenance of the vault's own record; `name` is
#                      this pass's match key.
_PACKAGE_MANIFEST_FIELDS = (
    "description",
    "tool_type",
    "parameters_schema",
    "return_schema",
    "implementation_notes",
    "dependencies",
)

# WHY THE HEADER IS MIRRORED AT ALL. `vault._tool_header` derives every one of
# these from the body, and `Vault.list_tools()` returns the HEADER list — so
# `systemu tools list`, `capability_index.derive_index` and
# `table_reconciler._project_tools` all read the header and NONE of them read the
# body. A pass that fixed only bodies would be invisible to the exact surface that
# reported F26. See `_header_mirror`.


def _load_manifest_baseline(vault_dir: Path) -> Dict[str, Any]:
    """Per tool name, a FINGERPRINT of what the package last wrote per field.

    Never raises → ``{}`` on anything, which degrades to the bootstrap path (which
    preserves before it writes), not to a wrong decision."""
    try:
        p = vault_dir / _MANIFEST_BASELINE_FILENAME
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt baseline is "no baseline"
        return {}


def _field_digest(value: Any) -> str:
    """A stable fingerprint of one manifest field value, or ``""`` on failure.

    DIGESTS, NOT VALUES — for CORRECTNESS, not size. This file exists ONLY to
    answer "has this field moved since the package wrote it?". Storing the values
    would put a second full copy of the shipped manifest inside the vault: DEC-43's
    one-fact-N-copies shape, and a file a later reader could mistake for an
    authority and read a tool's real description or schema out of. A digest cannot
    be mistaken for one. (Size is NOT the argument and is nearly a wash — measured
    on the real 41-tool catalog: 35.5 KB of values against 23.1 KB of digests.)

    ``sort_keys`` so a schema dict that round-trips through JSON in a different
    key order does not read as an operator edit. Returns ``""`` when the value
    cannot be canonicalised at all; the caller treats that as "no evidence" and
    leaves the field alone, which is the non-destructive direction.
    """
    try:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def _header_mirror(body: Dict[str, Any]) -> Dict[str, Any]:
    """The header fields this pass owns, derived FROM THE REFRESHED BODY exactly the
    way ``vault._tool_header`` derives them.

    DERIVED, NOT COPIED FROM THE PACKAGED INDEX — three reasons, all measured:

      1. ``_tool_header`` is the canonical producer and it reads the Tool, i.e. the
         BODY. Copying the packaged header would install a value the next
         ``vault.save_tool`` immediately overwrites, and this pass would put it back
         on the following boot: a write loop, not a convergence.
      2. THE SHIPPED CATALOG DOES NOT AGREE WITH ITSELF. ``web_search``'s packaged
         header carries a SHORTER description than its packaged body. F25 pins
         agreement for ``dependencies`` across the three copies; nothing pins it for
         prose. Deriving from the body picks the one authority and heals the drift
         instead of propagating whichever copy was consulted.
      3. ``tool_type`` needs the model's own coercion. Bodies ship the synonym
         ``"web"``; ``Tool.tool_type`` coerces it to ``api_call`` and ``_tool_header``
         emits the coerced value. Writing the raw synonym would flip-flop against
         every real save.

    Returns ``(write, expected_summaries)`` — header keys to SET, and the freshly
    derived schema summaries, which the caller uses only to decide whether a stored
    summary has gone stale and must be DELETED. See the long note below.
    """
    out: Dict[str, Any] = {}
    if "description" in body:
        out["description"] = body["description"]
    if "dependencies" in body:
        out["dependencies"] = list(body.get("dependencies") or [])
    if "tool_type" in body:
        try:
            from systemu.core.models import coerce_tool_type
            out["tool_type"] = coerce_tool_type(body["tool_type"]).value
        except Exception:  # noqa: BLE001 — no coercion available ⇒ leave it alone
            pass
    try:
        from systemu.core.schema_utils import schema_param_names
        out["parameter_names"] = schema_param_names(body.get("parameters_schema") or {})
    except Exception:  # noqa: BLE001
        pass

    # THE TWO SCHEMA SUMMARIES ARE INVALIDATED, NOT REWRITTEN — and the difference
    # is a regression this pass caused and had to back out of.
    #
    # A corrected `parameters_schema` leaves the header's summary describing the OLD
    # schema, and `jobs._backfill_tool_headers_v061` cannot see that: its trigger is
    # `if all("parameters_schema_summary" in t for t in tools_index): return`, i.e.
    # it only ever repairs an ABSENT summary. So this pass has to act.
    #
    # It first WROTE the freshly-derived value, which is where it went wrong.
    # Filling that key in on every seed header made the sweep's guard true, and the
    # sweep is ALSO the only repair for the other three header fields the seed loop
    # drops (`dry_run_status`, `version`, `implementation_path`). MEASURED: with the
    # summaries written, `test_dropped_header_fields_self_heal_on_next_boot` failed
    # with those three permanently missing. A fix that disarms somebody else's
    # repair is not a fix.
    #
    # Deleting instead leaves `vault._tool_header` the SINGLE writer of every
    # derived header field — no second derivation to drift, no flip-flop between
    # boots — and ARMS the sweep rather than disarming it. Absent is also the safe
    # side: the sweep's own trigger treats absent as "repair me", whereas a stale
    # summary is silently wrong. The window is the same one an updated seed has
    # always had.
    #
    # Deleted only when the stored value actually disagrees with the body, so a
    # converged vault writes nothing and this cannot loop.
    try:
        from systemu.vault.vault import _summarise_schema
        expected: Dict[str, Any] = {
            "parameters_schema_summary": _summarise_schema(
                body.get("parameters_schema") or {}),
            "return_schema_summary": _summarise_schema(body.get("return_schema") or {}),
        }
    except Exception:  # noqa: BLE001 — no canonical summariser ⇒ prove nothing
        expected = {}
    return out, expected


def converge_package_manifest(vault_dir, *, logger_=None) -> Dict[str, Any]:
    """F26 — bring an EXISTING vault's SEED tool records back onto the shipped
    catalog for the fields the PACKAGE owns, and leave everything the operator
    owns exactly where it is.

    THE DEFECT. ``run``'s seed loop decides everything from one comparison:
    ``sha256(vault impl) == sha256(pkg impl)``. Correcting a manifest field alone —
    a dependency list, a description, a parameter schema — leaves the ``.py``
    byte-identical, so the tool lands in ``skipped_identical`` and its record is
    never opened. Measured on the real catalog: a vault whose
    ``tool_tool_web_read.json`` declared ``dependencies: ["playwright"]`` (wrong,
    corrected in-package by F25) came out of ``run()`` with ``skipped_identical=41``
    and the stale list intact, so ``systemu tools list`` kept reporting the tool
    UNAVAILABLE and refusing it. That made the shipped starter catalog effectively
    IMMUTABLE after first seed — a whole class of silent staleness, not one field.

    WHY THIS RUNS AHEAD OF ``run``'s FAST PATH, like its three siblings. This
    project's live-tryout rule folds fixes into the CURRENT version without a bump,
    so ``installed == vault_seed`` on every already-booted vault and anything behind
    that return never executes where the bug lives.

    ─── WHAT IT REFUSES TO TOUCH ───────────────────────────────────────────────
    A vault is USER DATA, so convergence is gated three ways and every refusal is
    counted rather than silent:

      * NAME NOT IN THE PACKAGE — an operator-forged tool. Never considered; this
        pass only ever iterates the PACKAGE index.
      * AMBIGUOUS NAME — ``vault._update_index`` upserts on ``id``, so two entries
        may share a ``name`` (``governor._materialise_forge`` builds a Tool straight
        from the LLM-supplied name with a fresh id). Picking a winner IS the bug
        ``normalize_seed_forged_flags`` already had; refuse the whole name.
      * IMPLEMENTATION NOT THE PACKAGE'S — ``sha256`` mismatch on the file that
        actually EXECUTES (resolved by ``_resolve_vault_impl``, so a body declaring
        a redirected path is judged on the redirect). The record then describes the
        OPERATOR's code and the package has no standing to rewrite it. This also
        covers ``tool_recalibrator``, which always writes new code to the impl path
        BEFORE it edits description / schemas — so a recalibrated tool is excluded
        by construction.

    ``enabled`` is untouched by definition: it is not in ``_PACKAGE_MANIFEST_FIELDS``.
    A disabled tool's manifest IS still repaired, deliberately — a disable is a
    statement about RUNNING the tool, not about its description, and refusing to
    repair it would leave a stale island the operator could never clear.

    NO APPROVAL MOVES. ``command_approvals.tool_signature`` binds a blessing to
    ``(name, sha1(implementation bytes), effect tags, host_class)``. This pass
    changes none of those four: the implementation is byte-identical to the
    package's by precondition, the name is the match key, and ``effect_tags``
    belongs to ``backfill_effect_tags``. So a refresh can neither invalidate a
    standing approval nor carry one onto new code — there is no new code.

    ─── THE PART A SHA GUARD CANNOT DO ─────────────────────────────────────────
    The sha proves the CODE is the package's. It does NOT prove the MANIFEST is:
    ``interface/components/entity_edit.apply_tool_edit`` writes ``description`` /
    ``implementation_notes`` / ``dependencies`` from the workshop dialog and never
    touches the ``.py``. That edit is structurally invisible to a byte comparison of
    the implementation.

    So this pass FINGERPRINTS what the PACKAGE wrote, per tool per field, in
    ``.package_manifest_baseline.json`` (digests, not values — see
    ``_field_digest``). From then on the question is decidable per FIELD:

        digest(vault value) == baseline   → nobody has touched it since the
                                            package wrote it ⇒ refresh
        digest(vault value) != baseline   → the OPERATOR changed it ⇒ leave it,
                                            for ever, across every future
                                            correction (``skipped_operator_edited``)

    THE ONE BOOT THAT IS GENUINELY UNDECIDABLE is the first one, on a vault that
    predates the baseline — which is every vault F26 exists for. There is no third
    version to diff against, so the choice is between converging (the whole point)
    and never converging (the bug). It converges, and copies the pre-refresh body
    to ``<vault>/.pre-migration/manifest-bootstrap/`` first, so that single
    ambiguous decision is RECOVERABLE instead of silent. Outside
    ``tools/implementations/`` for the reason ``_preserve_before_overwrite``
    documents: a preserved copy must never be reachable as an implementation.

    EVERY COUNTER IS A COUNT OF WRITES THAT HAPPENED. ``manifest_fields_refreshed``
    is incremented per (tool, field) pair whose value actually differed and only
    after the write succeeded — never per record considered. That is the F14/F26
    lesson: ``stamped=41`` and ``updated=41`` were both counts of work ATTEMPTED
    being read as work DONE.

    File layout only, like its siblings — ``storage.sqlite.vault._tool_header``
    recomputes headers from the live row. NEVER raises; this is the boot path.
    """
    log = logger_ or logger
    vault_dir = Path(vault_dir)
    errors: list = []
    result: Dict[str, Any] = {
        "manifest_tools_refreshed": 0, "manifest_fields_refreshed": 0,
        "manifest_headers_refreshed": 0, "manifest_skipped_operator_edited": 0,
        "manifest_skipped_modified": 0, "manifest_skipped_ambiguous": 0,
        "manifest_skipped_impl_path": 0, "manifest_preserved": 0, "errors": errors,
    }

    try:
        try:
            pkg_vault = _package_vault_root()
            pkg_idx = json.loads(
                (pkg_vault / "tools" / "index.json").read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            result["skipped"] = f"package index unreadable: {exc}"
            return result

        vault_idx_path = vault_dir / "tools" / "index.json"
        if not vault_idx_path.is_file():
            result["skipped"] = "no vault tool index"
            return result
        try:
            vault_idx = json.loads(vault_idx_path.read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            result["skipped"] = f"vault index unreadable: {exc}"
            return result
        if not isinstance(vault_idx, list):
            result["skipped"] = "vault index not a list"
            return result

        # Identity by NAME (the migrator's rule), but a name is NOT a key — collect
        # every match and refuse the ambiguous ones. Same posture as
        # `normalize_seed_forged_flags`.
        vault_by_name: Dict[str, list] = {}
        for e in vault_idx:
            if isinstance(e, dict) and e.get("name"):
                vault_by_name.setdefault(e["name"], []).append(e)

        pkg_impl_dir = pkg_vault / "tools" / "implementations"
        vault_impl_dir = vault_dir / "tools" / "implementations"
        baseline = _load_manifest_baseline(vault_dir)
        new_baseline: Dict[str, Any] = dict(baseline)
        bootstrap_root = (vault_dir / _PRE_MIGRATION_DIRNAME
                          / _MANIFEST_BOOTSTRAP_DIRNAME)
        index_dirty = False

        for pkg_entry in pkg_idx:
            if not isinstance(pkg_entry, dict):
                continue
            name = pkg_entry.get("name")
            pkg_tid = pkg_entry.get("id")
            if not name or not pkg_tid:
                continue
            matches = vault_by_name.get(name) or []
            if not matches:
                continue                       # not installed in this vault
            if len(matches) > 1:
                result["manifest_skipped_ambiguous"] += 1
                continue
            vault_entry = matches[0]

            try:
                pkg_body_path = pkg_vault / "tools" / f"tool_{pkg_tid}.json"
                if not pkg_body_path.is_file():
                    continue
                pkg_body = json.loads(pkg_body_path.read_text(encoding="utf-8"))
                if not isinstance(pkg_body, dict):
                    continue

                vault_tid = vault_entry.get("id") or pkg_tid
                body_path = vault_dir / "tools" / f"tool_{vault_tid}.json"
                if not body_path.is_file():
                    continue                   # no record here to converge
                body = json.loads(body_path.read_text(encoding="utf-8"))
                if not isinstance(body, dict):
                    continue

                # PROVENANCE. Hash what RUNS — a body declaring a redirected
                # `implementation_path` is judged on the redirect, and anything
                # landing outside `vault/tools/implementations/` is refused.
                vault_impl = _resolve_vault_impl(
                    vault_dir, vault_impl_dir, name, body.get(_IMPL_PATH_FIELD))
                if vault_impl is None:
                    result["manifest_skipped_impl_path"] += 1
                    continue
                pkg_impl = pkg_impl_dir / f"{name}.py"
                if not (pkg_impl.is_file() and vault_impl.is_file()):
                    continue                   # cannot prove provenance ⇒ leave alone
                if _file_sha256(vault_impl) != _file_sha256(pkg_impl):
                    result["manifest_skipped_modified"] += 1
                    continue

                prior = baseline.get(name) if isinstance(
                    baseline.get(name), dict) else None
                pkg_values = {f: pkg_body[f] for f in _PACKAGE_MANIFEST_FIELDS
                              if f in pkg_body}

                pending: Dict[str, Any] = {}
                for field, pkg_val in pkg_values.items():
                    cur = body.get(field)
                    if cur == pkg_val:
                        continue               # already converged; nothing to write
                    if prior is not None and field in prior:
                        cur_digest = _field_digest(cur)
                        if not cur_digest or prior[field] != cur_digest:
                            # Either the operator moved this field away from what
                            # the package last wrote — it is theirs now, for ever —
                            # or the value cannot be fingerprinted at all, which is
                            # missing evidence and resolves the same way: no write.
                            result["manifest_skipped_operator_edited"] += 1
                            continue
                    pending[field] = pkg_val

                # The baseline fingerprints what the PACKAGE ships, whether or not
                # this boot had to write anything — a field the operator has already
                # matched by hand is still package-authored as far as the next
                # correction is concerned.
                new_baseline[name] = {f: _field_digest(v)
                                      for f, v in pkg_values.items()}

                if pending:
                    if prior is None:
                        # UNDECIDABLE BOOT — keep the pre-refresh record. Raises on
                        # failure, which lands in the per-tool handler below and
                        # SKIPS the write (fail-safe, same posture as
                        # `_preserve_before_overwrite`'s existing callers).
                        _preserve_before_overwrite(
                            body_path, bootstrap_root, f"tools/tool_{vault_tid}.json")
                        result["manifest_preserved"] += 1
                    body.update(pending)
                    _write_text_atomic(body_path, json.dumps(body, indent=2) + "\n")
                    result["manifest_fields_refreshed"] += len(pending)
                    result["manifest_tools_refreshed"] += 1

                # The header is DERIVED from the body and is what every live reader
                # actually reads. Mirror it whether or not the body moved: a vault
                # can hold a converged body behind a stale header.
                mirror, expected_summaries = _header_mirror(body)
                changed_header = False
                for k, v in mirror.items():
                    if vault_entry.get(k) != v:
                        vault_entry[k] = v
                        changed_header = True
                for k, v in expected_summaries.items():
                    # Present AND wrong ⇒ drop it, so `_tool_header` (via
                    # `jobs._backfill_tool_headers_v061`) re-derives it as the one
                    # writer of derived header fields. Absent already means
                    # "repair me" to that sweep, so leave an absent key alone.
                    if k in vault_entry and vault_entry.get(k) != v:
                        del vault_entry[k]
                        changed_header = True
                if changed_header:
                    result["manifest_headers_refreshed"] += 1
                    index_dirty = True
            except Exception as exc:  # noqa: BLE001 — never raise per tool
                errors.append(f"manifest {name}: {exc}")

        if index_dirty:
            try:
                _write_text_atomic(vault_idx_path,
                                   json.dumps(vault_idx, indent=2) + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"index_write: {exc}")
                # The count has to follow the DISK. A header write that failed
                # changed nothing, so reporting it would be the F26 lie again.
                result["manifest_headers_refreshed"] = 0

        if new_baseline != baseline:
            try:
                _write_text_atomic(vault_dir / _MANIFEST_BASELINE_FILENAME,
                                   json.dumps(new_baseline, indent=2, sort_keys=True)
                                   + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"baseline_write: {exc}")

        if (result["manifest_fields_refreshed"] or result["manifest_headers_refreshed"]
                or result["manifest_skipped_ambiguous"]
                or result["manifest_skipped_impl_path"] or errors):
            log.info(
                "[ManifestConverge] tools_refreshed=%d fields_refreshed=%d "
                "headers_refreshed=%d operator_edited=%d impl_modified=%d "
                "ambiguous=%d impl_path=%d preserved=%d errors=%d",
                result["manifest_tools_refreshed"],
                result["manifest_fields_refreshed"],
                result["manifest_headers_refreshed"],
                result["manifest_skipped_operator_edited"],
                result["manifest_skipped_modified"],
                result["manifest_skipped_ambiguous"],
                result["manifest_skipped_impl_path"],
                result["manifest_preserved"], len(errors))
        if result["manifest_preserved"]:
            log.warning(
                "[ManifestConverge] %d seed tool record(s) were refreshed from the "
                "packaged catalog before any baseline existed; the pre-refresh "
                "records are kept at %s", result["manifest_preserved"], bootstrap_root)
        return result
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[ManifestConverge] unexpected failure (non-fatal): %s", exc)
        errors.append(str(exc))
        return result


def _maybe_log_profile_notice(vault_dir) -> None:
    """v0.9.0 (Layer 1): if vault has no user_profile.json, log a one-line
    nudge so operators discover the wizard."""
    try:
        if not (Path(vault_dir) / "user_profile.json").exists():
            logger.info("[VaultMigrator] no user profile set yet — run "
                        "`systemu user init` to personalize systemu.")
    except Exception:
        pass


def run(vault_dir: Path, *, logger_=None) -> Dict[str, Any]:
    """v0.8.22: idempotent vault upgrade. Returns telemetry dict."""
    log = logger_ or logger

    # Off-switch
    if (os.environ.get("SYSTEMU_VAULT_AUTO_MIGRATE", "on") or "on").lower() == "off":
        log.info("[VaultMigrator] disabled via SYSTEMU_VAULT_AUTO_MIGRATE=off")
        return {"skipped": True, "reason": "disabled"}

    _maybe_log_profile_notice(vault_dir)

    installed = _installed_version()

    # G0: backfill EffectTags independently of the seed-tool fast path, so a
    # version bump that only adds the vocabulary still stamps every tool. Own
    # version marker; never raises.
    backfill_effect_tags(vault_dir, version=installed, logger_=log)

    # Re-label mis-flagged SEED tools (repo code wrongly marked LLM-forged), sha-
    # guarded so only a byte-identical-to-package body is cleared. Like the G0
    # backfill this runs INDEPENDENTLY of the seed-tool fast path below — this
    # fix ships without a version bump, so an existing vault's .seed_version
    # already equals `installed` and anything behind that return would never run.
    forged_norm = normalize_seed_forged_flags(vault_dir, logger_=log)

    # F26 — bring seed RECORDS back onto the shipped catalog for the fields the
    # PACKAGE owns. Ahead of the fast path for the same reason as its three
    # siblings, and for a reason specific to this defect: the seed loop below keys
    # every decision on the IMPLEMENTATION's bytes, so a manifest-only correction
    # is invisible to it even on a version bump (it lands in `skipped_identical`).
    # Gated on provenance + a recorded baseline; see the docstring.
    manifest = converge_package_manifest(vault_dir, logger_=log)

    # Project body tags onto the index headers the readers actually read. Runs on
    # EVERY boot, ahead of the fast path below, for the reason its two siblings
    # do: a vault already sitting on the installed version takes the fast path,
    # and the backfill above takes ITS fast path too, so anything gated behind
    # either would never execute on a real deployed vault. Idempotent; no write
    # once converged.
    converged = converge_index_effect_tags(vault_dir, logger_=log)

    # REPAIR A VAULT DAMAGED BY AN EARLIER BOOT.
    #
    # The pass above MIRRORS; it cannot DERIVE. A body that lost its
    # `effect_tags` key on some earlier boot therefore stays unclassified and its
    # header is cleared to match — fail-closed, but permanently UNKNOWN at every
    # gate. That is exactly the state the pre-fix migrator left behind: its seed
    # loop overwrote bodies with the packaged ones (which carry no `effect_tags`)
    # on a boot whose `.effect_tags_seed` was already current, so the only
    # re-deriver took its fast path on every later boot.
    #
    # Three independent gates each blocked the repair and none could re-derive:
    # the backfill's marker (above), this pass's mirror-only rule, and the
    # `installed == vault_seed` fast path immediately below — which returns
    # BEFORE the post-seed-loop `force=True` re-derive, putting that call out of
    # reach on precisely the vaults that need it. Measured against a vault
    # damaged by a real subprocess boot of the pre-fix build: five boots of the
    # fixed build at the SAME version left `run_command` and `file_write` absent
    # in body AND header, 15/41 non-empty against 17/41 on a fresh vault.
    #
    # So the repair has to sit HERE, above the fast path, keyed on OBSERVED
    # DAMAGE rather than on a version. `force=True` because the marker is current
    # by construction on every vault this fires for. One pass suffices: the
    # backfill writes the key onto EVERY body it processes, including ones it
    # classifies to `[]`, so `bodies_missing_tag_key` is 0 on the next boot and this
    # cannot loop. A vault with nothing to repair pays one dict lookup.
    #
    # F14 — this reads `bodies_missing_tag_key`, NOT the sibling
    # `bodies_with_empty_tags`. The trigger must stay keyed on the ABSENT key: an
    # empty-but-present list is a legitimate steady state for a tool the classifier
    # genuinely cannot resolve, and re-deriving on it every boot would re-run the
    # whole backfill forever on any vault holding one such tool. (The two counters
    # used to be one, under the name `unclassified_bodies`, which is exactly why the
    # log said 0 while 24 tools were unclassified.)
    if converged.get("bodies_missing_tag_key"):
        log.info("[VaultMigrator] %d tool bodies carry no effect_tags — "
                 "re-deriving (a previous boot wiped them)",
                 converged["bodies_missing_tag_key"])
        backfill_effect_tags(vault_dir, version=installed, logger_=log, force=True)
        converge_index_effect_tags(vault_dir, logger_=log)

    vault_seed = _read_seed_version(vault_dir)

    def _with_manifest(summary: Dict[str, Any]) -> Dict[str, Any]:
        """Carry the F26 counters onto every exit. The fast path is the ONLY exit a
        real deployed vault takes under this project's no-bump release rule, so a
        manifest number that appears solely on the seed-loop path reaches nobody."""
        for k in ("manifest_tools_refreshed", "manifest_fields_refreshed",
                  "manifest_headers_refreshed", "manifest_skipped_operator_edited",
                  "manifest_skipped_modified", "manifest_preserved"):
            summary[k] = manifest.get(k, 0)
        return summary

    # Fast path
    if installed == vault_seed:
        return _with_manifest({"fast_path": True, "seed_version": installed,
                               "forged_normalized": forged_norm.get("normalized", 0)})

    errors: list = []

    # Load package + vault tool indices
    try:
        pkg_vault = _package_vault_root()
        pkg_idx = json.loads((pkg_vault / "tools" / "index.json").read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"package_index: {exc}")
        log.error("[VaultMigrator] cannot read package index: %s", exc)
        return _with_manifest({"errors": errors, "added": 0, "impl_replaced": 0})

    try:
        vault_idx_path = vault_dir / "tools" / "index.json"
        vault_idx = json.loads(vault_idx_path.read_text(encoding="utf-8")) \
                    if vault_idx_path.exists() else []
    except Exception as exc:
        errors.append(f"vault_index: {exc}")
        log.error("[VaultMigrator] cannot read vault index: %s", exc)
        return _with_manifest({"errors": errors, "added": 0, "impl_replaced": 0})

    pkg_names = {e.get("name") for e in pkg_idx if e.get("name")}
    vault_by_name = {e.get("name"): e for e in vault_idx if e.get("name")}

    added: list = []
    # F26 — WHAT THIS LIST ACTUALLY HOLDS, and why it is no longer called
    # `updated`. An entry lands here when the seed loop OVERWROTE the tool's
    # IMPLEMENTATION FILE, i.e. `{name}.py` was missing or its bytes differed from
    # the package's. It is a file-copy tally, not a record-change tally, and it was
    # misread in exactly the way this project has now hit twice:
    #
    #   * It is BLIND to manifest-only drift. Correcting `dependencies` in the
    #     shipped catalog leaves the .py byte-identical, so the tool goes to
    #     `skipped_identical` and this number stays 0 while the record stays stale.
    #     Nothing in the line tells the operator the catalog was NOT refreshed.
    #   * When it IS non-zero it OVER-reports. A tool counts here because its .py
    #     changed, even if the body JSON that got copied over the top was already
    #     byte-identical — so `updated=41` was read as "41 records refreshed" on a
    #     run where zero manifest fields moved.
    #
    # Renamed rather than redefined because the daemon logs the whole summary dict
    # verbatim (`_v0822_run_vault_migrator`), so an `updated` key survives to the
    # operator's console no matter what the log line says. Same defect shape as
    # F14's `stamped=41`. `tests/test_f26_...::test_impl_counter_is_named_for_what_
    # it_counts` pins the new name against the measured filesystem delta AND
    # asserts `updated` never comes back.
    impl_replaced: list = []
    skipped_forged = 0
    skipped_identical = 0
    preserved = 0
    kept_disabled: list = []

    pkg_impl_dir = pkg_vault / "tools" / "implementations"
    vault_impl_dir = vault_dir / "tools" / "implementations"
    vault_impl_dir.mkdir(parents=True, exist_ok=True)

    # Where clobbered vault files are copied before being replaced. Labelled with
    # the version being migrated FROM — that is what the preserved bytes are.
    # Sanitised because the label comes from `.seed_version`'s CONTENT.
    backup_root = (vault_dir / _PRE_MIGRATION_DIRNAME
                   / _safe_version_label(vault_seed))

    # NOTE — WHY ONLY THE IMPLEMENTATION IS PRESERVED, NOT THE TOOL BODY JSON.
    # `backfill_effect_tags` runs ABOVE this, on the same boot, and rewrites EVERY
    # tool body to stamp `effect_tags` (measured: 41/41 bodies rewritten, so every
    # body already differs from the package's by the time this loop compares).
    # Copying a body aside here would therefore preserve a machine-generated
    # intermediate the migrator itself had just written — never the operator's
    # original, which was already gone — while presenting itself as protection.
    # A backup that cannot hold what it claims to hold is worse than none, so the
    # body is deliberately left out and `preserved` counts implementations only.
    # If body preservation is ever wanted it has to happen BEFORE the backfill,
    # not here.
    #
    # NOTE — WHY `tools/index.json` IS MERGED AND NOT COPIED ASIDE EITHER.
    # The write-back below rewrites the whole file, and the update branch swaps
    # each matched entry for the package's. Three measurements say a
    # `_preserve_before_overwrite` backup is the wrong shape for it:
    #   1. THE SAME TRAP AS THE BODY JSON. `normalize_seed_forged_flags` runs
    #      ABOVE this and rewrites `tools/index.json` itself whenever it clears a
    #      mis-flag — and the POC finding is that real vaults mis-flag EVERY seed,
    #      so it is the common case. A copy taken here would preserve a file the
    #      migrator had already rewritten on this boot.
    #   2. NO DIVERGENCE PREDICATE EXISTS. A vault index always differs from the
    #      package's (user-forged tools live only in the vault's), so a
    #      `_diverges`-gated backup would fire on 100% of migrations and
    #      `preserved` would stop meaning "you had local edits" — the cry-wolf
    #      failure the body NOTE above exists to avoid.
    #   3. NOTHING IN IT IS UNIQUE TO IT. Every field `vault._tool_header` emits
    #      is derived from the tool body, and `jobs._backfill_tool_headers_v061`
    #      re-derives the five a live header carries and the packaged one lacks
    #      (`dry_run_status`, `version`, the two schema summaries,
    #      `implementation_path`) on the next boot.
    # What does NOT self-heal is `enabled`, because the body it is derived from is
    # replaced by the package's in the same pass and every seed ships enabled.
    # That one field is carried across instead — see `_operator_disabled`.

    for pkg_entry in pkg_idx:
        name = pkg_entry.get("name")
        tid = pkg_entry.get("id")
        if not name or not tid:
            continue
        pkg_impl = pkg_impl_dir / f"{name}.py"
        pkg_body = pkg_vault / "tools" / f"tool_{tid}.json"
        if not pkg_impl.exists():
            # package entry has no impl file (e.g., shipped as data only); skip
            continue
        vault_entry = vault_by_name.get(name)
        # The two destinations this loop can CLOBBER. Bound once, and both the
        # preservation and the copy2 below use these SAME objects — a backup
        # taken from a differently-resolved path would look like protection while
        # protecting nothing. Note this is deliberately `{name}.py`, NOT
        # `_resolve_vault_impl`: the overwrite here is by name, and the backup has
        # to follow the overwrite rather than the other passes' resolution.
        dest_impl = vault_impl_dir / f"{name}.py"
        dest_body = vault_dir / "tools" / f"tool_{tid}.json"
        # Does this vault have the tool switched OFF? Read BEFORE anything is
        # overwritten — the body carrying the answer is one of the files this
        # iteration replaces. See `_operator_disabled`.
        disabled = _operator_disabled(vault_dir, vault_entry, dest_body)
        if vault_entry is None:
            # ADD: new seed tool
            try:
                # An ADD can still clobber: a vault may hold an ORPHANED
                # implementation file with no index entry backing it.
                if _diverges(dest_impl, pkg_impl):
                    _preserve_before_overwrite(
                        dest_impl, backup_root, f"implementations/{name}.py")
                    preserved += 1
                shutil.copy2(pkg_impl, dest_impl)
                if pkg_body.exists():
                    # body: see NOTE in `run` — replaced, not preserved, but an
                    # ORPHANED body's disable is still operator intent, and this
                    # run is about to make it reachable again by re-indexing it.
                    _install_body(pkg_body, dest_body, disabled)
                vault_idx.append(_entry_keeping_disable(pkg_entry, disabled))
                added.append(tid)
                if disabled:
                    kept_disabled.append(name)
            except Exception as exc:
                errors.append(f"add {name}: {exc}")
        else:
            # Identity by NAME → treat as seed regardless of forged flag.
            # UPDATE if impl content differs.
            existing_impl = dest_impl
            try:
                if existing_impl.exists() and _file_sha256(existing_impl) == _file_sha256(pkg_impl):
                    skipped_identical += 1
                else:
                    # Differing bytes under a seed NAME is exactly the
                    # operator-edited-seed case. Preserve first; if that fails we
                    # land in the handler below and the copy2 never runs.
                    if _diverges(existing_impl, pkg_impl):
                        _preserve_before_overwrite(
                            existing_impl, backup_root, f"implementations/{name}.py")
                        preserved += 1
                    shutil.copy2(pkg_impl, existing_impl)
                    if pkg_body.exists():
                        _install_body(pkg_body, dest_body, disabled)  # see NOTE
                    # Replace vault index entry with package authoritative version
                    # — MERGED, not copied: the operator's `enabled` survives.
                    for i, e in enumerate(vault_idx):
                        if e.get("name") == name:
                            vault_idx[i] = _entry_keeping_disable(pkg_entry, disabled)
                            break
                    impl_replaced.append(tid)
                    if disabled:
                        kept_disabled.append(name)
            except Exception as exc:
                errors.append(f"update {name}: {exc}")

    # Skipped count includes user-forged tools (vault entries whose name isn't in package)
    skipped_forged = sum(1 for e in vault_idx if e.get("name") not in pkg_names)

    # Write back vault tools index if changed
    if added or impl_replaced:
        try:
            vault_idx_path.write_text(json.dumps(vault_idx, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            errors.append(f"vault_index_write: {exc}")

    # RE-DERIVE EFFECT TAGS FOR WHAT THIS LOOP JUST REPLACED.
    #
    # Every ADD and every UPDATE above overwrites BOTH the tool body
    # (`_install_body`) and its index header (`_entry_keeping_disable(pkg_entry,
    # ...)`) with the PACKAGED versions — and the packaged `tools/index.json` and
    # tool bodies carry no `effect_tags` key at all. So this loop WIPES the
    # classification the backfill stamped at the top of this same boot, and
    # because that backfill already wrote `.effect_tags_seed` for the installed
    # version, its own fast path means no later boot re-stamps it either. Measured
    # before this block existed: `run()` reported updated=1 / skipped_identical=40
    # and the updated tool ended with the key ABSENT from both records,
    # permanently.
    #
    # `force=True` is required precisely because the marker is already current.
    # Re-DERIVING (rather than carrying the pre-migration tags across) is the
    # correct repair: the loop replaced the IMPLEMENTATION too, so the old
    # classification describes source that is no longer there — carrying it would
    # be the same mistake `_entry_keeping_disable` avoids by refusing to carry the
    # schema summaries across.
    if added or impl_replaced:
        backfill_effect_tags(vault_dir, version=installed, logger_=log, force=True)
        converge_index_effect_tags(vault_dir, logger_=log)

        # F26 — RE-RECORD THE MANIFEST BASELINE FOR WHAT THIS LOOP JUST REPLACED.
        # The loop installs the PACKAGE's body verbatim, so those records are
        # converged by definition; what they lack is a baseline saying so. Without
        # this, a tool ADDED on this boot has no recorded package value until the
        # NEXT boot, and an operator who edits its description in between would
        # have that edit flattened by the first bootstrap refresh (correctly
        # preserved, but flattened). Cheap: everything is already converged, so the
        # pass writes no bodies — it only extends the baseline.
        #
        # Counters are SUMMED, not replaced: the disk delta for the whole `run` is
        # the sum of both passes, and a counter that reported only the second one
        # would under-report exactly the fields the first one fixed.
        again = converge_package_manifest(vault_dir, logger_=log)
        for _k, _v in again.items():
            if isinstance(_v, int) and isinstance(manifest.get(_k), int):
                manifest[_k] = manifest[_k] + _v

    # Wire Wild Card (ADD-only)
    wild_card_added = 0
    try:
        wild_card_added = _wire_wild_card(vault_dir, added)
    except Exception as exc:
        errors.append(f"wild_card: {exc}")

    # Atomic .seed_version write
    try:
        _write_seed_version_atomic(vault_dir, installed)
    except Exception as exc:
        errors.append(f"seed_version_write: {exc}")

    summary = {
        "fast_path": False,
        "seed_version_from": vault_seed,
        "seed_version_to": installed,
        "added": len(added),
        # NOT `updated` — see the long note where this list is declared. This is
        # implementation FILES overwritten; the manifest numbers below are the ones
        # that answer "was the catalog refreshed?".
        "impl_replaced": len(impl_replaced),
        "skipped_forged": skipped_forged,
        # Seeds whose IMPLEMENTATION matched the package's, so the seed loop copied
        # nothing. Their RECORDS are still converged — by `converge_package_manifest`
        # above, which is the whole F26 fix — so this is not "skipped the tool", it
        # is "skipped the file copy".
        "skipped_identical": skipped_identical,
        "wild_card_added": wild_card_added,
        "forged_normalized": forged_norm.get("normalized", 0),
        # Seed IMPLEMENTATIONS copied aside because this run was about to
        # overwrite bytes that were not the package's. Non-zero means the operator
        # had LOCAL EDITS. Returned, not just logged, so the daemon's summary
        # carries it to the operator — a count buried in a log line is a count
        # nobody reads. Counts only genuine divergence: if it fired on every
        # replaced file it would be non-zero on a vault nobody had touched, and a
        # warning that cries wolf across 41 seeds trains people to ignore it.
        "preserved": preserved,
        "preserved_dir": str(backup_root) if preserved else "",
        # Seeds this run replaced that the vault had switched OFF, and which stay
        # off. Named rather than counted because the operator has to be able to
        # tell WHICH tool is still disabled after an upgrade that shipped it a new
        # implementation — the alternative posture (re-arming them) is the silent
        # one. Zero on a vault where nothing was disabled, so it never cries wolf.
        "kept_disabled": len(kept_disabled),
        "kept_disabled_names": list(kept_disabled),
        "errors": errors,
    }
    _with_manifest(summary)
    # THE OPERATOR-FACING LINE. Every name says what it counts and every number is
    # pinned to the filesystem by `tests/test_f26_manifest_corrections_reach_the_
    # vault.py`. The old line read
    #     `0.0.0 → 0.10.23: added=0 updated=41 skipped=0 …`
    # which an operator (and the next engineer) reads as "the catalog was
    # refreshed" — on a run where not one manifest field had moved. `skipped=%d`
    # is gone too: it summed `skipped_forged` (the operator's OWN tools, which are
    # not "skipped" by anything, they are simply not the package's) with
    # `skipped_identical` (seeds whose .py matched), so the one number answered two
    # unrelated questions and neither of them the one being asked.
    log.info("[VaultMigrator] %s → %s: added=%d impl_replaced=%d impl_identical=%d "
             "manifest_tools_refreshed=%d manifest_fields_refreshed=%d "
             "manifest_operator_owned=%d user_tools=%d wild_card_added=%d "
             "preserved=%d kept_disabled=%d errors=%d",
             vault_seed, installed, len(added), len(impl_replaced),
             skipped_identical, summary["manifest_tools_refreshed"],
             summary["manifest_fields_refreshed"],
             summary["manifest_skipped_operator_edited"], skipped_forged,
             wild_card_added, preserved, len(kept_disabled), len(errors))
    if preserved:
        log.warning("[VaultMigrator] %d locally-modified vault file(s) were replaced by the "
                    "packaged version; the previous contents are preserved at %s",
                    preserved, backup_root)
    if kept_disabled:
        log.warning("[VaultMigrator] %d updated seed tool(s) were left DISABLED because this "
                    "vault had them switched off: %s. Enable them on the Tools page if the "
                    "new version should run.", len(kept_disabled), ", ".join(kept_disabled))
    return summary


def _wire_wild_card(vault_dir: Path, new_tool_ids: list) -> int:
    """ADD-only wiring of new tool ids into the Wild Card. POC §5.2 step 5:
    look up id=='shadow_wildcard' in shadow_army/index.json; never assume a
    fixed subdirectory name. Returns count of ids actually added."""
    if not new_tool_ids:
        return 0
    sa_idx_path = vault_dir / "shadow_army" / "index.json"
    if not sa_idx_path.exists():
        return 0
    sa_idx = json.loads(sa_idx_path.read_text(encoding="utf-8"))
    wc_entry = next((e for e in sa_idx if e.get("id") == "shadow_wildcard"), None)
    if wc_entry is None:
        return 0
    # Update index entry's tool_ids (ADD-only)
    existing = wc_entry.get("tool_ids") or []
    fresh = [t for t in new_tool_ids if t not in existing]
    if not fresh:
        return 0
    wc_entry["tool_ids"] = existing + fresh
    sa_idx_path.write_text(json.dumps(sa_idx, indent=2) + "\n", encoding="utf-8")

    # Also update the per-shadow shadow.json (the writer prepends 'shadow_';
    # so the dir is 'shadow_shadow_wildcard')
    wc_dir = vault_dir / "shadow_army" / "shadow_shadow_wildcard"
    shadow_json = wc_dir / "shadow.json"
    if shadow_json.exists():
        wc = json.loads(shadow_json.read_text(encoding="utf-8"))
        existing2 = wc.get("tool_ids") or []
        wc["tool_ids"] = existing2 + [t for t in new_tool_ids if t not in existing2]
        shadow_json.write_text(json.dumps(wc, indent=2) + "\n", encoding="utf-8")

    return len(fresh)
