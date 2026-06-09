"""NiceGUI Dashboard — Settings page.

Editable configuration for:
  • LLM tier model names
  • Auto-approve scrolls toggle
  • Vault directory (read-only display)
  • API key status check (write to .env via dotenv)
"""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_settings_page() -> None:
    state = AppState.get()
    config = state.config

    ui.label("⚙️ Settings").style(
        f"font-size: 22px; font-weight: 800; color: {THEME['text']}; margin-bottom: 20px;"
    )

    with ui.column().style("max-width: 680px; gap: 24px;"):

        # ── LLM Tiers ──────────────────────────────────────────────────────
        _section_header("LLM Routing Tiers")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 14px;"
        ):
            tier1 = _model_input(
                "Tier 1 — Deep Reasoning",
                "Scroll refinement, skill extraction, shadow decisions, evolution analysis",
                config.tier1_model,
            )
            tier2 = _model_input(
                "Tier 2 — Structured Output / Code",
                "Tool spec design, code generation, agentic execution decisions",
                config.tier2_model,
            )
            tier3 = _model_input(
                "Tier 3 — Fast / Formatting",
                "Log-to-instruction conversion, context snapshot compaction",
                config.tier3_model,
            )

        # ── Behaviour ──────────────────────────────────────────────────────
        _section_header("Behaviour")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 14px;"
        ):
            # v0.6.1-b: renamed control + env var.  See notify_user's
            # action-ordering contract — this flag auto-picks actions[0] in
            # EVERY prompt, not just scroll approval.
            auto_approve = ui.checkbox(
                "Non-interactive mode (auto-pick the safe-default action in every prompt)",
            ).style(f"color: {THEME['text']};")
            auto_approve.value = config.non_interactive
            ui.label(
                "When enabled, the daemon auto-selects the first (safe-by-default) action "
                "in every notify_user prompt — useful for CI / unattended runs. "
                "Replaces SYSTEMU_AUTO_APPROVE_SCROLLS (which misleadingly cascaded to all prompts)."
            ).style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )

        # ── Stuck-loop guard (v0.8.21) ─────────────────────────────────────
        _section_header("Stuck-loop guard")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 14px;"
        ):
            stuck_settings_card()

        # ── Execution Mode (v0.9.7 Phase 3.2) ────────────────────────────
        _section_header("Execution Mode")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 14px;"
        ):
            adherence_card()

        # ── Gate Mode dial (Phase 3 Batch 3 / spec §4.3 · D4-D5) ──────────
        # Uses the tokenized .s-card class (no new inline f-string style —
        # keeps the UI-style lint gate clean for this Phase-3 render).
        _section_header("Gate Mode")
        with ui.column().classes("s-card").style("gap: 14px; padding: 20px;"):
            gate_mode_card()

        # ── Vault ──────────────────────────────────────────────────────────
        _section_header("Storage")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 8px;"
        ):
            with ui.row().style("align-items: center; gap: 10px;"):
                ui.label("Vault Directory:").style(
                    f"font-size: 13px; color: {THEME['text_muted']}; min-width: 140px;"
                )
                ui.label(config.vault_dir).style(
                    f"font-size: 13px; color: {THEME['text']}; font-family: monospace; "
                    f"background: {THEME['surface2']}; padding: 4px 10px; border-radius: 6px;"
                )

        # ── API Key ────────────────────────────────────────────────────────
        _section_header("OpenRouter API Key")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 10px;"
        ):
            key_status = "✅ Set" if config.openrouter_api_key else "❌ Not set — add OPENROUTER_API_KEY to .env"
            key_color  = THEME["success"] if config.openrouter_api_key else THEME["danger"]
            ui.label(key_status).style(f"font-size: 14px; color: {key_color}; font-weight: 600;")
            ui.label("API key is loaded from the .env file. Editing is not supported live for security — update the .env file and restart.").style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )

        # ── Connections (v0.8.18) ──────────────────────────────────────────
        # Operator enters per-tool API keys here.  Values are stored via
        # CredentialStore (keyring, 0600-file fallback) — never written to
        # .env and never echoed back; only the last-4 of a stored key is shown.
        _section_header("Connections")
        with ui.column().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 12px; padding: 20px; gap: 16px;"
        ):
            ui.label(
                "Connect the credentials your tools declare. Keys are stored "
                "securely in your OS keychain — they are never shown after saving."
            ).style(f"font-size: 12px; color: {THEME['text_muted']};")

            try:
                conn_rows = connection_rows(state.vault)
            except Exception as exc:  # pragma: no cover - defensive UI guard
                conn_rows = []
                ui.label(f"Could not load connections: {exc}").style(
                    f"font-size: 12px; color: {THEME['danger']};"
                )

            if not conn_rows:
                ui.label(
                    "No tools declare credential requirements yet."
                ).style(f"font-size: 13px; color: {THEME['text_muted']};")
            else:
                for row in conn_rows:
                    _connection_card(row)

        # ── Save Button ────────────────────────────────────────────────────
        def _save():
            """Write updated model names + auto-approve back to .env."""
            try:
                _update_env_var("SYSTEMU_TIER1_MODEL", tier1.value.strip())
                _update_env_var("SYSTEMU_TIER2_MODEL", tier2.value.strip())
                _update_env_var("SYSTEMU_TIER3_MODEL", tier3.value.strip())
                _update_env_var("SYSTEMU_NON_INTERACTIVE", "1" if auto_approve.value else "0")
                # Patch live config too (until restart)
                config.tier1_model           = tier1.value.strip() or config.tier1_model
                config.tier2_model           = tier2.value.strip() or config.tier2_model
                config.tier3_model           = tier3.value.strip() or config.tier3_model
                config.non_interactive       = auto_approve.value
                ui.notify("Settings saved to .env. Restart daemon to fully apply.", type="positive")
            except Exception as exc:
                ui.notify(f"Error saving settings: {exc}", type="negative")

        ui.button("💾 Save Settings", on_click=_save).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px; margin-top: 8px;"
        )


