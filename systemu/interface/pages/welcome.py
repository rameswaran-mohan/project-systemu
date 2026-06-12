"""W9.1 — first-run onboarding (/welcome).

The UserProfile model shipped in v0.9.0 and was never collected — fresh
installs ran identity-blind (a run literally guessed the operator's location
by IP). One screen, four steps:

  1. API key — STATUS ONLY. Mirrors the Settings security stance: the key is
     never typed into the UI; the page shows whether it's loaded and how to
     add it to .env.
  2. Model preset — the 8.1 quality/budget choice, surfaced at the moment it
     matters most (first run).
  3. Office profile — name, location, timezone (detected, editable), output
     folder, plus role/organisation stored as user_facts (the UserProfile
     schema is extra:forbid — office context lives in facts, by design).
  4. Try it — points at the quick lane and the recorder.

The dashboard's home page redirects here while ``needs_onboarding`` is true;
"Maybe later" writes a skip-sentinel fact so the redirect never nags.
"""
from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

_SKIP_TAG = "onboarding_skipped"


def onboarding_steps() -> List[str]:
    """Pure: the wizard's step titles (contract for tests + rendering)."""
    return ["API key", "Model preset", "Your profile", "Try it"]


def personas() -> List[str]:
    """Charter v2 requirement 5: one product, persona-adaptive. The choice
    is stored as a fact; trust posture + starter kits consume it later."""
    return ["Personal", "Freelance", "Solo business", "Small business team",
            "Enterprise professional"]


def starter_prompts() -> List[str]:
    """W10.4: one-click first tasks — safe, local, instantly demonstrative
    (each lands pre-filled in Chat's quick lane; the operator clicks Run)."""
    return [
        "List the files in my deliverables folder and write a short markdown index of them",
        "Create a CSV named expenses_template.csv with columns Date, Vendor, Amount, Category",
        "Search the web for today's top 3 news headlines about AI assistants and summarize them",
    ]


def detect_timezone() -> str:
    """Best-effort IANA-ish timezone name for prefill; never raises."""
    try:
        from datetime import datetime, timezone
        local = datetime.now(timezone.utc).astimezone()
        name = getattr(local.tzinfo, "key", None) or local.tzname()
        return str(name or "UTC")
    except Exception:
        return "UTC"


def needs_onboarding(vault) -> bool:
    """True when no profile exists AND the operator hasn't skipped.

    Defensive: any error means False — the page shell must never break or
    redirect-loop because the vault is unhappy.
    """
    try:
        if vault.get_user_profile() is not None:
            return False
        from systemu.runtime.user_profile import get_facts
        return not get_facts(vault, tags=[_SKIP_TAG])
    except Exception:
        return False


def mark_skipped(vault) -> None:
    """Record the operator's 'later' so the redirect stops nagging."""
    from systemu.runtime.user_profile import add_fact
    add_fact(vault, "onboarding deferred by operator",
             source="onboarding", tags=[_SKIP_TAG])


def save_onboarding(vault, *, name: str, location: str, timezone: str,
                    output_dir: str, role: str = "", org: str = "",
                    persona: str = ""):
    """Persist the collected profile + office context. Returns the profile."""
    from systemu.core.models import UserProfile
    from systemu.runtime.user_profile import add_fact

    profile = UserProfile(
        name=name.strip(),
        location_text=location.strip(),
        timezone=timezone.strip() or "UTC",
        default_output_dir=output_dir.strip(),
    )
    vault.save_user_profile(profile)
    if persona.strip():
        add_fact(vault, f"Usage persona: {persona.strip()}",
                 source="onboarding", tags=["office_context", "persona"])
    if role.strip():
        add_fact(vault, f"Role: {role.strip()}",
                 source="onboarding", tags=["office_context"])
    if org.strip():
        add_fact(vault, f"Organisation: {org.strip()}",
                 source="onboarding", tags=["office_context"])
    return profile


