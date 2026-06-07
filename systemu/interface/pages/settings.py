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