def connection_rows(vault) -> list:
    """Rows for tools that declare credential requirements (v0.8.18).

    Enumerates FULL Tool records via the real Vault API — the lightweight
    index header does NOT carry ``requires_credentials``, so we read each
    tool's full record: ``load_index("tools")`` headers + ``get_tool(id)``.
    """
    from systemu.runtime.credentials.store import CredentialStore
    store = CredentialStore()
    rows, seen = [], set()
    try:
        headers = vault.load_index("tools") or []
    except Exception:
        headers = []
    for header in headers:
        tid = header.get("id") if isinstance(header, dict) else None
        if not tid:
            continue
        try:
            t = vault.get_tool(tid)
        except Exception:
            continue
        for req in (getattr(t, "requires_credentials", None) or []):
            sig = (t.name, req.key)
            if sig in seen:
                continue
            seen.add(sig)
            st = store.status(req.key)
            rows.append({"tool": t.name, "key": req.key, "label": req.label,
                         "signup_url": req.signup_url,
                         "present": st["present"], "last4": st["last4"]})
    return rows


def save_credential(key: str, value: str) -> None:
    from systemu.runtime.credentials.store import CredentialStore
    CredentialStore().set(key, value)


def delete_credential(key: str) -> None:
    from systemu.runtime.credentials.store import CredentialStore
    CredentialStore().delete(key)


def _section_header(title: str) -> None:
    ui.label(title).style(
        f"font-size: 14px; font-weight: 700; color: {THEME['text_muted']}; "
        f"text-transform: uppercase; letter-spacing: 0.08em;"
    )


