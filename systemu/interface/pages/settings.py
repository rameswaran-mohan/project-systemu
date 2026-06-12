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

    ui.label("Settings").style(
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

            # W8.1: preset fill-in. Applying a preset fills the three tier
            # inputs; "Save Settings" then persists them as the explicit
            # SYSTEMU_TIER*_MODEL vars (which always win over the preset env,
            # so there is never a precedence surprise).
            from sharing_on.model_presets import PRESETS, is_budget_class
            from systemu.interface.design.primitives import button as _ds_btn

            @ui.refreshable
            def _brain_advisory() -> None:
                if is_budget_class(tier1.value):
                    ui.label(
                        "Tier 1 (the reasoning brain) is a flash/free-class "
                        "model — cheap, but it caps task quality. Apply the "
                        "'quality' preset for noticeably better results."
                    ).classes("s-banner s-banner--warn w-full")

            with ui.row().classes("w-full items-center q-gutter-sm"):
                preset_select = ui.select(
                    sorted(PRESETS), label="Preset",
                ).classes("s-input")

                def _apply_preset(_=None):
                    tiers = PRESETS.get(preset_select.value or "")
                    if not tiers:
                        ui.notify("Pick a preset first.", type="info")
                        return
                    tier1.value = tiers["tier1"]
                    tier2.value = tiers["tier2"]
                    tier3.value = tiers["tier3"]
                    _brain_advisory.refresh()
                    ui.notify(
                        f"Preset '{preset_select.value}' filled in — click "
                        "Save Settings to apply.", type="info",
                    )

                _ds_btn("Apply preset", variant="ghost", on_click=_apply_preset)

            _brain_advisory()
            tier1.on("change", lambda _: _brain_advisory.refresh())

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

        # ── Evolution schedule (v0.9 Phase-5 3f) ───────────────────────────
        # Tokenized .s-card wrapper (like Gate Mode) — no new inline f-string
        # style, keeping the UI-style lint gate clean for this render.
        _section_header("Evolution schedule")
        with ui.column().classes("s-card").style("gap: 14px; padding: 20px;"):
            evolution_schedule_card()

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
            key_status = "✓ Set" if config.openrouter_api_key else "✗ Not set — add OPENROUTER_API_KEY to .env"
            key_color  = THEME["success"] if config.openrouter_api_key else THEME["danger"]
            ui.label(key_status).style(f"font-size: 14px; color: {key_color}; font-weight: 600;")
            ui.label("API key is loaded from the .env file. Editing is not supported live for security — update the .env file and restart.").style(
                f"font-size: 12px; color: {THEME['text_muted']};"
            )

        # ── MCP servers (W9.3) ─────────────────────────────────────────────
        _section_header("MCP servers")
        with ui.column().classes("s-card w-full"):
            ui.label(
                "Connect external MCP tool servers (bring-your-own URL; "
                "OAuth-protected servers are not supported yet). Discovered "
                "tools stay OFF until you enable them — enabled connector "
                "tools become callable from Chat's Quick answer lane."
            ).classes("s-muted")

            from systemu.runtime.mcp.connections import (
                add_server, all_servers, enabled_tools, is_tool_enabled,
                remove_server, set_tool_enabled,
            )
            from systemu.interface.design.primitives import button as _mcp_btn

            def _mcp_tool_dialog(server: str, tools: list) -> None:
                from systemu.interface.design import card as _card
                with ui.dialog() as dlg, _card(classes="s-dialog q-pa-lg"):
                    ui.label(f"Tools on {server}").classes("s-dialog-title")
                    if not tools:
                        ui.label("The server reported no tools.").classes("s-muted")
                    for t in tools:
                        with ui.row().classes("w-full items-center q-gutter-sm"):
                            ui.label(t["name"]).classes("s-cell s-cell--bold")
                            ui.label(t.get("description", "")).classes(
                                "s-muted").style("flex: 1;")
                            sw = ui.switch(
                                value=is_tool_enabled(state.vault, server, t["name"]),
                            ).props("dense")

                            def _toggle(e, s=server, tool=t):
                                set_tool_enabled(
                                    state.vault, s, tool["name"],
                                    bool(getattr(e, "value", False)),
                                    description=tool.get("description", ""),
                                    schema=tool.get("schema") or {})
                                _mcp_servers.refresh()

                            sw.on_value_change(_toggle)
                    ui.button("Close", on_click=dlg.close).classes(
                        "s-btn s-btn--ghost q-mt-md")
                dlg.open()

            @ui.refreshable
            def _mcp_servers() -> None:
                servers = all_servers(state.vault)
                if not servers:
                    ui.label("No servers connected.").classes("s-muted")
                for srv in servers:
                    with ui.row().classes("w-full items-center q-gutter-sm"):
                        ui.label(srv).classes("s-mono").style("flex: 1;")

                        async def _discover(_=None, s=srv):
                            import asyncio
                            from systemu.runtime.mcp.client import mcp_list_tools
                            # W7.1 pattern: network off the loop; client
                            # captured before the await for post-await UI.
                            try:
                                client = ui.context.client
                            except Exception:
                                client = None
                            ui.notify(f"Discovering tools on {s}…", type="info")
                            out = await asyncio.to_thread(
                                lambda: mcp_list_tools(server=s))
                            if client is None:
                                return
                            try:
                                with client:
                                    if not out.get("success"):
                                        ui.notify(
                                            f"Discovery failed: {out.get('error')}",
                                            type="negative")
                                        return
                                    _mcp_tool_dialog(s, out.get("tools") or [])
                            except Exception:
                                pass

                        def _remove(_=None, s=srv):
                            remove_server(state.vault, s)
                            ui.notify(f"Removed {s} (its tools are disabled).",
                                      type="info")
                            _mcp_servers.refresh()

                        _mcp_btn("Discover tools", variant="ghost",
                                 on_click=_discover)
                        _mcp_btn("Remove", variant="ghost", on_click=_remove)
                enabled_now = sorted(e["name"] for e in enabled_tools(state.vault))
                if enabled_now:
                    ui.label("Enabled connector tools: " + ", ".join(enabled_now)
                             ).classes("s-muted")

            _mcp_servers()
            with ui.row().classes("w-full items-center q-gutter-sm"):
                mcp_url_in = ui.input(
                    placeholder="http://localhost:8080",
                ).classes("s-input").style("flex: 1;")

                def _add_mcp(_=None):
                    url = (mcp_url_in.value or "").strip()
                    if not url.startswith(("http://", "https://")):
                        ui.notify("Enter a full http(s):// server URL.",
                                  type="warning")
                        return
                    add_server(state.vault, url)
                    mcp_url_in.set_value("")
                    ui.notify("Server added — discover its tools to enable them.",
                              type="positive")
                    _mcp_servers.refresh()

                _mcp_btn("Add server", variant="primary", on_click=_add_mcp)

        # ── Telegram reach (W10.1 — status only, env-configured) ───────────
        # ── Help (W11.5) ───────────────────────────────────────────────────
        _section_header("Help")
        with ui.column().classes("s-card w-full"):
            ui.label(
                "New here, or showing a colleague around? The guided tour "
                "walks the six main screens in two minutes."
            ).classes("s-muted")
            from systemu.interface.design.primitives import button as _tour_btn
            _tour_btn("Replay the tour", variant="ghost",
                      on_click=lambda _=None: ui.navigate.to("/?tour=0"))
            # W12 (B7): the one-page operator SOP ships with the install.
            ui.label(
                "Prefer reading? OPERATOR-SOP.md (next to the app) is the "
                "one-page guide: the record → approve → run → results loop, "
                "what each approval card means, and troubleshooting."
            ).classes("s-muted")

        _section_header("Telegram")
        with ui.column().classes("s-card w-full"):
            import os as _tg_os
            _tg_token = bool(_tg_os.environ.get("SHARING_ON_TELEGRAM_BOT_TOKEN", "").strip())
            _tg_allow = bool(_tg_os.environ.get("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "").strip())
            if _tg_token and _tg_allow:
                ui.label(
                    "Configured — needs-you items and task outcomes are "
                    "pushed to your allowlisted Telegram users; /status "
                    "answers from there."
                ).classes("s-cell")
            else:
                ui.label(
                    "Not configured. Set SHARING_ON_TELEGRAM_BOT_TOKEN and "
                    "SHARING_ON_TELEGRAM_ALLOWED_USER_IDS in .env and "
                    "restart the daemon — tokens are never entered in the "
                    "browser. Requires: pip install 'python-telegram-bot>=20'."
                ).classes("s-muted")

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

        ui.button("Save Settings", on_click=_save).style(
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
                status_text  = f"✓ Connected {masked}".rstrip()
                status_color = THEME["success"]
            else:
                status_text  = "✗ Not connected"
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

    # W2.4: floor-pierce visibility — flags no_floor / override→allow on a
    # floor gate type (gate_mode.floor_pierces), independent of the dial mode.
    from systemu.interface.ui_helpers import render_floor_pierce_banner
    render_floor_pierce_banner()

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

    ui.button("Save Stuck-loop guard", on_click=_save).style(
        f"background: {THEME['primary']}; color: white; border-radius: 8px; margin-top: 8px;")


# ─────────────────────────────────────────────────────────────────────────────
#  Evolution schedule cadence (v0.9 Phase-5 3f / spec §6)
# ─────────────────────────────────────────────────────────────────────────────

_EVOLUTION_HOUR_DEFAULT = 3


def get_evolution_schedule() -> dict:
    """v0.9 Phase-5 3f: read the daily evolution-check hour (UTC) from env.

    Mirrors get_stuck_settings — always returns a sane default if the env var
    is missing, unparseable, or out of the 0-23 range.
    """
    import os
    raw = os.environ.get("SYSTEMU_EVOLUTION_HOUR", str(_EVOLUTION_HOUR_DEFAULT))
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        return {"hour": _EVOLUTION_HOUR_DEFAULT}
    if not (0 <= hour <= 23):
        return {"hour": _EVOLUTION_HOUR_DEFAULT}
    return {"hour": hour}


def save_evolution_schedule(*, hour: int) -> None:
    """v0.9 Phase-5 3f: validate 0-23; persist to .env; patch live os.environ.

    Raises ValueError on out-of-range input (UI surfaces it via ui.notify).
    The APScheduler cron trigger is fixed at daemon boot, so changing this only
    takes effect after a daemon restart (the card notes this).
    """
    import os
    hour = int(hour)
    if not (0 <= hour <= 23):
        raise ValueError("hour must be in 0..23")
    _update_env_var("SYSTEMU_EVOLUTION_HOUR", str(hour))
    os.environ["SYSTEMU_EVOLUTION_HOUR"] = str(hour)


def evolution_schedule_card() -> None:
    """v0.9 Phase-5 3f: render the evolution-cadence section (mirrors
    stuck_settings_card). Caller wraps it in `with ui.column():`."""
    state = get_evolution_schedule()
    # Token-class / plain-string styling only (keeps the UI-style lint gate at 0
    # new violations for this Phase-5 render).
    ui.label(
        "The daily evolution check (skill/tool effectiveness sweep + recalibration "
        "proposals) runs once a day at this hour, in UTC. The manual \"Run check now\" "
        "button on the Evolution page is unaffected by this setting."
    ).classes("s-muted").style("font-size: 12px;")
    with ui.row().style("gap: 16px; align-items: center;"):
        ui.label("Daily run hour (UTC, 0-23):").classes("s-cell").style("font-size: 13px;")
        hour_input = ui.number(label="", value=state["hour"], min=0, max=23).style("width: 110px;")
    ui.label(
        "⚠ The scheduler's cron trigger is fixed when the daemon boots — "
        "restart the daemon to fully apply a changed hour."
    ).classes("s-text-warn").style("font-size: 12px;")

    def _save():
        try:
            save_evolution_schedule(hour=int(hour_input.value
                                             if hour_input.value is not None
                                             else _EVOLUTION_HOUR_DEFAULT))
            ui.notify(
                "Evolution schedule saved. Restart daemon to fully apply.",
                type="positive",
            )
        except ValueError as exc:
            ui.notify(f"Invalid value: {exc}", type="negative")
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    ui.button("Save Evolution Schedule", on_click=_save).props(
        "no-caps").classes("s-btn s-btn--primary").style("margin-top: 8px;")
