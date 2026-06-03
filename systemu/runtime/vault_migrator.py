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


def run(vault_dir: Path, *, logger_=None) -> Dict[str, Any]:
    """v0.8.22: idempotent vault upgrade. Returns telemetry dict."""
    log = logger_ or logger

    # Off-switch
    if (os.environ.get("SYSTEMU_VAULT_AUTO_MIGRATE", "on") or "on").lower() == "off":
        log.info("[VaultMigrator] disabled via SYSTEMU_VAULT_AUTO_MIGRATE=off")
        return {"skipped": True, "reason": "disabled"}

    installed = _installed_version()
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
