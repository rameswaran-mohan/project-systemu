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
            # renamed control + env var.  See notify_user's
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


def _section_header(title: str) -> None:
    ui.label(title).style(
        f"font-size: 14px; font-weight: 700; color: {THEME['text_muted']}; "
        f"text-transform: uppercase; letter-spacing: 0.08em;"
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
