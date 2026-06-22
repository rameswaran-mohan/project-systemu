"""`.env`-backed gate-mode settings (spec §4.3 / D4-D5).

Mirrors the adherence settings (interface/pages/settings.get_adherence_settings
/ save_adherence_settings) but lives in runtime/ and is IMPORT-LIGHT: it does
NOT import NiceGUI. interface/pages/settings.py pulls in `nicegui` at module
load, so its `_update_env_var` writer is DUPLICATED here (~12 lines) rather
than imported, keeping this module usable from headless / CLI contexts.

Persists three vars:
  * SYSTEMU_GATE_MODE      — bypass | risk_tiered | approve_only
  * SYSTEMU_GATE_OVERRIDES — json.dumps of a {gate_type: allow|ask|deny} map
  * SYSTEMU_GATE_NO_FLOOR  — "1" | "0"
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

_VALID_MODES = ("bypass", "risk_tiered", "approve_only")


def _update_env_var(key: str, value: str) -> None:
    """Update a single variable in the .env file (duplicated from
    interface/pages/settings.py to avoid importing NiceGUI)."""
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    found = False
    updated = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            updated.append(f"{key}={value}\n")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key}={value}\n")
    env_path.write_text("".join(updated), encoding="utf-8")


def get_gate_mode_settings() -> dict:
    """Read the current gate-mode dial from env → {mode, overrides, no_floor}."""
    raw_mode = (os.environ.get("SYSTEMU_GATE_MODE") or "risk_tiered").strip().lower()
    if raw_mode not in _VALID_MODES:
        raw_mode = "risk_tiered"

    overrides: Dict[str, str] = {}
    raw_overrides = os.environ.get("SYSTEMU_GATE_OVERRIDES")
    if raw_overrides:
        try:
            parsed = json.loads(raw_overrides)
            if isinstance(parsed, dict):
                overrides = {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            overrides = {}

    raw_no_floor = (os.environ.get("SYSTEMU_GATE_NO_FLOOR") or "0").strip().lower()
    no_floor = raw_no_floor in ("1", "true", "yes", "on")

    return {"mode": raw_mode, "overrides": overrides, "no_floor": no_floor}


def save_gate_mode_settings(*, mode: str,
                            overrides: Optional[Dict[str, str]] = None,
                            no_floor: bool = False) -> None:
    """Validate + persist the gate-mode dial to .env; patch live os.environ.

    Raises ValueError for an unknown mode (never silently downgrades)."""
    if mode not in _VALID_MODES:
        raise ValueError(
            f"mode must be one of {'/'.join(_VALID_MODES)}, got {mode!r}")

    overrides_json = json.dumps(overrides or {}, sort_keys=True)
    no_floor_str = "1" if no_floor else "0"

    _update_env_var("SYSTEMU_GATE_MODE", mode)
    _update_env_var("SYSTEMU_GATE_OVERRIDES", overrides_json)
    _update_env_var("SYSTEMU_GATE_NO_FLOOR", no_floor_str)

    os.environ["SYSTEMU_GATE_MODE"] = mode
    os.environ["SYSTEMU_GATE_OVERRIDES"] = overrides_json
    os.environ["SYSTEMU_GATE_NO_FLOOR"] = no_floor_str
