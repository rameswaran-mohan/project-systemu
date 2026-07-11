"""v0.8.22 — silently bring the vault's seed tools up to the installed
package's version. Auto-runs on daemon start. Idempotent via .seed_version.
Identity is by NAME (not by `forged_by_systemu` flag) — POC finding: user
vaults mis-flag all seed tools as forged, so a flag-based filter would
prevent legitimate seed updates from ever reaching the vault. Never raises;
wrapped by daemon try/except so a migration failure can't break boot.
"""
from __future__ import annotations

import hashlib
import importlib.resources
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict

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


def backfill_effect_tags(vault_dir, *, version: str | None = None, logger_=None) -> Dict[str, Any]:
    """G0 — one-pass, idempotent backfill of ``Tool.effect_tags`` onto every vault
    tool body, classified deterministically from its implementation source
    (spec UNIFIED-v2 §5.7 / §9 G0).

    Version-gated via ``.effect_tags_seed`` (independent of the seed-tool fast
    path), so it runs once per version bump and is a no-op on every boot after.
    NEVER raises — a classification failure on one tool must not break boot; it
    leaves that tool's tags empty (⇒ UNKNOWN-at-gate) and moves on.
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

            impl_rel = body.get("implementation_path") or (f"{name}.py" if name else "")
            source = ""
            if impl_rel:
                impl_path = impl_dir / impl_rel
                if impl_path.exists():
                    try:
                        source = impl_path.read_text(encoding="utf-8", errors="replace")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"source {tid}: {exc}")

            # R-A13b-2ii-a — MERGE the structural+curated scan with the MONOTONIC
            # money-move FLOOR: any independent money signal (import/host/attr) ALWAYS
            # adds MONEY_MOVE, so a money-move tool can never be stamped WITHOUT it
            # (the fail-closed-on-money-move guarantee; the floor is re-derived from
            # source each version → idempotent). (2ii-b's `declared` merge is deferred.)
            try:
                if source:
                    tagset = {t.value for t in classify_source(source)}
                    if effect_signals.any_money_move_signal(source):
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

        log.info("[EffectTagBackfill] version=%s stamped=%d errors=%d", version, stamped, len(errors))
        return {"fast_path": False, "effect_tags_seed": version, "stamped": stamped, "errors": errors}
    except Exception as exc:  # noqa: BLE001 — never break boot
        log.error("[EffectTagBackfill] unexpected failure (non-fatal): %s", exc)
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

    vault_seed = _read_seed_version(vault_dir)

    # Fast path
    if installed == vault_seed:
        return {"fast_path": True, "seed_version": installed}

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
