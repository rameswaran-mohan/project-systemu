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

HEADLESS (F3).  Docker / CI / a remote box has no TTY and no browser, so
every gate above must also be reachable from argv alone.  ``HEADLESS_REMEDIES``
is the declarative registry of *how*: one entry per check id, naming the
non-interactive command and/or the environment variable that satisfies it.
``setup_status`` attaches the entry to each check as ``check["headless"]``, so
the truth-teller and the escape hatch can never drift apart silently —
``tests/test_headless_onboarding.py`` fails the moment a check exists with no
declared path (whether or not it is ``required``; covering all of them closes
the "flip required to False to dodge the fence" loophole).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOUR_FACT_TAG = "tour_completed"


def _provider_remedy():
    """(env vars, hint) for the `key_present` gate — generated from THE MINT."""
    from systemu.runtime import provider_status as _ps
    return (tuple(s.env for s in _ps.PROVIDER_SPECS),
            "make at least one LLM provider usable: "
            + _ps.configure_hint(
                {s.provider: _ps.ProviderStatus(s.provider, s.display, s.env,
                                                s.rule, _ps.STATE_UNKNOWN, "")
                 for s in _ps.PROVIDER_SPECS})
            + " Or run `systemu setup --key <KEY> --no-validate`.")


_PROVIDER_ENV_VARS, _PROVIDER_REMEDY_HINT = _provider_remedy()


# ── F3: the non-interactive path for every gate ─────────────────────────────
# ``argv``: a command that satisfies the check with stdin closed. ``<ANGLED>``
#           segments are operator-supplied values.
# ``env``:  environment variables that satisfy it instead (Docker / systemd).
# Every key of this dict MUST be a check id produced by ``setup_status``, and
# every check id MUST appear here. Neither direction may drift.
HEADLESS_REMEDIES: Dict[str, Dict[str, Any]] = {
    # F19: the check id stays `key_present` (welcome.py's `_REDIRECT_REQUIRED`
    # and the onboarding-gate tests address it by that name), but the FACT is
    # "at least one provider is usable" and the remedies name all of them.
    # `env` and `hint` are GENERATED from `provider_status.PROVIDER_SPECS` at
    # import, so a sixth provider becomes a headless remedy with nobody editing
    # a tuple — the old literal offered a headless operator one env var out of
    # five, and never mentioned the keyless provider at all.
    "key_present": {
        "argv": ("sharing_on", "setup", "--key", "<OPENROUTER_KEY>", "--no-validate"),
        "env": _PROVIDER_ENV_VARS,
        "hint": _PROVIDER_REMEDY_HINT,
    },
    "models_configured": {
        "argv": ("sharing_on", "setup", "--preset", "balanced", "--no-validate"),
        "env": ("SYSTEMU_MODEL_PRESET", "SYSTEMU_TIER1_MODEL"),
        "hint": "set SYSTEMU_MODEL_PRESET, or run `systemu setup --preset balanced`",
    },
    "output_dir_ok": {
        "argv": ("sharing_on", "setup", "--output-dir", "<PATH>", "--no-validate"),
        "env": ("SYSTEMU_OUTPUT_DIR",),
        "hint": "run `systemu setup --output-dir <PATH> --no-validate`, or "
                "set SYSTEMU_OUTPUT_DIR — the folder is created for you",
    },
    "vault_seeded": {
        "argv": ("sharing_on", "init"),
        "env": (),
        "hint": "run `systemu init` in your working folder",
    },
    "profile_present": {
        "argv": ("sharing_on", "user", "init", "--non-interactive",
                 "--name", "<NAME>"),
        "env": (),
        "hint": "run `systemu user init --non-interactive --name <NAME>` "
                "(no TTY needed); `systemu user set <field> <value>` also "
                "creates the profile if it is missing",
    },
    "tour_completed": {
        "argv": ("sharing_on", "onboarding", "complete-tour"),
        "env": (),
        "hint": "run `systemu onboarding complete-tour` — the tour is a "
                "browser affordance, this records that it does not apply",
    },
}


def headless_remedy(check_id: str) -> Optional[Dict[str, Any]]:
    """The declared TTY-free way to satisfy ``check_id`` — None if undeclared."""
    return HEADLESS_REMEDIES.get(check_id)


def _check(check_id: str, label: str, ok: bool, detail: str = "",
           *, required: bool = True) -> Dict[str, Any]:
    return {"id": check_id, "label": label, "ok": bool(ok),
            "detail": detail, "required": required,
            "headless": HEADLESS_REMEDIES.get(check_id)}


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

    # 1. A usable LLM provider — the one thing nothing works without.
    #
    # F19 / DEC-43: this read `config.openrouter_api_key` (falling back to the
    # env var), which is a third private copy of the satisfaction recipe. An
    # operator with a Google key, or with Ollama actually running, was held on
    # the onboarding gate and told to go and get an OpenRouter key, while the
    # Settings page a click away reported those same providers as fine.
    #
    # `any_provider_usable` spends the keyless witness only when a tier
    # explicitly selects it: this runs on the onboarding-gate render path.
    from systemu.runtime import provider_status as _ps
    try:
        # env_overlay preserves the old check's "config OR the environment"
        # reach — the checklist must answer for the .env the operator edited a
        # moment ago, not for the snapshot the daemon booted with.
        usable = _ps.any_provider_usable(_ps.env_overlay(config))
    except Exception:
        usable = False
    checks.append(_check(
        "key_present", "LLM provider configured", bool(usable),
        "" if usable else (
            "No provider is usable yet. " + _PROVIDER_REMEDY_HINT
            + " Credentials live in .env and are never typed in the browser."),
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
        f"{n} tool(s)" if seeded else "run `systemu init` in your working folder",
        required=False))

    # 5. Operator profile — who the assistant works for (W9.2).
    profile = None
    try:
        profile = vault.get_user_profile()
    except Exception:
        pass
    checks.append(_check(
        "profile_present", "Operator profile saved", profile is not None,
        "" if profile is not None else
        "complete the welcome wizard — or, with no browser, "
        "`systemu user init --non-interactive`"))

    # 6. The guided tour (W11.5).
    done = tour_completed(vault)
    checks.append(_check(
        "tour_completed", "Guided tour finished", done,
        "" if done else
        "the tour starts right after the wizard — with no browser, waive it "
        "with `systemu onboarding complete-tour`"))

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