def _connection_card(row: dict) -> None:
    """Render one credential connection: status + masked entry + Save/Disconnect.

    ``row`` is a dict from :func:`connection_rows`:
    {tool, key, label, signup_url, present, last4}.
    """
    key = row["key"]
    with ui.column().style(
        f"background: {THEME['surface2']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 10px; padding: 14px; gap: 8px;"
    ):
        # Header row — label + key id + status badge
        with ui.row().style("align-items: center; gap: 10px; flex-wrap: wrap;"):
            ui.label(row["label"]).style(
                f"font-size: 13px; font-weight: 600; color: {THEME['text']};"
            )
            ui.label(key).style(
                f"font-size: 11px; color: {THEME['text_muted']}; font-family: monospace; "
                f"background: {THEME['surface']}; padding: 2px 8px; border-radius: 6px;"
            )
            if row["present"]:
                last4 = row.get("last4")
                masked = f"••••{last4}" if last4 else ""
                status_text  = f"✅ Connected {masked}".rstrip()
                status_color = THEME["success"]
            else:
                status_text  = "❌ Not connected"
                status_color = THEME["danger"]
            ui.label(status_text).style(
                f"font-size: 12px; font-weight: 600; color: {status_color};"
            )

        if row.get("signup_url"):
            ui.link("Get an API key ↗", row["signup_url"], new_tab=True).style(
                f"font-size: 11px; color: {THEME['primary']};"
            )

        # Masked entry + actions
        with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
            inp = ui.input(placeholder="Paste or type the API key", password=True).style(
                f"flex: 1; min-width: 220px; background: {THEME['surface']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 8px; "
                f"color: {THEME['text']}; font-size: 13px; font-family: monospace;"
            )

            def _save_cred(_=None, key=key, inp=inp, label=row["label"]) -> None:
                value = (inp.value or "").strip()
                if not value:
                    ui.notify("Enter a key before saving.", type="warning")
                    return
                try:
                    save_credential(key, value)
                    inp.value = ""
                    ui.notify(f"{label} connected.", type="positive")
                except Exception as exc:
                    ui.notify(f"Could not save {label}: {exc}", type="negative")

            def _disconnect(_=None, key=key, label=row["label"]) -> None:
                try:
                    delete_credential(key)
                    ui.notify(f"{label} disconnected.", type="info")
                except Exception as exc:
                    ui.notify(f"Could not disconnect {label}: {exc}", type="negative")

            ui.button("Save", on_click=_save_cred).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )
            if row["present"]:
                ui.button("Disconnect", on_click=_disconnect).style(
                    f"background: transparent; color: {THEME['danger']}; "
                    f"border: 1px solid {THEME['danger']}; border-radius: 8px;"
                )


def _model_input(label: str, description: str, current_value: str):
    with ui.column().style("gap: 4px;"):
        ui.label(label).style(f"font-size: 13px; font-weight: 600; color: {THEME['text']};")
        ui.label(description).style(f"font-size: 11px; color: {THEME['text_muted']};")
        inp = ui.input(value=current_value).style(
            f"width: 100%; background: {THEME['surface2']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 8px; "
            f"color: {THEME['text']}; font-size: 13px; font-family: monospace;"
        )
    return inp


def _update_env_var(key: str, value: str) -> None:
    """Update a single variable in the .env file."""
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


def get_adherence_settings() -> dict:
    """v0.9.7: read the current execution-adherence value from env."""
    import os
    raw = (os.environ.get("SYSTEMU_EXECUTION_ADHERENCE") or "auto").strip().lower()
    if raw not in ("auto", "free", "guided", "strict"):
        raw = "auto"
    return {"execution_adherence": raw}


def save_adherence_settings(*, execution_adherence: str) -> None:
    """v0.9.7: validate + persist execution_adherence to .env; patch live os.environ."""
    import os
    if execution_adherence not in ("auto", "free", "guided", "strict"):
        raise ValueError(
            f"execution_adherence must be one of auto/free/guided/strict, got {execution_adherence!r}"
        )
    _update_env_var("SYSTEMU_EXECUTION_ADHERENCE", execution_adherence)
    os.environ["SYSTEMU_EXECUTION_ADHERENCE"] = execution_adherence


def save_adherence_preset(preset_name: str) -> None:
    """v0.9.7: apply a named preset — writes all preset env vars to .env + os.environ."""
    import os
    from systemu.runtime.adherence import apply_preset
    env_map = apply_preset(preset_name)  # raises KeyError for unknown names
    for key, value in env_map.items():
        _update_env_var(key, value)
        os.environ[key] = value


