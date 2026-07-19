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

    The backfill PREFERS this declared set over the structural scan but the MONOTONIC
    money-move FLOOR still unions ``money_move`` in, so a declaration can RAISE severity but
    can NEVER declare-away a scanner-detected money-move (the fail-closed-on-money guarantee).
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


def backfill_effect_tags(vault_dir, *, version: str | None = None, logger_=None) -> Dict[str, Any]:
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
    """
    log = logger_ or logger
    vault_dir = Path(vault_dir)
    version = version or _installed_version()

    try:
        marker = vault_dir / _EFFECT_TAGS_SEED_FILENAME
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == str(version):
            return {"fast_path": True, "effect_tags_seed": version}

        idx_path = vault_dir / "tools" / "index.json"
        if not idx_path.exists():
            # no tool catalog yet — retry on a later boot (do NOT stamp)
            return {"skipped": True, "reason": "no tool index"}

        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8")) or []
        except Exception as exc:  # noqa: BLE001
            log.error("[EffectTagBackfill] cannot read tool index: %s", exc)
            return {"skipped": True, "reason": f"index unreadable: {exc}"}

        from systemu.runtime.effect_tags import classify_source, EffectTag
        from systemu.runtime import effect_signals

        impl_dir = vault_dir / "tools" / "implementations"
        stamped = 0
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

            # R-A13b-2ii — PREFER a tool's SELF-DECLARED effect_tags (TOOL_META) over the
            # structural+curated scan, then apply the MONOTONIC money-move FLOOR: any
            # independent money signal (import/host/attr) ALWAYS adds MONEY_MOVE, so a
            # money-move tool can never be stamped WITHOUT it (the fail-closed-on-money
            # guarantee). A declaration can RAISE severity (2ii-b) but the floor means it
            # can NEVER declare-away a scanner-detected money-move. The floor is re-derived
            # from source each version → idempotent.
            try:
                if source:
                    declared = _declared_effect_tags(source)          # self-report (2ii-b)
                    scanned = {t.value for t in classify_source(source)}
                    tagset = set(declared) if declared else scanned   # declared PREFERRED
                    if effect_signals.any_money_move_signal(source):  # MONOTONIC money FLOOR
                        tagset.add(EffectTag.MONEY_MOVE.value)
                    tags = sorted(tagset)
                else:
                    tags = []
            except Exception as exc:  # noqa: BLE001
                errors.append(f"classify {tid}: {exc}")
                tags = []

            body["effect_tags"] = tags
            try:
                _write_text_atomic(body_path, json.dumps(body, indent=2) + "\n")
                stamped += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write {tid}: {exc}")

        try:
            _write_text_atomic(marker, str(version))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"marker: {exc}")

        log.info(
            "[EffectTagBackfill] version=%s stamped=%d skipped_impl_path=%d errors=%d",
            version, stamped, skipped_impl_path, len(errors))
        return {"fast_path": False, "effect_tags_seed": version, "stamped": stamped,
                "skipped_impl_path": skipped_impl_path, "errors": errors}
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[EffectTagBackfill] unexpected failure (non-fatal): %s", exc)
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


def _maybe_log_profile_notice(vault_dir) -> None:
    """v0.9.0 (Layer 1): if vault has no user_profile.json, log a one-line
    nudge so operators discover the wizard."""
    try:
        if not (Path(vault_dir) / "user_profile.json").exists():
            logger.info("[VaultMigrator] no user profile set yet — run "
                        "`sharing_on user init` to personalize systemu.")
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

    vault_seed = _read_seed_version(vault_dir)

    # Fast path
    if installed == vault_seed:
        return {"fast_path": True, "seed_version": installed,
                "forged_normalized": forged_norm.get("normalized", 0)}

    errors: list = []

    # Load package + vault tool indices
    try:
        pkg_vault = _package_vault_root()
        pkg_idx = json.loads((pkg_vault / "tools" / "index.json").read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"package_index: {exc}")
        log.error("[VaultMigrator] cannot read package index: %s", exc)
        return {"errors": errors, "added": 0, "updated": 0}

    try:
        vault_idx_path = vault_dir / "tools" / "index.json"
        vault_idx = json.loads(vault_idx_path.read_text(encoding="utf-8")) \
                    if vault_idx_path.exists() else []
    except Exception as exc:
        errors.append(f"vault_index: {exc}")
        log.error("[VaultMigrator] cannot read vault index: %s", exc)
        return {"errors": errors, "added": 0, "updated": 0}

    pkg_names = {e.get("name") for e in pkg_idx if e.get("name")}
    vault_by_name = {e.get("name"): e for e in vault_idx if e.get("name")}

    added: list = []
    updated: list = []
    skipped_forged = 0
    skipped_identical = 0

    pkg_impl_dir = pkg_vault / "tools" / "implementations"
    vault_impl_dir = vault_dir / "tools" / "implementations"
    vault_impl_dir.mkdir(parents=True, exist_ok=True)

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
        if vault_entry is None:
            # ADD: new seed tool
            try:
                shutil.copy2(pkg_impl, vault_impl_dir / f"{name}.py")
                if pkg_body.exists():
                    shutil.copy2(pkg_body, vault_dir / "tools" / f"tool_{tid}.json")
                vault_idx.append(pkg_entry)
                added.append(tid)
            except Exception as exc:
                errors.append(f"add {name}: {exc}")
        else:
            # Identity by NAME → treat as seed regardless of forged flag.
            # UPDATE if impl content differs.
            existing_impl = vault_impl_dir / f"{name}.py"
            try:
                if existing_impl.exists() and _file_sha256(existing_impl) == _file_sha256(pkg_impl):
                    skipped_identical += 1
                else:
                    shutil.copy2(pkg_impl, existing_impl)
                    if pkg_body.exists():
                        shutil.copy2(pkg_body, vault_dir / "tools" / f"tool_{tid}.json")
                    # Replace vault index entry with package authoritative version
                    for i, e in enumerate(vault_idx):
                        if e.get("name") == name:
                            vault_idx[i] = pkg_entry
                            break
                    updated.append(tid)
            except Exception as exc:
                errors.append(f"update {name}: {exc}")

    # Skipped count includes user-forged tools (vault entries whose name isn't in package)
    skipped_forged = sum(1 for e in vault_idx if e.get("name") not in pkg_names)

    # Write back vault tools index if changed
    if added or updated:
        try:
            vault_idx_path.write_text(json.dumps(vault_idx, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            errors.append(f"vault_index_write: {exc}")

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
        "updated": len(updated),
        "skipped_forged": skipped_forged,
        "skipped_identical": skipped_identical,
        "wild_card_added": wild_card_added,
        "forged_normalized": forged_norm.get("normalized", 0),
        "errors": errors,
    }
    log.info("[VaultMigrator] %s → %s: added=%d updated=%d skipped=%d wild_card_added=%d errors=%d",
             vault_seed, installed, len(added), len(updated),
             skipped_forged + skipped_identical, wild_card_added, len(errors))
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