def build_welcome_page() -> None:
    """Render the one-screen wizard (token classes; no inline f-styles)."""
    from nicegui import ui
    from sharing_on.model_presets import PRESETS, is_budget_class
    from systemu.interface.dashboard_state import AppState

    state = AppState.get()
    vault = state.vault
    config = state.config

    with ui.column().classes("w-full items-center"):
        with ui.column().classes("s-card").style("max-width: 720px; width: 100%;"):
            ui.label("Welcome to Systemu").classes("s-page-title")
            ui.label(
                "Your office assistant learns how you work and runs it for "
                "you — with you in control. Four quick steps."
            ).classes("s-muted")

            # ── 1. API key (status only — never typed here) ────────────────
            ui.label(f"1 · {onboarding_steps()[0]}").classes("s-section-head")
            if getattr(config, "openrouter_api_key", ""):
                ui.label("API key loaded — you're ready to run tasks.").classes("s-cell")
            else:
                ui.label(
                    "No API key found. Add OPENROUTER_API_KEY=<your key> to "
                    "the .env file next to the app and restart — keys are "
                    "never entered in the browser."
                ).classes("s-banner s-banner--warn w-full")

            # ── 2. Model preset ───────────────────────────────────────────
            ui.label(f"2 · {onboarding_steps()[1]}").classes("s-section-head")
            ui.label(
                "The reasoning model decides how good results feel. "
                "'quality' is recommended for office work; 'budget' is the "
                "cheapest. Change anytime in Settings."
            ).classes("s-muted")
            preset_select = ui.select(sorted(PRESETS), label="Preset").classes("s-input")
            if is_budget_class(getattr(config, "tier1_model", "")):
                ui.label(
                    "Currently on a flash/free-class reasoning model — fine "
                    "to start, but it caps task quality."
                ).classes("s-muted")

            # ── 3. Profile ────────────────────────────────────────────────
            ui.label(f"3 · {onboarding_steps()[2]}").classes("s-section-head")
            persona_in = ui.select(
                personas(), label="How will you use Systemu?",
            ).classes("s-input s-input-full")
            name_in = ui.input("Your name").classes("s-input s-input-full")
            loc_in = ui.input(
                "Location (city, country)",
                placeholder="Chennai, IN",
            ).classes("s-input s-input-full")
            tz_in = ui.input("Timezone", value=detect_timezone()).classes(
                "s-input s-input-full")
            out_in = ui.input(
                "Where should produced files go?",
                value=getattr(config, "output_dir", "") or "",
            ).classes("s-input s-input-full")
            role_in = ui.input(
                "Your role (optional)", placeholder="Finance analyst",
            ).classes("s-input s-input-full")
            org_in = ui.input(
                "Organisation / team (optional)", placeholder="Acme Pvt Ltd",
            ).classes("s-input s-input-full")

            # ── 4. Try it ─────────────────────────────────────────────────
            ui.label(f"4 · {onboarding_steps()[3]}").classes("s-section-head")
            ui.label(
                "Pick a starter below (it lands pre-filled in Chat — you "
                "press Run), or hit Record and do any task once: Systemu "
                "watches and turns it into a repeatable workflow. That's "
                "the superpower."
            ).classes("s-muted")
            from urllib.parse import quote as _q
            for _p in starter_prompts():
                _label = ui.label(f"›  {_p}").classes("s-cell")
                _label.style("cursor: pointer;")
                _label.on("click",
                          lambda _, p=_p: ui.navigate.to(
                              f"/chat?prefill={_q(p)}"))

            def _finish(_=None) -> None:
                if not name_in.value.strip():
                    ui.notify("Please tell me your name.", type="warning")
                    return
                try:
                    # Preset chosen → persist as explicit tier vars (same
                    # semantics as Settings: explicit always wins).
                    tiers = PRESETS.get(preset_select.value or "")
                    if tiers:
                        from systemu.interface.pages.settings import _update_env_var
                        _update_env_var("SYSTEMU_TIER1_MODEL", tiers["tier1"])
                        _update_env_var("SYSTEMU_TIER2_MODEL", tiers["tier2"])
                        _update_env_var("SYSTEMU_TIER3_MODEL", tiers["tier3"])
                        config.tier1_model = tiers["tier1"]
                        config.tier2_model = tiers["tier2"]
                        config.tier3_model = tiers["tier3"]
                    save_onboarding(
                        vault,
                        name=name_in.value, location=loc_in.value,
                        timezone=tz_in.value, output_dir=out_in.value,
                        role=role_in.value or "", org=org_in.value or "",
                        persona=persona_in.value or "",
                    )
                except Exception as exc:
                    logger.exception("[Welcome] onboarding save failed")
                    ui.notify(f"Could not save: {exc}", type="negative")
                    return
                ui.notify("All set — welcome aboard.", type="positive")
                ui.navigate.to("/")

            def _later(_=None) -> None:
                try:
                    mark_skipped(vault)
                except Exception:
                    logger.debug("[Welcome] skip sentinel failed", exc_info=True)
                ui.navigate.to("/")

            from systemu.interface.design.primitives import button
            with ui.row().classes("w-full q-gutter-sm q-mt-md"):
                button("Finish setup", variant="primary", on_click=_finish)
                button("Maybe later", variant="ghost", on_click=_later)