def adherence_card() -> None:
    """v0.9.7: render the Execution Mode section. Caller wraps it in `with ui.column():`."""
    state = get_adherence_settings()

    _ADHERENCE_OPTIONS = [
        ("auto", "Auto — infer from request kind (chat→free, SOP→guided)"),
        ("free", "Free Agent — first-principles reasoning, SOP is advisory"),
        ("guided", "Guided Autonomy — follow intent + key steps, adapt details"),
        ("strict", "Strict Replay — follow SOP step-by-step, no deviation"),
    ]
    _PRESET_OPTIONS = [
        ("", "— apply a preset —"),
        ("locked_sop", "Locked SOP — strict adherence, no auto-grants"),
        ("assisted", "Assisted — guided adherence, conservative auto-grants"),
        ("autonomous", "Autonomous — free agent, broad auto-grants (dev/testing)"),
    ]

    ui.label(
        "Controls how tightly the agent follows recorded SOPs vs. exercising "
        "autonomous judgment. 'Auto' defers to the request kind; explicit values "
        "override globally. Presets set adherence + harness auto-grant flags together."
    ).style(f"font-size: 12px; color: {THEME['text_muted']};")

    with ui.row().style("gap: 16px; align-items: center; flex-wrap: wrap;"):
        ui.label("Adherence level:").style(f"font-size: 13px; color: {THEME['text']};")
        adherence_sel = ui.select(
            options={k: v for k, v in _ADHERENCE_OPTIONS},
            value=state["execution_adherence"],
        ).style("min-width: 320px;")

    with ui.row().style("gap: 16px; align-items: center; flex-wrap: wrap; margin-top: 8px;"):
        ui.label("Quick preset:").style(f"font-size: 13px; color: {THEME['text']};")
        preset_sel = ui.select(
            options={k: v for k, v in _PRESET_OPTIONS},
            value="",
        ).style("min-width: 320px;")
        ui.label(
            "Applying a preset overwrites adherence + all harness auto-grant flags."
        ).style(f"font-size: 11px; color: {THEME['text_muted']};")

    def _save_adherence():
        try:
            save_adherence_settings(execution_adherence=adherence_sel.value)
            ui.notify(
                f"Execution mode set to '{adherence_sel.value}'. "
                "Restart daemon to fully apply.",
                type="positive",
            )
        except (ValueError, Exception) as exc:
            ui.notify(f"Error saving execution mode: {exc}", type="negative")

    def _apply_preset():
        pname = preset_sel.value
        if not pname:
            ui.notify("Select a preset first.", type="warning")
            return
        try:
            save_adherence_preset(pname)
            # Update the adherence selector to reflect the preset's level
            from systemu.runtime.adherence import ADHERENCE_PRESETS
            new_level = ADHERENCE_PRESETS[pname].get("SYSTEMU_EXECUTION_ADHERENCE", "auto")
            adherence_sel.value = new_level
            preset_sel.value = ""
            ui.notify(
                f"Preset '{pname}' applied. Restart daemon to fully apply.",
                type="positive",
            )
        except (KeyError, Exception) as exc:
            ui.notify(f"Error applying preset: {exc}", type="negative")

    with ui.row().style("gap: 8px; margin-top: 8px;"):
        ui.button("Save Adherence", on_click=_save_adherence).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px;"
        )
        ui.button("Apply Preset", on_click=_apply_preset).style(
            f"background: transparent; color: {THEME['primary']}; "
            f"border: 1px solid {THEME['primary']}; border-radius: 8px;"
        )


# ── Gate Mode dial (Phase 3 Batch 3 / spec §4.3 · D4-D5) ─────────────────────

# The three modes, in dial order, with human-readable labels.
_GATE_MODE_OPTIONS = {
    "bypass":       "Bypass — auto-grant everything except the floor (DANGER)",
    "risk_tiered":  "Risk-tiered — the Governor (grant low-risk, ask the rest)",
    "approve_only": "Approve-only — always ask, never auto-grant",
}

# Per-gate-type override verdicts an operator may pin in the advanced grid.
_GATE_OVERRIDE_VERDICTS = {
    "":     "— mode default —",
    "allow": "Always allow (auto-grant)",
    "ask":   "Always ask",
    "deny":  "Always deny (audit-only)",
}

# Gate types the override grid exposes (the adapters that flow into the queue).
_GATE_TYPES = ("scroll", "dep", "forge", "evolution", "recovery", "harness")


