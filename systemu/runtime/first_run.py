"""First-run setup truth (W11.3).

One place answers "is this install actually ready to work?" — consumed by
the daemon boot log, the /welcome wizard, and the W11.4 onboarding gate.

* ``setup_status``  — pure checks, NEVER raises. Each check carries a
  ``required`` flag: only key / profile / tour may ever block the dashboard
  (the W11.4 gate); models, output folder and vault seeding are surfaced
  loudly but never hold the operator hostage.
* ``auto_setup``    — fixes only what is safe to fix silently: directories.
  It never writes keys and never changes model choices — those are explicit
  operator decisions (the installer and the wizard ask; this module only
  tells the truth about them).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

TOUR_FACT_TAG = "tour_completed"


def _check(check_id: str, label: str, ok: bool, detail: str = "",
           *, required: bool = True) -> Dict[str, Any]:
    return {"id": check_id, "label": label, "ok": bool(ok),
            "detail": detail, "required": required}


def tour_completed(vault) -> bool:
    """True when the guided tour has been finished (or explicitly ended)."""
    try:
        from systemu.runtime.user_profile import get_facts
        return bool(get_facts(vault, tags=[TOUR_FACT_TAG]))
    except Exception:
        return False


def setup_status(config, vault) -> List[Dict[str, Any]]:
    """The install's readiness checklist. Pure, ordered, never raises."""
    checks: List[Dict[str, Any]] = []

    # 1. API key — the one thing nothing works without.
    try:
        key = (getattr(config, "openrouter_api_key", "") or
               os.environ.get("OPENROUTER_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    checks.append(_check(
        "key_present", "API key configured", bool(key),
        "" if key else ("Set OPENROUTER_API_KEY in .env — the installer or "
                        "the welcome screen shows how (never typed in the browser)."),
    ))

    # 2. Models — informational: no preset/tiers simply means the defaults.
    preset = (os.environ.get("SYSTEMU_MODEL_PRESET", "") or "").strip()
    tiers = [v for v in (os.environ.get(f"SYSTEMU_TIER{i}_MODEL", "")
                         for i in (1, 2, 3)) if (v or "").strip()]
    if preset:
        detail = f"preset: {preset}"
    elif tiers:
        detail = "explicit tier models set"
    else:
        detail = "defaults in effect — pick a preset in Settings for a stronger brain"
    checks.append(_check("models_configured", "Models chosen", True, detail,
                         required=False))

    # 3. Output folder — where produced files land.
    try:
        out = (getattr(config, "output_dir", "") or "").strip()
    except Exception:
        out = ""
    if out:
        ok = Path(out).expanduser().is_dir()
        checks.append(_check(
            "output_dir_ok", "Output folder ready", ok,
            out if ok else f"{out} is missing — auto-created at next boot",
            required=False))
    else:
        checks.append(_check("output_dir_ok", "Output folder ready", True,
                             "defaults to <vault>/output", required=False))

    # 4. Tool catalog — an unseeded vault can't run anything.
    seeded, n = False, 0
    try:
        tools = vault.load_index("tools") or []
        n = len(tools)
        seeded = n > 0
    except Exception:
        pass
    checks.append(_check(
        "vault_seeded", "Tool catalog seeded", seeded,
        f"{n} tool(s)" if seeded else "run `sharing_on init` in your working folder",
        required=False))

    # 5. Operator profile — who the assistant works for (W9.2).
    profile = None
    try:
        profile = vault.get_user_profile()
    except Exception:
        pass
    checks.append(_check(
        "profile_present", "Operator profile saved", profile is not None,
        "" if profile is not None else "complete the welcome wizard"))

    # 6. The guided tour (W11.5).
    done = tour_completed(vault)
    checks.append(_check(
        "tour_completed", "Guided tour finished", done,
        "" if done else "the tour starts right after the wizard"))

    return checks


def auto_setup(config, vault) -> List[str]:
    """Fix what is safe to fix silently. Returns the list of fixes applied.

    Directories only — creating a folder is always correct and reversible.
    Keys and model choices are explicit operator decisions and are NEVER
    touched here.
    """
    fixed: List[str] = []
    try:
        out = (getattr(config, "output_dir", "") or "").strip()
        if out:
            p = Path(out).expanduser()
            if not p.is_dir():
                p.mkdir(parents=True, exist_ok=True)
                fixed.append(f"created output folder {p}")
        else:
            root = getattr(vault, "root", None)
            if root:
                p = Path(root) / "output"
                if not p.is_dir():
                    p.mkdir(parents=True, exist_ok=True)
                    fixed.append(f"created default output folder {p}")
    except Exception as exc:
        logger.warning("[FirstRun] could not ensure output folder: %s", exc)
    return fixed
