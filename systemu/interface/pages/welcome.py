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
    """Notice a provider added to .env while /welcome is open (W11.4).

    THE RE-CHECK WRITER — and only a writer: it never decides whether the
    install is usable (that verdict comes from the mint, see
    ``finalize_onboarding``). Credentials are never typed in the browser, so the
    operator edits .env in their editor and clicks Re-check; this reads the live
    environment first, then the named .env file, WITHOUT stomping the process
    environment (no ``load_dotenv`` override — the daemon's env stays exactly as
    it booted), and writes what it finds onto the live config snapshot the mint
    is about to score. Returns True when at least one provider value was
    refreshed. Never raises.

    IT REFRESHES EVERY PROVIDER, not just OpenRouter. It used to read
    ``OPENROUTER_API_KEY`` alone, which made Re-check useless for the very
    remedies step 1 offers a few lines below: an operator who added a Google key
    — or who pointed ``OLLAMA_URL`` at a server they had just started, the one
    remedy needing no credential at all — clicked Re-check and was told nothing
    had changed. The attribute/env-var pairing is read off
    ``provider_status.PROVIDER_SPECS``, so this page names no credential of its
    own and a sixth provider is picked up with nobody editing this function.
    """
    import os
    from systemu.runtime import provider_status as _ps

    file_values = None   # read the .env at most once, and only if needed
    found = False
    for spec in _ps.PROVIDER_SPECS:
        value = (os.environ.get(spec.env, "") or "").strip()
        if not value:
            if file_values is None:
                try:
                    from dotenv import dotenv_values
                    file_values = dotenv_values(env_file) or {}
                except Exception:
                    file_values = {}
            raw = file_values.get(spec.env)
            value = raw.strip() if type(raw) is str else ""
        if not value:
            continue
        try:
            setattr(config, spec.attr, value)
            found = True
        except Exception:
            logger.debug("[Welcome] could not refresh %s", spec.provider,
                         exc_info=True)
    return found


def presets_that_run_here(config, usable) -> list:
    """The step-2 presets whose EVERY tier would route to a usable provider.

    ``[(name, [provider ids it uses]), ...]``, in dropdown order.

    THE REMEDY LIST IS COMPUTED, NEVER ASSERTED. "Pick a preset below that uses
    Ollama" is a true sentence only while such a preset is actually in the
    dropdown three lines down, and today none is: every shipped preset names a
    cloud model id. Adding a local preset later turns the offer on with nobody
    editing the copy, and a preset that is only PARTLY local never qualifies --
    one unkeyed tier is still a failed task.

    The current per-tier provider OVERRIDES are applied while scoring, because
    picking a preset changes the tier MODELS only: an operator pinned to a dead
    provider is not rescued by any preset, and must not be told they are.

    Scored with the mint's own ``routed_provider`` -- the same resolution the
    SELECTION comes from, so an offer made here cannot rest on different
    routing from the sentence that makes it.
    """
    from sharing_on.model_presets import PRESETS
    from systemu.runtime import provider_status as _ps
    out = []
    for name in sorted(PRESETS):
        tiers = PRESETS.get(name) or {}
        served: "List[str] | None" = []
        for i in (1, 2, 3):
            try:
                override = getattr(config, f"tier{i}_provider", "")
            except Exception:
                override = ""
            pid = _ps.routed_provider(tiers.get(f"tier{i}", ""), override, config)
            if pid not in usable:
                served = None
                break
            if pid not in served:
                served.append(pid)
        if served:
            out.append((name, served))
    return out


def _sentence(text: str) -> str:
    """End a borrowed clause exactly once (mint details already carry '?')."""
    body = (text or "").strip()
    return body if body.endswith((".", "!", "?")) else body + "."