def _gate_mode_card_model(settings: dict) -> dict:
    """Pure, NiceGUI-free model for the gate_mode_card.

    Decides whether the persistent Bypass DANGER banner is shown (D4/P12 — we
    are NEVER silent about Bypass) and exposes the mode option list so the
    rendering logic is unit-testable without standing up NiceGUI.
    """
    mode = (settings or {}).get("mode", "risk_tiered")
    return {
        "mode": mode,
        "mode_options": dict(_GATE_MODE_OPTIONS),
        # The danger banner is shown IFF the active mode is Bypass.
        "show_danger_banner": mode == "bypass",
        "overrides": dict((settings or {}).get("overrides") or {}),
        "no_floor": bool((settings or {}).get("no_floor", False)),
    }


def gate_mode_card() -> None:
    """Render the always-visible Gate-Mode dial. Caller wraps it in a column.

    Mirrors ``adherence_card``: a mode select bound to the persisted setting,
    an advanced expander with a per-gate-type override grid + floor editor, and
    a PERSISTENT danger banner whenever the dial is on Bypass (D4/P12)."""
    from systemu.runtime.gate_mode_settings import (
        get_gate_mode_settings,
        save_gate_mode_settings,
    )
    from systemu.interface.command.gate_mode import (
        FLOOR_GATE_TYPES,
        FLOOR_CAPABILITIES,
    )

    model = _gate_mode_card_model(get_gate_mode_settings())

    ui.label(
        "Controls whether reverse-harness gates (scroll/dep/forge/evolution/"
        "recovery/harness) are auto-granted, asked, or denied. Risk-tiered is "
        "the Governor; Bypass auto-grants everything except the safety floor."
    ).classes("s-muted").style("font-size: 12px;")

    # ── Persistent Bypass danger banner (D4 / P12 — never silent) ─────────────
    @ui.refreshable
    def _danger_banner(mode_value: str) -> None:
        if mode_value != "bypass":
            return
        with ui.element("div").classes("s-banner s-banner--danger").style(
            "margin: 4px 0;"
        ):
            ui.icon("warning")
            ui.label(
                "BYPASS is ON — gates are auto-granted (except the safety floor). "
                "Destructive actions can run without your approval."
            )

    _danger_banner(model["mode"])

    with ui.row().style("gap: 16px; align-items: center; flex-wrap: wrap;"):
        ui.label("Gate mode:").style("font-size: 13px;")
        mode_sel = ui.select(
            options=model["mode_options"],
            value=model["mode"],
        ).style("min-width: 360px;")
        mode_sel.on(
            "update:model-value",
            lambda _: _danger_banner.refresh(mode_sel.value),
        )

    # ── Advanced: per-gate-type override grid + floor editor ──────────────────
    override_sels: dict = {}
    no_floor_box = None
    with ui.expansion("Advanced — per-gate overrides & floor", icon="tune").style(
        "margin-top: 4px;"
    ):
        ui.label(
            "Pin a verdict per gate type (overrides the mode AND the floor)."
        ).classes("s-muted").style("font-size: 12px;")
        for gate_type in _GATE_TYPES:
            with ui.row().style("gap: 12px; align-items: center; flex-wrap: wrap;"):
                ui.label(gate_type).style("font-size: 13px; min-width: 110px;")
                override_sels[gate_type] = ui.select(
                    options=dict(_GATE_OVERRIDE_VERDICTS),
                    value=model["overrides"].get(gate_type, ""),
                ).style("min-width: 240px;")

        ui.separator().classes("s-sep")
        ui.label("Safety floor (D5)").classes("s-field-label").style(
            "margin-top: 8px;"
        )
        ui.label(
            "These gate types / capabilities ALWAYS ask, even under Bypass — "
            "unless you disable the floor entirely."
        ).classes("s-muted").style("font-size: 12px;")
        with ui.row().style("gap: 8px; flex-wrap: wrap;"):
            for gt in sorted(FLOOR_GATE_TYPES):
                ui.html(f'<span class="s-pill s-pill--warn">{gt}</span>')
            for cap in sorted(FLOOR_CAPABILITIES):
                ui.html(f'<span class="s-pill s-pill--muted">{cap}</span>')
        no_floor_box = ui.checkbox(
            "Disable the safety floor (no_floor) — NOT recommended",
            value=model["no_floor"],
        )

    def _save_gate_mode():
        try:
            overrides = {
                gt: sel.value
                for gt, sel in override_sels.items()
                if sel.value  # skip "" (= mode default)
            }
            save_gate_mode_settings(
                mode=mode_sel.value,
                overrides=overrides,
                no_floor=bool(no_floor_box.value) if no_floor_box else False,
            )
            _danger_banner.refresh(mode_sel.value)
            ui.notify(
                f"Gate mode set to '{mode_sel.value}'. Restart daemon to fully apply.",
                type="positive",
            )
        except (ValueError, Exception) as exc:
            ui.notify(f"Error saving gate mode: {exc}", type="negative")

    with ui.row().style("gap: 8px; margin-top: 8px;"):
        from systemu.interface.design.primitives import button as _s_button
        _s_button("Save Gate Mode", variant="primary", on_click=_save_gate_mode)


