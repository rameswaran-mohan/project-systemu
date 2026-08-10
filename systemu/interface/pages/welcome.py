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
    (each lands pre-filled in Chat's quick lane; the operator clicks Run).

    Kept as the DEFAULT: `persona_content.DEFAULT_SKIN.starters` equals this
    list (pinned by tests), so an operator who hasn't answered step 3 sees
    exactly the pre-registry page.
    """
    return [
        "List the files in my deliverables folder and write a short markdown index of them",
        "Create a CSV named expenses_template.csv with columns Date, Vendor, Amount, Category",
        "Search the web for today's top 3 news headlines about AI assistants and summarize them",
    ]


#: Appended to the dare when the reasoning tier is flash/free-class. Forging a
#: tool is the hardest thing the agent does and it degrades badly on budget
#: models — the dare must not oversell what this install will actually manage.
DARE_BUDGET_CAVEAT = " (works best on the quality preset)"


def dare_label(skin, config) -> str:
    """The persona's forge dare, tier-caveated. Pure — no UI, never raises."""
    line = getattr(skin, "dare_line", "") or ""
    try:
        from sharing_on.model_presets import is_budget_class
        if is_budget_class(getattr(config, "tier1_model", "") or ""):
            return line + DARE_BUDGET_CAVEAT
    except Exception:
        logger.debug("[Welcome] dare tier check failed", exc_info=True)
    return line


def detect_timezone() -> str:
    """Best-effort IANA timezone name for prefill; never raises.

    W12 (audit F3): prefers tzlocal (ships with apscheduler) so Windows
    yields 'Asia/Kolkata' rather than 'India Standard Time' — downstream
    consumers expect IANA names.
    """
    try:
        import tzlocal
        name = tzlocal.get_localzone_name()
        if name:
            return str(name)
    except Exception:
        pass
    try:
        from datetime import datetime, timezone
        local = datetime.now(timezone.utc).astimezone()
        name = getattr(local.tzinfo, "key", None) or local.tzname()
        return str(name or "UTC")
    except Exception:
        return "UTC"


def _refresh_key_status(config, *, env_file: str = ".env") -> bool:
    """Notice a key added to .env while /welcome is open (W11.4).

    The key is never typed in the browser — the operator edits .env in their
    editor and clicks Re-check. Reads the live environment first, then the
    .env file next to the app, WITHOUT stomping the process environment (no
    load_dotenv override — the daemon's env stays exactly as booted). Updates
    the live config snapshot on success. Never raises.
    """
    import os
    key = (os.environ.get("OPENROUTER_API_KEY", "") or "").strip()
    if not key:
        try:
            from dotenv import dotenv_values
            key = ((dotenv_values(env_file) or {})
                   .get("OPENROUTER_API_KEY") or "").strip()
        except Exception:
            key = ""
    if key:
        try:
            config.openrouter_api_key = key
        except Exception:
            pass
    return bool(key)


# The W11.4 redirect funnels fresh installs to /welcome on these checks ONLY.
# The tour is deliberately excluded: its steps navigate spine routes, so
# gating on it would redirect-loop — it auto-starts after the wizard and
# offers resume until completed instead (W11.5).
_REDIRECT_REQUIRED = ("key_present", "profile_present")


def onboarding_gate(vault, config) -> List[str]:
    """W11.4: the required first-run steps still incomplete — [] means free.

    Mandatory applies to FRESH installs only: a pre-W11 'skipped' sentinel is
    honored forever, and SYSTEMU_SKIP_ONBOARDING=1 is the CI/dev/smoke escape
    hatch. Defensive: any error returns [] — never brick the dashboard.
    """
    import os
    try:
        if (os.environ.get("SYSTEMU_SKIP_ONBOARDING", "") or "").lower() in ("1", "true"):
            return []
        from systemu.runtime.user_profile import get_facts
        if get_facts(vault, tags=[_SKIP_TAG]):
            return []  # pre-W11 'later' honored — no retroactive nagging
        from systemu.runtime.first_run import setup_status
        return [c["id"] for c in setup_status(config, vault)
                if c["id"] in _REDIRECT_REQUIRED and not c["ok"]]
    except Exception:
        return []


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


def finalize_onboarding(vault, config, *, name: str, location: str = "",
                        timezone: str = "", output_dir: str = "", role: str = "",
                        org: str = "", persona: str = "", preset: str = "",
                        refresh_key_fn=None) -> "tuple[bool, str]":
    """Validate (API key + name required) and persist onboarding, then report
    ``(ok, message)``. ``ok=False`` → ``message`` is a user-facing reason and
    nothing past the failed gate was persisted; ``ok=True`` → profile saved
    (message "").

    Shared by the Finish button AND the step-4 starter use-cases so a starter
    click finishes setup instead of bouncing off the onboarding gate (the
    starter used to bare-navigate to /chat, which the W11.4 gate redirected
    straight back to /welcome — the "just refreshes" bug)."""
    # F19 / DEC-43: the "can this install run?" verdict comes from THE ONE MINT,
    # never from one config attribute. An operator with a Google key, or with
    # Ollama answering, used to be held here and told to add an OpenRouter key
    # while the Settings page a click away reported those providers as fine.
    # `_refresh_key_status` is still consulted first: it is a WRITER (it pulls a
    # freshly-saved OpenRouter key out of .env into the live config), not a
    # verdict, and the mint scores the config it just updated.
    from systemu.runtime import provider_status as _ps
    refresh = refresh_key_fn if refresh_key_fn is not None else _refresh_key_status
    refresh(config)
    _view = _ps.env_overlay(config)
    if not _ps.any_provider_usable(_view):
        # Name the remedies, all of them — the old copy said "Add your API key
        # first", which for a Google or Ollama operator was a dead end.
        return (False, "Set up an LLM provider first (step 1) — Systemu can't "
                       "run without one. "
                       + _ps.configure_hint(_ps.all_provider_statuses(
                           _view, probe=_ps.unprobed)))
    if not (name or "").strip():
        return (False, "Please tell me your name.")
    try:
        # Preset chosen → persist as explicit tier vars (explicit always wins).
        from sharing_on.model_presets import PRESETS
        tiers = PRESETS.get(preset or "")
        if tiers:
            from systemu.interface.pages.settings import _update_env_var
            _update_env_var("SYSTEMU_TIER1_MODEL", tiers["tier1"])
            _update_env_var("SYSTEMU_TIER2_MODEL", tiers["tier2"])
            _update_env_var("SYSTEMU_TIER3_MODEL", tiers["tier3"])
            config.tier1_model = tiers["tier1"]
            config.tier2_model = tiers["tier2"]
            config.tier3_model = tiers["tier3"]
        save_onboarding(
            vault, name=name, location=location, timezone=timezone,
            output_dir=output_dir, role=role or "", org=org or "",
            persona=persona or "")
    except Exception as exc:
        logger.exception("[Welcome] onboarding save failed")
        return (False, f"Could not save: {exc}")
    return (True, "")


def build_welcome_page() -> None:
    """Render the one-screen wizard (token classes; no inline f-styles)."""
    from nicegui import ui
    from sharing_on.model_presets import PRESETS, is_budget_class
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design.primitives import button
    from systemu.interface.persona_content import DARE_PROMPT, skin_for

    state = AppState.get()
    vault = state.vault
    config = state.config

    # W11.4: while the gate holds (fresh install, key/profile missing) the
    # wizard is mandatory — no skip is offered. Voluntary visitors keep it.
    _gate_active = bool(onboarding_gate(vault, config))

    with ui.column().classes("w-full items-center"):
        with ui.column().classes("s-card").style("max-width: 720px; width: 100%;"):
            ui.label("Welcome to Systemu").classes("s-page-title")
            ui.label(
                "Your office assistant learns how you work and runs it for "
                "you — with you in control. Four quick steps. "
                # The growth thesis: both halves are shipped, gated behaviour
                # (forge offers ask first; the recorder learns workflows), so
                # this is a description, not a roadmap.
                "What you see today is the smallest Systemu will ever be — it "
                "builds new tools when it's missing one (with your approval) "
                "and learns workflows by watching you work."
            ).classes("s-muted")

            # ── 1. API key (status only — never typed here) ────────────────
            ui.label(f"1 · {onboarding_steps()[0]}").classes("s-section-head")
            # F19: the STATUS disclosure consumes the mint, so this step and the
            # Settings page cannot disagree about the same machine.
            from systemu.runtime import provider_status as _ps
            _pstat = _ps.all_provider_statuses(
                _ps.env_overlay(config), probe=_ps.unprobed
                if not _ps.selects_keyless(config) else None,
                cache_ttl_s=_ps.PROBE_CACHE_TTL_S)
            _usable = _ps.satisfied_providers(_pstat)
            if _usable:
                ui.label(
                    "Provider ready: " + ", ".join(u.display for u in _usable)
                    + " — you're set to run tasks."
                ).classes("s-cell")
            else:
                ui.label(
                    "No LLM provider is usable yet — and Systemu can't think "
                    "without one. " + _ps.configure_hint(_pstat)
                    + " Get an OpenRouter key at openrouter.ai/keys. Edit the "
                    ".env file next to the app, save, then click Re-check. "
                    "Credentials are never entered in the browser."
                ).classes("s-banner s-banner--warn w-full")

                def _recheck(_=None) -> None:
                    # W11.4: no restart dance — reload .env in place.
                    _refresh_key_status(config)
                    from systemu.runtime import provider_status as _rps
                    _rps.clear_probe_cache()   # the operator just changed .env
                    if _rps.any_provider_usable(_rps.env_overlay(config)):
                        ui.notify("Provider found — you're ready.",
                                  type="positive")
                        ui.navigate.to("/welcome")
                    else:
                        ui.notify(
                            "Still no usable provider — save .env and try again.",
                            type="warning")

                button("Re-check", variant="primary", on_click=_recheck)

            # ── 2. Model preset ───────────────────────────────────────────
            ui.label(f"2 · {onboarding_steps()[1]}").classes("s-section-head")
            ui.label(
                "The reasoning model decides how good results feel. "
                "'quality' is recommended for office work; 'budget' is the "
                "cheapest. Change anytime in Settings."
            ).classes("s-muted")
            preset_select = ui.select(sorted(PRESETS), label="Preset").classes("s-input s-input-full")
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

            def _run_finalize(dest: str, success_msg: str) -> None:
                """Validate + persist onboarding, then navigate to ``dest`` on
                success (Finish → the tour; a starter → /chat?prefill=…). On a
                validation failure, notify and stay put — same as Finish."""
                ok, msg = finalize_onboarding(
                    vault, config,
                    name=name_in.value, location=loc_in.value,
                    timezone=tz_in.value, output_dir=out_in.value,
                    role=role_in.value or "", org=org_in.value or "",
                    persona=persona_in.value or "",
                    preset=preset_select.value or "")
                if not ok:
                    ui.notify(msg, type="warning")
                    return
                ui.notify(success_msg, type="positive")
                ui.navigate.to(dest)

            # ── 4. Try it ─────────────────────────────────────────────────
            ui.label(f"4 · {onboarding_steps()[3]}").classes("s-section-head")
            ui.label(
                "Pick a starter below (it lands pre-filled in Chat — you "
                "press Run), or hit Record and do any task once: Systemu "
                "watches and turns it into a repeatable workflow. That's "
                "the superpower."
            ).classes("s-muted")
            from urllib.parse import quote as _q

            def _starter_line(text: str, prompt: str) -> None:
                _label = ui.label(f"›  {text}").classes("s-cell")
                _label.style("cursor: pointer;")
                # Finalize onboarding FIRST, then open the starter pre-filled —
                # a bare navigate to /chat would be bounced back by the W11.4
                # onboarding gate (the "just refreshes" bug).
                _label.on("click",
                          lambda _, p=prompt: _run_finalize(
                              f"/chat?prefill={_q(p)}",
                              "Setup saved — opening your starter…"))

            @ui.refreshable
            def _starters() -> None:
                """The persona answer's first consumer (Charter v2 req 5).

                Step 3's picker re-renders this block; before anything is
                picked `skin_for(None)` yields DEFAULT_SKIN, whose starters
                equal `starter_prompts()` — the pre-registry page, unchanged.
                """
                skin = skin_for(persona_in.value)
                for _p in skin.starters:
                    _starter_line(_p, _p)
                # The dare is a fourth starter, not decoration: DARE_PROMPT is
                # deliberately something the stock toolbox lacks, so clicking
                # it walks the operator into a real forge offer (which still
                # asks for approval before building anything).
                _starter_line(dare_label(skin, config), DARE_PROMPT)

            _starters()
            persona_in.on_value_change(lambda _=None: _starters.refresh())

            def _finish(_=None) -> None:
                # W11.4: setup is enforced (key + name required). Shared
                # validate + persist with the step-4 starters via
                # finalize_onboarding. W11.5 handoff: success flows straight
                # into the guided tour (replayable from Settings).
                _run_finalize("/?tour=0", "All set — welcome aboard.")

            def _later(_=None) -> None:
                try:
                    mark_skipped(vault)
                except Exception:
                    logger.debug("[Welcome] skip sentinel failed", exc_info=True)
                ui.navigate.to("/")

            with ui.row().classes("w-full q-gutter-sm q-mt-md"):
                button("Finish setup", variant="primary", on_click=_finish)
                if not _gate_active:
                    # Voluntary visit (already set up, or pre-W11 skip) —
                    # leaving is fine. Fresh installs get no skip: mandatory.
                    button("Maybe later", variant="ghost", on_click=_later)