def tier_readiness_warning(statuses, config) -> str:
    """Step 1's second sentence: "...but not for the tiers you are on". Or "".

    THE GAP DEC-43 LEFT. Making satisfaction unconditional on selection was
    right, and it means step 1 now tells the keyless operator "Provider ready:
    Ollama" and Finish lets them through. Both true -- about SATISFACTION. The
    router routes by the tier MODELS, and the shipped defaults are served by
    OpenRouter, which has no key on that machine. The first starter click fails.

    The second fact already had a mint (``unusable_selected``) and a surface
    (the Settings red flag). It did not have one HERE, which is where a fresh
    operator stands. So this consumes that mint -- the same ``statuses`` the
    "Provider ready" line above was minted from, so the two sentences cannot be
    about different observations -- and adds no verdict of its own.

    It does not derive the SELECTION it scores either: ``routed_tier_providers``
    is the mint's, and Settings reads the same one. This page briefly owned that
    derivation while the Settings banner used another, which is how one screen
    warned about a machine the other called fine.

    Silent when nothing is satisfied: that machine belongs to the no-provider
    banner, and two banners each telling half of "you cannot run" is the
    reader's version of two verdicts. ASCII (DEC-32c). Never raises.
    """
    from systemu.runtime import provider_status as _ps
    try:
        satisfied = _ps.satisfied_providers(statuses)
        if not satisfied:
            return ""
        unusable = _ps.unusable_selected(
            statuses, _ps.routed_tier_providers(config))
        if not unusable:
            return ""
        blocked = ", ".join(u.display for u in unusable)
        ready = ", ".join(s.display for s in satisfied)
        # THE REMEDY TEXT IS THE MINT'S OWN, per blocked provider: "add
        # GOOGLE_API_KEY to .env" for a key-based one, "is `ollama serve`
        # running?" for the keyless one. Writing a remedy here would be a
        # second recipe, and it would tell an Ollama operator to add a key.
        detail = (unusable[0].detail if len(unusable) == 1 else
                  "; ".join(f"{u.display}: {u.detail}" for u in unusable))
        line = (f"Heads up: your model tiers are set to run on {blocked}, "
                f"which this machine cannot use yet, so the first real task "
                f"would fail. ")
        runnable = presets_that_run_here(config,
                                         {s.provider for s in satisfied})
        if runnable:
            name, served = runnable[0]
            serves = ", ".join(_ps.SPEC_BY_PROVIDER[p].display for p in served)
            line += (f"Pick the '{name}' preset in step 2 below - it runs on "
                     f"{serves}, which is working here. Or fix the provider "
                     f"instead: {_sentence(detail)}")
        else:
            # No preset changes the answer, so do not imply one does.
            line += (f"No preset in step 2 runs entirely on {ready}, so the "
                     f"fix is on the provider side: {_sentence(detail)}")
        return line.encode("ascii", "backslashreplace").decode("ascii")
    except Exception:
        logger.debug("[Welcome] tier readiness line failed", exc_info=True)
        return ""


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
        return (False, "Set up an LLM provider first (step 1) - Systemu can't "
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
    # P2d: local first-run funnel. Inside the SUCCESS branch only - a run that
    # bounced off the provider gate or the name gate above returned already, so
    # "Finished setup" can never be stamped for a setup that did not finish.
    from systemu.runtime.funnel import mark_milestone
    mark_milestone(vault, "setup_finished")
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

    # P2d: local first-run funnel - the first step of the journey table on
    # /insights. One small JSON sidecar in the vault, no network, never raises;
    # first-write-wins, so a returning visitor does not move the date.
    from systemu.runtime.funnel import mark_milestone
    mark_milestone(vault, "welcome_rendered")

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
            #
            # THE SAME WITNESS FINISH SPENDS. This used to hand the mint
            # `unprobed` unless a tier already named the keyless provider, so on
            # a keyless machine with Ollama answering it printed "No LLM
            # provider is usable yet" — under a Finish button that (once
            # unified) accepts that machine, and on an install whose daemon had
            # already booted on it. A status line and the gate beside it must be
            # the same claim. The render cost is bounded by the mint's 20 s memo,
            # which the health banner on this very page has usually just warmed.
            from systemu.runtime import provider_status as _ps
            _view = _ps.env_overlay(config)
            _pstat = _ps.all_provider_statuses(
                _view, cache_ttl_s=_ps.PROBE_CACHE_TTL_S)
            _usable = _ps.satisfied_providers(_pstat)
            if _usable:
                ui.label(
                    "Provider ready: " + ", ".join(u.display for u in _usable)
                    + " — you're set to run tasks."
                ).classes("s-cell")
                # "Ready" is a claim about SATISFACTION; the router routes by
                # the tier MODELS. On the machine DEC-43 was found on those are
                # two different providers, and the line above alone would send a
                # fresh operator into a first task that cannot run. Same
                # `_pstat`, so the caveat and the claim are one observation.
                _tier_warn = tier_readiness_warning(_pstat, _view)
                if _tier_warn:
                    ui.label(_tier_warn).classes("s-banner s-banner--warn w-full")
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