def get_stuck_settings() -> dict:
    """v0.8.21: read current values from env. Always returns sane defaults if missing."""
    import os
    return {
        "guard_on":    (os.environ.get("SYSTEMU_STUCK_GUARD", "on") or "on").lower() != "off",
        "no_progress": int(os.environ.get("SYSTEMU_STUCK_NO_PROGRESS", "5") or "5"),
        "tool_fails":  int(os.environ.get("SYSTEMU_STUCK_TOOL_FAILS", "3") or "3"),
    }


def save_stuck_settings(*, guard_on: bool, no_progress: int, tool_fails: int) -> None:
    """v0.8.21: validate ranges; persist to .env; patch live os.environ.
    Raises ValueError on out-of-range input (UI surfaces the error via ui.notify)."""
    if not (1 <= int(no_progress) <= 30):
        raise ValueError("no_progress must be in 1..30")
    if not (1 <= int(tool_fails) <= 10):
        raise ValueError("tool_fails must be in 1..10")
    import os
    g_str = "on" if guard_on else "off"
    _update_env_var("SYSTEMU_STUCK_GUARD", g_str)
    _update_env_var("SYSTEMU_STUCK_NO_PROGRESS", str(int(no_progress)))
    _update_env_var("SYSTEMU_STUCK_TOOL_FAILS", str(int(tool_fails)))
    os.environ["SYSTEMU_STUCK_GUARD"] = g_str
    os.environ["SYSTEMU_STUCK_NO_PROGRESS"] = str(int(no_progress))
    os.environ["SYSTEMU_STUCK_TOOL_FAILS"] = str(int(tool_fails))


def stuck_settings_card() -> None:
    """v0.8.21: render the Stuck-loop guard section. Caller wraps it in `with ui.column():`."""
    state = get_stuck_settings()
    guard_cb = ui.checkbox("Enable stuck-loop guard (pause when the agent stops making progress)").style(
        f"color: {THEME['text']};")
    guard_cb.value = state["guard_on"]
    ui.label(
        "When enabled, the runtime pauses for operator input if the agent makes no "
        "objective progress for N iterations OR the same tool fails N consecutive times. "
        "Tunables below apply on the NEXT iteration — no daemon restart needed."
    ).style(f"font-size: 12px; color: {THEME['text_muted']};")
    with ui.row().style("gap: 16px; align-items: center;"):
        ui.label("Iterations without progress before pause:").style(
            f"font-size: 13px; color: {THEME['text']};")
        no_prog = ui.number(label="", value=state["no_progress"], min=1).style("width: 110px;")
    with ui.row().style("gap: 16px; align-items: center;"):
        ui.label("Consecutive same-tool failures before pause:").style(
            f"font-size: 13px; color: {THEME['text']};")
        tool_fails = ui.number(label="", value=state["tool_fails"], min=1).style("width: 110px;")

    def _save():
        try:
            save_stuck_settings(guard_on=guard_cb.value,
                                 no_progress=int(no_prog.value or 5),
                                 tool_fails=int(tool_fails.value or 3))
            ui.notify("Stuck-loop guard saved (live for next iteration).", type="positive")
        except ValueError as exc:
            ui.notify(f"Invalid value: {exc}", type="negative")
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    ui.button("💾 Save Stuck-loop guard", on_click=_save).style(
        f"background: {THEME['primary']}; color: white; border-radius: 8px; margin-top: 8px;")
