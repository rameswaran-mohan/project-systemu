"""Systemu CLI command groups — registered under the main sharing_on CLI.

Phase S1 Groups:
  scrolls   list / show / refine / approve
  army      list / show / awaken / execute
  tools     list / forge
  skills    list
  settings  show

Phase S2 Groups:
  evolve    run / show-pending
  daemon    start / stop / status

All commands share a single Vault and Config instance via Click context.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Force utf-8 standard out encoding to prevent Windows cp1252 crashes on emojis
if sys.stdout.encoding.lower() != 'utf-8' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding.lower() != 'utf-8' and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

os.environ["PYTHONIOENCODING"] = "utf-8"
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

# D12: THE consent rule for every command that creates a standing permission --
# whether there is a human to ask, what counts as a yes, and which exit code
# says so. `roots grant` and the census grant both sit on it; before they did,
# one of them took a `y` off a pipe as informed consent and the other refused.
# Each command still owns its own disclosure and refusal COPY.
from systemu.interface import consent_prompt

console = Console()


def _maybe_install_bridge_writer() -> None:
    """v0.8.6: when run as an execute subprocess from the dashboard,
    JobManager sets SYSTEMU_EVENT_BRIDGE_FILE. Install the writer so this
    subprocess's EventBus events surface to the dashboard.
    """
    import os
    bridge_file = os.environ.get("SYSTEMU_EVENT_BRIDGE_FILE", "")
    if not bridge_file:
        return
    try:
        from systemu.interface.event_bridge_writer import install_bridge_writer
        install_bridge_writer(bridge_file)
    except Exception:
        # Bridge install failure must not break the subprocess
        pass


# ─── Shared initialiser ───────────────────────────────────────────────────────

def _get_vault_and_config(ctx: click.Context):
    """Return (Config, Vault) from the Click context object, initialising if needed."""
    from sharing_on.config import Config
    from systemu.vault.factory import open_vault
    from systemu.interface.notifications import set_vault
    from systemu.pipelines.activity_extractor import init_pipeline

    obj = ctx.ensure_object(dict)
    if "config" not in obj:
        obj["config"] = Config.from_env()
    if "vault" not in obj:
        cfg = obj["config"]
        # open_vault respects SYSTEMU_STORAGE so CLI subprocesses and the
        # dashboard always write to the same backend (SQLite, file, etc.).
        vlt = open_vault(cfg)
        obj["vault"] = vlt
        set_vault(vlt)
        init_pipeline(cfg, vlt)

    return obj["config"], obj["vault"]


# ── v0.8.0 Pattern 1 — Pending-decision exit wrapper ─────────────────────────

def _handle_pending_decision_or_run(ctx, work):
    """Run ``work()`` and translate ``PendingOperatorDecision`` into a clean
    exit-75 (EX_TEMPFAIL) with an operator-friendly message.

    v0.8.0 Pattern 1: when a CLI command running in queue mode
    (SYSTEMU_DECISION_QUEUE=true, no-TTY) hits a notify_user call without a
    resolved decision, the queue persists a pending record and raises
    PendingOperatorDecision. This wrapper catches that exception and prints
    a clear "queued for operator review" message instead of letting the
    traceback escape, then exits with code 75 so the JobManager / scheduler
    can tell the difference between "failed" and "waiting for operator".
    """
    from systemu.approval.exceptions import PendingOperatorDecision
    try:
        return work()
    except PendingOperatorDecision as pd:
        console.print(
            f"[yellow]⏸  Queued for operator review.[/yellow]\n"
            f"   Decision ID:   [bold]{pd.decision_id}[/bold]\n"
            f"   Question key:  {pd.dedup_key}\n"
            f"   Options:       {', '.join(pd.options)}\n"
            f"\n"
            f"   Resolve via dashboard at /insights → Pending Actions tab,\n"
            f"   or:  [bold]systemu decisions resolve {pd.decision_id} --choice <option>[/bold]\n"
            f"\n"
            f"   Re-run this command after resolving to pick up the operator's choice."
        )
        ctx.exit(75)  # EX_TEMPFAIL


# ─────────────────────────────────────────────────────────────────────────────
#  scrolls group
# ─────────────────────────────────────────────────────────────────────────────

@click.group("scrolls")
def scrolls_group():
    """Manage Scrolls -- refined SOPs extracted from capture sessions."""


@scrolls_group.command("list")
@click.option("--status", "-s", default=None, help="Filter by status (e.g. pending_approval).")
@click.pass_context
def scrolls_list(ctx, status: Optional[str]):
    """List all Scrolls in the vault."""
    _, vault = _get_vault_and_config(ctx)
    from systemu.core.models import ScrollStatus
    filter_status = ScrollStatus(status) if status else None
    scrolls = vault.list_scrolls(status=filter_status)

    if not scrolls:
        console.print("[dim]No scrolls found.[/dim]")
        return

    table = Table(title="📜 Scrolls", show_lines=True)
    table.add_column("ID",      style="cyan",   no_wrap=True)
    table.add_column("Name",    style="bold")
    table.add_column("Status",  style="yellow")
    table.add_column("Session", style="dim")
    table.add_column("Tags",    style="dim")

    for s in scrolls:
        table.add_row(
            s["id"],
            s["name"],
            s["status"],
            s.get("source_session_id", "—"),
            ", ".join(s.get("tags", [])) or "—",
        )
    console.print(table)


@scrolls_group.command("show")
@click.argument("scroll_id")
@click.pass_context
def scrolls_show(ctx, scroll_id: str):
    """Show full detail of a Scroll."""
    _, vault = _get_vault_and_config(ctx)
    try:
        scroll = vault.get_scroll(scroll_id)
    except KeyError:
        console.print(f"[red]Scroll not found: {scroll_id}[/red]")
        sys.exit(1)

    console.print(Panel(
        f"[bold]{scroll.name}[/bold]\n\n"
        f"[dim]ID:[/dim]      {scroll.id}\n"
        f"[dim]Status:[/dim]  {scroll.status}\n"
        f"[dim]Session:[/dim] {scroll.source_session_id}\n"
        f"[dim]Tags:[/dim]    {', '.join(scroll.tags) or 'none'}\n\n"
        f"[bold]Narrative:[/bold]\n{scroll.narrative_md}",
        title="📜 Scroll Detail",
        border_style="cyan",
    ))

    if scroll.action_blocks:
        table = Table(title="Action Blocks", show_lines=True)
        table.add_column("#",        width=4,  style="dim")
        table.add_column("Action",   style="cyan")
        table.add_column("Target",   style="bold")
        table.add_column("App",      style="dim")
        table.add_column("Outcome",  style="dim")

        for ab in scroll.action_blocks:
            table.add_row(
                str(ab.step_number),
                ab.action,
                ab.target[:60] + "…" if len(ab.target) > 60 else ab.target,
                ab.application or "—",
                ab.expected_outcome[:50] + "…" if len(ab.expected_outcome) > 50 else ab.expected_outcome,
            )
        console.print(table)


@scrolls_group.command("refine")
@click.argument("session_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--auto", is_flag=True, help="Auto-approve the scroll (skip user prompt).")
@click.pass_context
def scrolls_refine(ctx, session_dir: str, auto: bool):
    """Refine a capture session into a Scroll (Stage 2).

    If instructions.md does not exist yet, the analyze pipeline is run first
    to generate it from the raw captured events.
    """
    config, vault = _get_vault_and_config(ctx)
    session_path = Path(session_dir)
    instructions_path = session_path / "instructions.md"

    console.print(f"\n[cyan]⚡ Refining session:[/cyan] {session_dir}\n")

    # Stage 1.5 — generate instructions.md if not already present
    if not instructions_path.exists():
        console.print("[dim]instructions.md not found — running analyze pipeline first...[/dim]")
        try:
            from sharing_on.events.store import EventStore
            from sharing_on.analyzer.unifier import unify_events
            from sharing_on.analyzer.step_detector import StepDetector
            from sharing_on.analyzer.generator import generate_instructions
            from sharing_on.output.markdown import render_markdown
            import json as _json

            db_file = session_path / "events.db"
            meta_file = session_path / "session.json"
            if not db_file.exists():
                console.print(f"[red]Error:[/red] No events.db in {session_dir}")
                sys.exit(1)

            meta = _json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            store = EventStore(db_file)
            events = unify_events(store.get_all_events())
            steps = StepDetector().detect_steps(events)

            if not steps:
                console.print("[yellow]⚠ No steps detected in session — cannot refine.[/yellow]")
                sys.exit(1)

            console.print(f"  [green]✓[/green] Detected {len(steps)} steps — generating instructions...")
            # v0.9.35 P2: run intent extraction on this path too (it previously
            # only generated instructions), threading the record-time
            # generalization out of session.json so params reach the markdown.
            from sharing_on.cli import _extract_intent_for_meta
            from sharing_on.analyzer.intent_extractor import write_intent_json

            intent = _extract_intent_for_meta(
                meta, steps=steps, events=events, config=config,
            )
            write_intent_json(intent, session_path)

            instructions = generate_instructions(
                steps=steps,
                session_name=meta.get("name", session_path.name),
                platform_info=meta.get("platform", "Unknown"),
                duration_seconds=0.0,
                api_key=config.openrouter_api_key,
                base_url=config.openrouter_base_url,
                model=config.tier3_model,
                intent=intent if intent.is_usable else None,
            )
            render_markdown(
                instructions=instructions,
                steps=steps,
                session_name=meta.get("name", session_path.name),
                session_id=meta.get("session_id", session_path.name),
                platform_info=meta.get("platform", ""),
                start_time=None,
                end_time=None,
                output_dir=session_path,
                event_count=len(events),
                intent=intent if intent.is_usable else None,
            )
            console.print(f"  [green]✓[/green] instructions.md generated")
        except Exception as exc:
            console.print(f"[red]Error during analyze:[/red] {exc}")
            import traceback; traceback.print_exc()
            sys.exit(1)

    # Stage 2 — Refine into Scroll
    from systemu.pipelines.scroll_refiner import refine_scroll
    from systemu.approval.exceptions import PendingOperatorDecision
    def _refine_work():
        try:
            scroll = refine_scroll(session_path, config, vault, auto_proceed=auto)
            console.print(f"\n[green]✓ Scroll created:[/green] {scroll.id} — status: {scroll.status}")
        except PendingOperatorDecision:
            raise   # v0.8.19: let the wrapper park it (exit 75) so re-run resumes with the answer
        except Exception as exc:
            console.print(f"\n[red]Error:[/red] {exc}")
            import traceback; traceback.print_exc()
            sys.exit(1)
    _handle_pending_decision_or_run(ctx, _refine_work)


@scrolls_group.command("approve")
@click.argument("scroll_id")
@click.pass_context
def scrolls_approve(ctx, scroll_id: str):
    """Approve a PENDING_APPROVAL scroll and trigger activity extraction (Stages 3-6)."""
    def _work():
        config, vault = _get_vault_and_config(ctx)
        from systemu.pipelines import activity_extractor as ae
        ae.init_pipeline(config, vault)
        from systemu.pipelines.scroll_refiner import approve_pending_scroll

        try:
            scroll = approve_pending_scroll(scroll_id, vault)
            console.print(f"\n[green]✓ Scroll {scroll_id} approved — pipeline running.[/green]")
        except (ValueError, KeyError) as exc:
            console.print(f"\n[red]Error:[/red] {exc}")
            sys.exit(1)
    _handle_pending_decision_or_run(ctx, _work)


# ─────────────────────────────────────────────────────────────────────────────
#  army group
# ─────────────────────────────────────────────────────────────────────────────

@click.group("army")
def army_group():
    """Manage the Shadow Army -- autonomous agent personas."""


@army_group.command("list")
@click.option("--status", "-s", default=None, help="Filter by status.")
@click.pass_context
def army_list(ctx, status: Optional[str]):
    """List all Shadows in the vault."""
    _, vault = _get_vault_and_config(ctx)
    from systemu.core.models import ShadowStatus
    filter_status = ShadowStatus(status) if status else None
    shadows = vault.list_shadows(status=filter_status)

    if not shadows:
        console.print("[dim]No shadows found.[/dim]")
        return

    table = Table(title="👥 Shadow Army", show_lines=True)
    table.add_column("ID",         style="cyan",  no_wrap=True)
    table.add_column("Name",       style="bold")
    table.add_column("Status",     style="yellow")
    table.add_column("Activities", justify="right")
    table.add_column("Skills",     justify="right")
    table.add_column("Tools",      justify="right")

    for s in shadows:
        table.add_row(
            s["id"], s["name"], s["status"],
            str(s.get("activity_count", 0)),
            str(len(s.get("skill_ids", []))),
            str(len(s.get("tool_ids", []))),
        )
    console.print(table)


@army_group.command("show")
@click.argument("shadow_id")
@click.pass_context
def army_show(ctx, shadow_id: str):
    """Show full detail of a Shadow."""
    _, vault = _get_vault_and_config(ctx)
    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        console.print(f"[red]Shadow not found: {shadow_id}[/red]")
        sys.exit(1)

    console.print(Panel(
        f"[bold]{shadow.name}[/bold]  ({shadow.status})\n\n"
        f"[dim]ID:[/dim]          {shadow.id}\n"
        f"[dim]Description:[/dim] {shadow.description}\n"
        f"[dim]Skills:[/dim]      {', '.join(shadow.skill_ids) or 'none'}\n"
        f"[dim]Tools:[/dim]       {', '.join(shadow.available_tool_ids) or 'none'}\n"
        f"[dim]Activities:[/dim]  {', '.join(shadow.assigned_activity_ids) or 'none'}\n\n"
        f"[bold]System Prompt (preview):[/bold]\n"
        f"{shadow.system_prompt[:400]}{'…' if len(shadow.system_prompt) > 400 else ''}",
        title="👤 Shadow Detail",
        border_style="magenta",
    ))


# ─────────────────────────────────────────────────────────────────────────────
#  tools group
# ─────────────────────────────────────────────────────────────────────────────

@click.group("tools")
def tools_group():
    """Manage the Tool registry."""


@tools_group.command("list")
@click.option("--status", "-s", default=None, help="Filter by status (proposed/forged/deployed).")
@click.pass_context
def tools_list(ctx, status: Optional[str]):
    """List all Tools in the vault."""
    _, vault = _get_vault_and_config(ctx)
    from systemu.core.models import ToolStatus
    filter_status = ToolStatus(status) if status else None
    tools = vault.list_tools(status=filter_status)

    if not tools:
        console.print("[dim]No tools found.[/dim]")
        return

    # F21: the vault status ("deployed") describes the RECORD; it says nothing
    # about whether the tool can run on this machine. A tool whose optional
    # dependency group is not installed used to render "deployed" here and then
    # fail at call time — a capability that looks present and silently does
    # nothing. The Status column now reports the effective state, and the
    # remedy is printed in the row rather than being something the operator has
    # to go and find.
    # DEFECT CAUGHT BY THE F21 FENCE, worth naming: the remedy contains
    # `systemu[browser]`, and Rich parses `[browser]` as a style tag and DELETES
    # it. The operator was shown "pip install systemu" — a command that runs
    # cleanly and installs nothing. Every remedy string that reaches a Rich
    # console must be escaped; `click.echo` paths (run_find_tools) must not be.
    from rich.markup import escape as _esc

    from systemu.runtime import optional_deps as _od

    table = Table(title="🔧 Tool Registry", show_lines=True)
    table.add_column("ID",     style="cyan",   no_wrap=True)
    table.add_column("Name",   style="bold")
    table.add_column("Type",   style="dim")
    table.add_column("Status", style="yellow")
    table.add_column("Description")

    unavailable = 0
    remedies: list = []
    for t in tools:
        desc = (t.get("description", "") or "")
        desc = (desc[:60] + "…") if len(desc) > 60 else (desc or "—")
        reason = _od.unavailable_reason(t.get("dependencies") or [])
        if reason:
            unavailable += 1
            cmd = _od.install_command(t.get("dependencies") or [])
            if cmd and cmd not in remedies:
                remedies.append(cmd)
            status_cell = f"[red]UNAVAILABLE[/red]\n[dim]{t['status']}[/dim]"
            desc = f"{_esc(desc)}\n[yellow]{_esc(reason)}[/yellow]"
        else:
            status_cell = t["status"]
        table.add_row(t["id"], t["name"], t.get("tool_type", "—"),
                      status_cell, desc)
    console.print(table)
    if unavailable:
        # F24: the remedy is repeated OUTSIDE the table. Inside it, Rich sizes
        # the Description column to the terminal and ELLIPSISES the overflow —
        # at 80 columns the operator was shown `pip install "systemu[brow…`.
        # The footer's own sentence ("listed above with the exact install
        # command") was therefore false on a default-width terminal, which is
        # the DEC-34 defect of asserting something the code does not do. A
        # bare `click.echo` line has no column to be truncated by.
        console.print(
            f"[yellow]▲ {unavailable} tool(s) are UNAVAILABLE — an optional "
            f"dependency group is not installed. Run `systemu doctor` for the "
            f"whole picture.[/yellow]"
        )
        for cmd in remedies:
            click.echo(f"  {cmd}")


# ─────────────────────────────────────────────────────────────────────────────
#  skills group
# ─────────────────────────────────────────────────────────────────────────────

@click.group("skills")
def skills_group():
    """Manage the Skills registry (Agent Skills Standard)."""


@skills_group.command("deprecate")
@click.argument("skill_id")
@click.option(
    "--reason",
    required=True,
    type=click.Choice(["gui_codification", "outdated", "broken"]),
    help="Why this skill is being deprecated.",
)
@click.option(
    "--reactivate",
    is_flag=True,
    help="Reset effectiveness_score to 1.0 instead of 0.0.",
)
@click.pass_context
def skills_deprecate(ctx, skill_id, reason, reactivate):
    """v0.6.5-e: deprecate (effectiveness_score=0.0) or reactivate a skill.

    Deprecated skills are excluded from shadow_decision matching when
    effectiveness_score < 0.5.  Use this command when the v0.6.0-d.5 startup
    deprecation sweep hasn't gated a known-bad skill (e.g., weather_report_creation).
    """
    # v0.9 Phase-5 3b: one mechanism — both the CLI and the Skills-page buttons
    # call skill_lifecycle.deprecate_skill (score flip + history append +
    # save_skill + audit jsonl). Keeps the CLI's get_skill error contract.
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    _, vault = _get_vault_and_config(ctx)
    try:
        result = deprecate_skill(
            skill_id, reason=reason, reactivate=reactivate, vault=vault,
        )
    except Exception as exc:
        console.print(f"[red]× skill {skill_id} not found: {exc}[/red]")
        ctx.exit(1)
        return

    icon = "▲" if reactivate else "▼"
    console.print(
        f"[green]{icon} {result['action'].title()}d {skill_id} "
        f"({result['name'] or '?'}) — "
        f"effectiveness_score={result['effectiveness_score']}[/green]"
    )


@skills_group.command("export")
@click.argument("skill_id")
@click.option(
    "--output", "-o", required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory where the spec-conformant skill bundle is written.",
)
@click.pass_context
def skills_export(ctx, skill_id: str, output: Path) -> None:
    """v0.7-d: Export a Systemu Skill as a portable Anthropic Agent Skill bundle.

    Writes ``<output>/<kebab-name>/SKILL.md`` with spec-conformant YAML
    frontmatter (top-level ``name`` + ``description``; everything else under
    ``metadata:``).  Bundles are validatable by the upstream ``skills-ref``
    CLI and copyable into ``anthropics/skills`` as a community contribution.
    """
    from systemu.pipelines.skill_exporter import export_skill

    _, vault = _get_vault_and_config(ctx)
    try:
        out = export_skill(skill_id=skill_id, target_dir=output, vault=vault)
    except KeyError:
        click.echo(f"ERROR: skill {skill_id!r} not found in vault", err=True)
        ctx.exit(3)
        return
    except FileExistsError as e:
        click.echo(f"ERROR: {e}", err=True)
        ctx.exit(4)
        return

    click.echo(f"Exported {skill_id} -> {out}")


@skills_group.command("list")
@click.option("--category", "-c", default=None, help="Filter by category.")
@click.pass_context
def skills_list(ctx, category: Optional[str]):
    """List all Skills in the vault."""
    _, vault = _get_vault_and_config(ctx)
    skills = vault.list_skills()

    if category:
        skills = [s for s in skills if s.get("category", "").lower() == category.lower()]

    if not skills:
        console.print("[dim]No skills found.[/dim]")
        return

    table = Table(title="🧠 Skills Registry", show_lines=True)
    table.add_column("ID",       style="cyan",  no_wrap=True)
    table.add_column("Name",     style="bold")
    table.add_column("Category", style="dim")
    table.add_column("Evidence", justify="right")
    table.add_column("Description")

    for s in skills:
        table.add_row(
            s["id"], s["name"], s.get("category", "—"),
            str(len(s.get("evidence_scroll_ids", []))),
            (s.get("description", "") or "")[:60] + "…"
                if len(s.get("description", "")) > 60
                else s.get("description", "—"),
        )
    console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
#  settings command
# ─────────────────────────────────────────────────────────────────────────────

def _no_provider_message(config, *, probe=None) -> str:
    """The headless refusal, naming EVERY provider the operator could configure.

    F19: this said "No OPENROUTER_API_KEY configured", which hid four other ways
    to proceed — including one that needs no credential at all. Generated from
    ``PROVIDER_SPECS`` via ``provider_status.configure_hint``, so a sixth
    provider appears here with nobody editing a sentence. ASCII (DEC-32c).
    """
    from systemu.runtime import provider_status as _ps
    statuses = _ps.all_provider_statuses(
        config, probe=probe, cache_ttl_s=_ps.PROBE_CACHE_TTL_S)
    return ("No LLM provider is usable, so the daemon would boot dead. "
            + _ps.configure_hint(statuses)
            + " Then run `systemu setup`, or start the daemon again.")


#: Presentation only, keyed by STATE — never by provider. Mirrors the dashboard
#: Settings card (``interface/pages/settings.py``) so the two surfaces cannot
#: describe the same minted state in different words.
_PROVIDER_MARK = {
    "set":         ("[green]OK[/green]",  "Set"),
    "missing":     ("[red]--[/red]",      "Not set"),
    "reachable":   ("[green]OK[/green]",  "Reachable"),
    "unreachable": ("[red]--[/red]",      "Not reachable"),
    "unknown":     ("[dim]?[/dim]",       "Unknown"),
}


def _render_settings_panel(config, *, statuses) -> None:
    """Paint the read-only settings panel from ALREADY-MINTED verdicts.

    F19 — WHY THE VERDICTS ARRIVE AS AN ARGUMENT. This panel used to decide the
    one row it showed with ``config.openrouter_api_key``, so on the same machine
    and the same minute the dashboard reported five providers (with Ollama
    probed and REACHABLE) and this reported "OpenRouter / API key set: No". Two
    operator-facing surfaces, one fact, two answers — DEC-43. The minting is the
    caller's job so this function stays pure and the probe stays visible at the
    call site, where its cost is paid.
    """
    from systemu.runtime import provider_status as _ps

    rows = []
    for spec in _ps.PROVIDER_SPECS:
        st = statuses.get(spec.provider)
        if st is None:
            continue
        mark, word = _PROVIDER_MARK.get(st.state, ("[dim]?[/dim]", "Unknown"))
        rows.append(f"  {st.display:<11} {mark} {word:<14} [dim]{st.detail}[/dim]")

    console.print(Panel(
        f"[bold]LLM Tiers[/bold]\n"
        f"  Tier 1 (deep reasoning):    [cyan]{config.tier1_model}[/cyan]\n"
        f"  Tier 2 (structured/code):   [cyan]{config.tier2_model}[/cyan]\n"
        f"  Tier 3 (fast/formatting):   [cyan]{config.tier3_model}[/cyan]\n\n"
        f"[bold]Behaviour[/bold]\n"
        f"  Non-interactive mode:       [yellow]{config.non_interactive}[/yellow]\n"
        f"  Vault directory:            [dim]{config.vault_dir}[/dim]\n\n"
        f"[bold]Providers[/bold]  [dim](credentials live in .env and are never "
        f"printed; Ollama takes no key, so its row reports whether it actually "
        f"ANSWERS)[/dim]\n"
        + "\n".join(rows),
        title="⚙️  Systemu Settings",
        border_style="blue",
    ))


def _settings_show_body(ctx):
    """Render the read-only settings panel. Shared by `settings show` and the
    bare-`settings` back-compat fallback (Phase 2 Task 7).

    The keyless provider's liveness witness is spent HERE, synchronously, and it
    is bounded: ``provider_status.DEFAULT_PROBE_TIMEOUT`` per address. Measured
    on the shipped default ``http://localhost:11434``: ~1.1 s with Ollama
    running, ~2.0 s with nothing listening (dual-stack, ``::1`` dropped then
    IPv4). ``settings show`` is an inspection command whose whole job is to
    report this, so the wait buys the answer; the 20 s memo means a second
    command in the same process is free. A command that CANNOT afford it passes
    ``probe=provider_status.unprobed`` and gets an honest "not probed".
    """
    from systemu.runtime import provider_status as _ps
    config, vault = _get_vault_and_config(ctx)
    _render_settings_panel(config, statuses=_ps.all_provider_statuses(
        config, cache_ttl_s=_ps.PROBE_CACHE_TTL_S))


@click.group("settings", invoke_without_command=True)
@click.pass_context
def settings_cmd(ctx):
    """Show or set Systemu configuration (models, vault dir, etc.).

    Bare ``settings`` shows the read-only view (back-compat); ``settings set``
    writes an allow-listed setting via the shared command layer (Phase 2)."""
    if ctx.invoked_subcommand is None:
        _settings_show_body(ctx)


@settings_cmd.command("show")
@click.pass_context
def settings_show_cmd(ctx):
    """Show current Systemu configuration (models, vault dir, etc.)."""
    _settings_show_body(ctx)


@settings_cmd.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--json", "as_json", is_flag=True, help="Emit the structured result as JSON.")
@click.pass_context
def settings_set_cmd(ctx, key: str, value: str, as_json: bool):
    """Set an allow-listed setting (canonical CLI-owned write -- Phase 2)."""
    from systemu.interface.command import verbs as _verbs
    _, vault = _get_vault_and_config(ctx)
    result = _verbs.settings_set(key, value, vault=vault)
    if as_json:
        click.echo(result.to_json())
    else:
        console.print(result.to_rich())
    ctx.exit(result.exit_code)


# ─────────────────────────────────────────────────────────────────────────────
#  Phase S2 — tools forge
# ─────────────────────────────────────────────────────────────────────────────

@tools_group.command("forge")
@click.option("--name", "-n", required=True, help="Tool name to forge (snake_case).")
@click.option("--context", "-c", default="", help="Context hint describing what the tool should do.")
@click.pass_context
def tools_forge(ctx, name: str, context: str):
    """Forge (generate code for) a tool by name.

    If the tool already exists as PROPOSED, generates its implementation.
    If it doesn't exist, first designs the specification, then generates code.
    """
    def _work():
        config, vault = _get_vault_and_config(ctx)
        from systemu.pipelines.tool_forge import forge_tool_by_name

        console.print(f"\n[cyan]🔧 Forging tool:[/cyan] {name}\n")
        result = forge_tool_by_name(name, config, vault, context_hint=context)
        if result:
            console.print(f"[green]✓ Tool '{result.name}' forged successfully (status: {result.status})[/green]")
        else:
            console.print("[yellow]Forge skipped or failed.[/yellow]")
    _handle_pending_decision_or_run(ctx, _work)


# ─────────────────────────────────────────────────────────────────────────────
#  tools dry-run — manual single-tool dry-run advance (v0.7.4 Pattern 2)
# ─────────────────────────────────────────────────────────────────────────────

@tools_group.command("dry-run")
@click.argument("tool_id")
@click.pass_context
def tools_dryrun(ctx, tool_id: str):
    """Run dry-run validation on a single tool (v0.7.4 Pattern 2).

    Equivalent to the dashboard `/tools` page [Dry-Run] action on a single row.
    On pass, the tool advances to DEPLOYED. On fail, the tool stays at FORGED
    with dry_run_status='failed' and a WARNING event is published.
    """
    config, vault = _get_vault_and_config(ctx)
    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        console.print(f"[red]Tool '{tool_id}' not found in vault.[/red]")
        ctx.exit(1)
        return

    if not getattr(tool, "implementation_path", None):
        console.print(
            f"[yellow]Tool '{tool.name}' has no implementation_path yet — "
            "skipping (forge incomplete).[/yellow]"
        )
        ctx.exit(2)
        return

    from systemu.pipelines.tool_dry_run import dry_run_tool
    from systemu.core.models import ToolStatus
    from systemu.interface.notifications import log_event

    result = dry_run_tool(tool, vault=vault, config=config)
    tool.dry_run_status = result.status

    if result.status == "passed":
        tool.status = ToolStatus.DEPLOYED
        vault.save_tool(tool)
        console.print(
            f"[green]✓ Tool '{tool.name}' dry-run passed ({result.elapsed_ms}ms) "
            f"— status advanced to DEPLOYED.[/green]"
        )
    elif result.status == "skipped":
        vault.save_tool(tool)
        console.print(
            f"[yellow]Tool '{tool.name}' dry-run skipped: {result.skip_reason}[/yellow]"
        )
    else:
        vault.save_tool(tool)
        log_event(
            "WARNING", "tool",
            f"Tool '{tool.name}' failed dry-run validation: {(result.error or '')[:200]}",
            {"tool_id": tool.id, "tool_name": tool.name, "error": result.error},
        )
        console.print(
            f"[red]✗ Tool '{tool.name}' dry-run failed: {result.error}[/red]"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  tools enable — Gate-3 enable via the shared command layer (Phase 2 Task 3)
# ─────────────────────────────────────────────────────────────────────────────

@tools_group.command("enable")
@click.argument("tool_id")
@click.option("--json", "as_json", is_flag=True, help="Emit the structured result as JSON.")
@click.pass_context
def tools_enable_cmd(ctx, tool_id: str, as_json: bool):
    """Gate-3 enable a tool (canonical CLI-owned write -- Phase 2)."""
    from systemu.interface.command import dispatch as _dispatch
    _, vault = _get_vault_and_config(ctx)
    result = _dispatch.dispatch("tools enable", [tool_id], vault=vault)
    if as_json:
        click.echo(result.to_json())
    else:
        console.print(result.to_rich())
    ctx.exit(result.exit_code)


# ─────────────────────────────────────────────────────────────────────────────
#  tools show / recalibrate — shared-layer verbs (Phase 2 Task 7)
#  Direct verb calls (not dispatch-routed) per the Task 7 plan; a uniform
#  dispatch-routing decision is deferred to a later coherence pass.
# ─────────────────────────────────────────────────────────────────────────────

@tools_group.command("show")
@click.argument("tool_id")
@click.option("--json", "as_json", is_flag=True, help="Emit the structured result as JSON.")
@click.pass_context
def tools_show_cmd(ctx, tool_id: str, as_json: bool):
    """Show a single tool's detail (read-only, via the shared view-model)."""
    from systemu.interface.command import verbs as _verbs
    _, vault = _get_vault_and_config(ctx)
    result = _verbs.tools_show(tool_id, vault=vault)
    if as_json:
        click.echo(result.to_json())
    else:
        console.print(result.to_rich())
    ctx.exit(result.exit_code)


@tools_group.command("recalibrate")
@click.argument("tool_id")
@click.option("--reason", "-r", required=True, help="Why this tool is being recalibrated.")
@click.option("--json", "as_json", is_flag=True, help="Emit the structured result as JSON.")
@click.pass_context
def tools_recalibrate_cmd(ctx, tool_id: str, reason: str, as_json: bool):
    """Bump a tool's version + record a recalibration entry (canonical write)."""
    from systemu.interface.command import verbs as _verbs
    _, vault = _get_vault_and_config(ctx)
    result = _verbs.tools_recalibrate(tool_id, reason=reason, vault=vault)
    if as_json:
        click.echo(result.to_json())
    else:
        console.print(result.to_rich())
    ctx.exit(result.exit_code)


# ─────────────────────────────────────────────────────────────────────────────
#  tools deps subgroup — operator-managed allow-list for tool pip dependencies
# ─────────────────────────────────────────────────────────────────────────────
#  Surfaces the v0.3.3 dependency installer to operators.  See:
#    * systemu/runtime/dependency_installer.py
#    * systemu/runtime/dep_approvals.py
#
#  Workflow:
#    1. A shadow tries a tool whose manifest declares `python-docx`.
#    2. Install mode is PROMPT (local default) → registry refuses to install
#       until approved, records it as pending, returns
#       error_type=dependency_install_pending_approval.
#    3. `sharing_on tools deps list` shows it in the Pending section.
#    4. Operator: `sharing_on tools deps approve python-docx`
#    5. Next shadow run: registry self-heals, installs, retries import.

@tools_group.group("deps")
def tools_deps_group():
    """Operator allow-list for tool pip dependencies."""


def _deps_store():
    """Resolve the default approval store rooted at ``data/``.

    Kept as a function (not a module-level singleton) so each CLI
    invocation reads the latest file from disk.
    """
    from pathlib import Path as _P
    from systemu.runtime.dep_approvals import DepApprovalStore
    return DepApprovalStore(_P("data") / "dep_approvals.json")


@tools_deps_group.command("list")
@click.option("--show-pending/--no-show-pending", default=True,
              help="Include pending (unapproved) packages.")
def tools_deps_list(show_pending: bool):
    """List approved and pending tool dependencies."""
    store = _deps_store()
    approved = store.list_approved()
    pending  = store.list_pending() if show_pending else []

    if approved:
        table = Table(title="✅ Approved tool dependencies", show_lines=False)
        table.add_column("Package",      style="bold green")
        table.add_column("Approved at",  style="dim")
        table.add_column("Approved by",  style="dim")
        table.add_column("First tool",   style="cyan")
        for entry in approved:
            table.add_row(
                entry["package"],
                entry.get("approved_at",  "—"),
                entry.get("approved_by",  "—"),
                entry.get("first_seen_tool") or "—",
            )
        console.print(table)
    else:
        console.print("[dim]No approved dependencies.[/dim]")

    if not show_pending:
        return
    if pending:
        table = Table(title="⏳ Pending approval", show_lines=False)
        table.add_column("Package",       style="bold yellow")
        table.add_column("First seen",    style="dim")
        table.add_column("First tool",    style="cyan")
        table.add_column("Request count", style="magenta", justify="right")
        for entry in pending:
            table.add_row(
                entry["package"],
                entry.get("first_seen_at", "—"),
                entry.get("first_seen_tool") or "—",
                str(entry.get("request_count", 0)),
            )
        console.print(table)
        console.print(
            "\n[dim]Approve with:[/dim] systemu tools deps approve <package>"
        )
    else:
        console.print("[dim]No pending dependencies.[/dim]")


@tools_deps_group.command("approve")
@click.argument("package")
@click.option("--tool-id", default=None, help="Originating tool id (for audit).")
@click.option("--by",      default="operator", help="Approver label recorded in audit.")
def tools_deps_approve(package: str, tool_id: Optional[str], by: str):
    """Approve a pip package so the registry may auto-install it.

    The approval is persisted to ``data/dep_approvals.json``.  After this
    command returns, the next ToolRegistry self-heal call that encounters
    this package will install it.  Already-running shadows do not
    retroactively benefit -- restart the daemon or re-trigger the activity.
    """
    store = _deps_store()
    newly = store.approve(package, approved_by=by, tool_id=tool_id)
    if newly:
        console.print(f"[green]✓ Approved '{package}' (by {by})[/green]")
    else:
        console.print(f"[yellow]'{package}' was already approved — no change.[/yellow]")


@tools_deps_group.command("revoke")
@click.argument("package")
def tools_deps_revoke(package: str):
    """Remove a pip package from the allow-list.

    Does not uninstall the package -- that's a separate decision.  In-process
    caches in already-running daemons / workers retain "satisfied" state
    until restart; the revoke takes effect for any newly-started process.
    """
    store = _deps_store()
    if store.revoke(package):
        console.print(f"[green]✓ Revoked '{package}'[/green]")
    else:
        console.print(f"[yellow]'{package}' was not approved — nothing to revoke.[/yellow]")


@tools_deps_group.command("doctor")
@click.pass_context
def tools_deps_doctor(ctx):
    """Scan all enabled tools for cross-tool dependency conflicts.

    Useful for CI / deploy verification.  Exits non-zero when conflicts
    are found so it can be wired into a pre-deploy check.
    """
    _, vault = _get_vault_and_config(ctx)
    from systemu.runtime.dep_conflicts import find_conflicts

    tools = vault.load_index("tools") or []
    enabled = [t for t in tools if t.get("enabled")]
    if not enabled:
        console.print("[dim]No enabled tools found.[/dim]")
        return
    conflicts = find_conflicts(enabled)
    if not conflicts:
        console.print(
            f"[green]✓ {len(enabled)} enabled tool(s) scanned — no dependency "
            f"conflicts.[/green]"
        )
        return
    console.print(
        f"[red]✗ Found {len(conflicts)} dependency conflict"
        f"{'s' if len(conflicts) != 1 else ''} across {len(enabled)} enabled tool(s):[/red]"
    )
    for c in conflicts:
        console.print(f"\n[bold red]{c.package}[/bold red]")
        for s in c.specs:
            console.print(f"  • {s.tool_name} ({s.tool_id or '—'}): {s.spec or '(any version)'}")
        console.print(f"  [yellow]→ {c.reason}[/yellow]")
    import sys as _sys
    _sys.exit(1)


@tools_deps_group.command("sync")
@click.option("--dry-run", is_flag=True, help="Show what would be installed without doing it.")
@click.pass_context
def tools_deps_sync(ctx, dry_run: bool):
    """Install every approved dep into the current Python (pre-warm).

    Useful at deploy time to avoid the first-call latency hit.  Honours
    the resolved InstallMode -- when mode=OFF nothing happens; when
    mode=PROMPT only approved deps are processed (which is all this
    command is for); when mode=ALWAYS this command is effectively a
    speedup over lazy installs.
    """
    config, _ = _get_vault_and_config(ctx)
    from systemu.runtime.dependency_installer import (
        InstallMode,
        InstallStatus,
        ensure_satisfied,
        resolve_install_mode,
    )
    store = _deps_store()
    approved = [e["package"] for e in store.list_approved()]
    if not approved:
        console.print("[dim]No approved dependencies to sync.[/dim]")
        return

    mode = resolve_install_mode(
        config_mode=getattr(config, "tool_dep_install_mode", None),
        systemu_mode=getattr(config, "systemu_mode", None),
    )
    if mode is InstallMode.OFF:
        console.print(
            "[yellow]Install mode is OFF — refusing to sync. "
            "Set SYSTEMU_TOOL_DEP_INSTALL_MODE=always or =prompt to enable.[/yellow]"
        )
        return

    console.print(f"[cyan]Syncing {len(approved)} approved deps (mode={mode.value})…[/cyan]")
    if dry_run:
        for p in approved:
            console.print(f"  • would install: {p}")
        return
    result = ensure_satisfied(
        approved,
        mode=mode,
        approvals=store,
        tool_name="<cli:tools deps sync>",
    )
    if result.ok:
        if result.installed_now:
            console.print(f"[green]✓ Installed: {', '.join(result.installed_now)}[/green]")
        else:
            console.print("[green]✓ All approved deps already satisfied.[/green]")
    else:
        console.print(f"[red]✗ Sync failed ({result.status.value}): {result.error}[/red]")
        if result.pip_stderr_tail:
            console.print(f"[dim]pip stderr tail:[/dim]\n{result.pip_stderr_tail}")


# ─────────────────────────────────────────────────────────────────────────────
#  Phase S2 — army awaken + execute
# ─────────────────────────────────────────────────────────────────────────────

@army_group.command("awaken")
@click.option("--name", "-n", required=True, help="Name for the new Shadow.")
@click.option("--activity", "-a", default=None, help="Activity ID to assign immediately.")
@click.option("--creativity",      type=int, default=50, show_default=True, help="Creativity level 0-100.")
@click.option("--professionalism", type=int, default=50, show_default=True, help="Professionalism level 0-100.")
@click.option("--techie",          type=int, default=50, show_default=True, help="Techie depth 0-100.")
@click.option("--thinking",        type=int, default=50, show_default=True, help="Thinking depth 0-100.")
@click.pass_context
def army_awaken(ctx, name: str, activity: Optional[str],
                creativity: int, professionalism: int, techie: int, thinking: int):
    """Manually create (awaken) a new Shadow persona.

    If --activity is provided, the shadow is assigned to that activity.
    Persona dimension sliders (0-100) adjust the shadow's system prompt tone.
    """
    import os as _os
    # Inject persona dimensions as env vars so shadow_decision.create_shadow can read them
    _os.environ["SYSTEMU_PERSONA_CREATIVITY"]      = str(creativity)
    _os.environ["SYSTEMU_PERSONA_PROFESSIONALISM"] = str(professionalism)
    _os.environ["SYSTEMU_PERSONA_TECHIE"]          = str(techie)
    _os.environ["SYSTEMU_PERSONA_THINKING"]        = str(thinking)

    persona_dims = {
        "creativity":      creativity,
        "professionalism": professionalism,
        "techie":          techie,
        "thinking":        thinking,
    }
    console.print(f"[dim]Persona dimensions: Creativity={creativity} | Professionalism={professionalism} | Techie={techie} | Thinking={thinking}[/dim]")

    config, vault = _get_vault_and_config(ctx)
    from systemu.pipelines.shadow_decision import create_shadow
    from systemu.core.models import Activity, ActivityStatus

    if activity:
        try:
            act = vault.get_activity(activity)
        except KeyError:
            console.print(f"[red]Activity not found: {activity}[/red]")
            sys.exit(1)
        if act.status == ActivityStatus.PARTIAL:
            console.print(f"[red]Activity '{act.name}' is PARTIAL — required tools aren't deployed yet.[/red]")
            missing = ", ".join(act.missing_tools) if act.missing_tools else "(check vault)"
            console.print(f"[dim]Missing tools: {missing}[/dim]")
            console.print("Forge and enable the missing tools first. The system will auto-assign a shadow once all tools are ready.")
            sys.exit(1)
        shadow = create_shadow(act, name, config, vault, persona_dimensions=persona_dims)
    else:
        from systemu.core.models import Shadow, ShadowStatus
        from systemu.core.utils import generate_id
        stub_act = Activity(
            id=generate_id("activity"), name=f"Manual: {name}",
            scroll_id="stub", status=ActivityStatus.UNASSIGNED,
        )
        shadow = create_shadow(stub_act, name, config, vault, persona_dimensions=persona_dims)

    console.print(f"\n[green]✓ Shadow '[bold]{shadow.name}[/bold]' awakened ({shadow.id})[/green]")


@army_group.command("execute")
@click.argument("shadow_id")
@click.argument("scroll_id")
@click.option("--dry-run", is_flag=True, help="Show execution plan without invoking real tools.")
@click.option("--origin", default="manual", show_default=True,
              help="v0.8.16: trigger origin stamped on every event "
                   "(manual=operator Execute button, scheduled=schedule fire).")
@click.pass_context
def army_execute(ctx, shadow_id: str, scroll_id: str, dry_run: bool, origin: str):
    """Execute a Scroll via a Shadow (agentic runtime).

    Uses the ShadowRuntime ReAct loop: Reason -> Tool Call -> Observe -> repeat.
    Requires the Shadow to have at least one DEPLOYED tool. Use --dry-run to
    preview the execution plan without invoking real tools (all PROPOSED tools allowed).

    v0.8.16: ``--origin`` tags every published event so the dashboard panes
    partition correctly.  The scheduled-execute job passes ``scheduled``; the
    operator Execute button uses the ``manual`` default.
    """
    _maybe_install_bridge_writer()   # v0.8.6
    config, vault = _get_vault_and_config(ctx)

    try:
        shadow = vault.get_shadow(shadow_id)
        scroll = vault.get_scroll(scroll_id)
    except KeyError as exc:
        console.print(f"[red]Not found: {exc}[/red]")
        sys.exit(1)

    # Build a minimal Activity if the scroll isn't linked to one
    from systemu.core.models import Activity, ActivityStatus
    from systemu.core.utils import generate_id as _gid
    activity: Activity | None = None
    if scroll.activity_id:
        try:
            activity = vault.get_activity(scroll.activity_id)
        except KeyError:
            pass
    if activity is None:
        activity = Activity(
            id=_gid("activity"),
            name=scroll.name,
            scroll_id=scroll.id,
            required_tool_ids=shadow.available_tool_ids,
            required_skill_ids=shadow.skill_ids,
            status=ActivityStatus.ASSIGNED,
            assigned_shadow_id=shadow.id,
        )

    console.print(Panel(
        f"[bold]Shadow:[/bold]  {shadow.name} ({shadow.id})\n"
        f"[bold]Scroll:[/bold]  {scroll.name} ({scroll.id})\n"
        f"[bold]Steps:[/bold]   {len(scroll.action_blocks)} action blocks\n\n"
        f"{'[yellow]⚠️  DRY RUN — no tools will be executed[/yellow]' if dry_run else '[cyan]⚡ LIVE — agentic execution starting[/cyan]'}",
        title="👤 ShadowRuntime",
        border_style="magenta",
    ))

    from systemu.runtime.shadow_runtime import ShadowRuntime
    runtime = ShadowRuntime(config=config, vault=vault)

    # offload-lint: ok — a terminal CLI command (@click army_execute), not a
    # dashboard handler: there is no running event loop here, and this call is
    # what creates one.
    result = asyncio.run(runtime.execute(shadow, activity, dry_run=dry_run, origin=origin))

    status  = result.get("status", "?")
    summary = result.get("summary", "")
    error   = result.get("error")

    status_colour = {"success": "green", "failure": "red", "partial": "yellow"}.get(status, "white")
    console.print(Panel(
        f"[bold]Status:[/bold]      [{status_colour}]{status.upper()}[/{status_colour}]\n"
        f"[bold]Summary:[/bold]     {summary}\n"
        f"[bold]Snapshots:[/bold]   {result.get('snapshots_taken', 0)}\n"
        f"[bold]Events:[/bold]      {result.get('total_events', 0)}\n"
        + (f"\n[red]Error:[/red] {error}" if error else ""),
        title="📋 Execution Result",
        border_style=status_colour,
    ))



# ─────────────────────────────────────────────────────────────────────────────
#  Phase S2 — evolve
# ─────────────────────────────────────────────────────────────────────────────

@click.group("evolve")
def evolve_group():
    """Run the Evolution Engine or view pending evolution proposals."""


@evolve_group.command("run")
@click.pass_context
def evolve_run(ctx):
    """Run the Evolution Engine now (don't wait for daily schedule)."""
    config, vault = _get_vault_and_config(ctx)
    from systemu.pipelines.evolution_engine import run_evolution_check

    console.print("\n[cyan]🧬 Running Evolution Engine ...[/cyan]\n")
    proposals = run_evolution_check(config, vault)
    console.print(f"\n[green]✓ Evolution check complete — {len(proposals)} proposals.[/green]")


@evolve_group.command("show-pending")
@click.pass_context
def evolve_show_pending(ctx):
    """Show all pending (unresolved) evolution proposals."""
    _, vault = _get_vault_and_config(ctx)
    evolutions = vault.list_evolutions()
    from systemu.core.models import EvolutionStatus
    pending = [e for e in evolutions if e.get("status") == EvolutionStatus.PROPOSED.value]

    if not pending:
        console.print("[dim]No pending evolution proposals.[/dim]")
        return

    table = Table(title="🧬 Pending Evolutions", show_lines=True)
    table.add_column("ID",         style="cyan",  no_wrap=True)
    table.add_column("Type",       style="yellow")
    table.add_column("Target",     style="dim")
    table.add_column("Description")

    for e in pending:
        table.add_row(
            e["id"], e["evolution_type"],
            e.get("target_entity_type", "—"),
            (e.get("description", "") or "")[:70] + "…"
                if len(e.get("description", "")) > 70
                else e.get("description", "—"),
        )
    console.print(table)


@evolve_group.command("apply")
@click.argument("evolution_id")
@click.pass_context
def evolve_apply(ctx, evolution_id: str):
    """Apply an approved evolution to its target entities."""
    config, vault = _get_vault_and_config(ctx)
    from systemu.pipelines.evolution_engine import apply_evolution

    ok = apply_evolution(evolution_id, config, vault)
    if ok:
        console.print(f"[green]✓ Evolution {evolution_id} applied.[/green]")
    else:
        console.print(f"[red]Failed to apply evolution {evolution_id}.[/red]")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  Phase S2 — chat
# ─────────────────────────────────────────────────────────────────────────────

@click.group("chat")
def chat_group():
    """Direct chat tasks -- skip the capture/record flow."""


@chat_group.command("submit")
@click.argument("prompt")
@click.pass_context
def chat_submit(ctx, prompt: str):
    """Run a free-text task through the full pipeline.

    Examples:

      systemu chat submit "take a screenshot of example.com and save to ~/Desktop/"

      systemu chat submit "/continue also add a timestamp watermark"
    """
    config, vault = _get_vault_and_config(ctx)
    from systemu.pipelines.direct_task import run_direct_task

    console.print(f"\n[cyan]💬 Running chat task ...[/cyan]")
    console.print(f"[dim]Prompt: {prompt[:100]}{'…' if len(prompt) > 100 else ''}[/dim]\n")

    activity = run_direct_task(prompt, config, vault)

    if activity is None:
        console.print("[red]✗ Task failed — see logs for details.[/red]")
        sys.exit(1)

    # Load latest chat history entry for this activity
    history = vault.load_chat_history(limit=10)
    scroll_id = getattr(activity, "scroll_id", None)
    entry = next(
        (e for e in reversed(history) if scroll_id and e.get("scroll_id") == scroll_id),
        history[-1] if history else {},
    )
    status  = entry.get("status", "unknown")
    exec_id = entry.get("execution_id", "—")

    status_colour = {"success": "green", "partial": "yellow", "failed": "red"}.get(
        status, "white"
    )
    console.print(Panel(
        f"[bold]Activity:[/bold]  {activity.name}\n"
        f"[bold]Status:[/bold]    [{status_colour}]{status.upper()}[/{status_colour}]\n"
        f"[bold]Shadow:[/bold]    {entry.get('shadow_id', '—')}\n"
        f"[bold]Execution:[/bold] {exec_id}",
        title="💬 Chat Task Result",
        border_style=status_colour,
    ))


@chat_group.command("history")
@click.option("--limit", "-n", default=20, show_default=True)
@click.pass_context
def chat_history(ctx, limit: int):
    """Show recent chat task history."""
    _, vault = _get_vault_and_config(ctx)
    entries = vault.load_chat_history(limit=limit)

    if not entries:
        console.print("[dim]No chat history yet.[/dim]")
        return

    table = Table(title="💬 Chat History", show_lines=True)
    table.add_column("Time",    style="dim",    no_wrap=True)
    table.add_column("Prompt",  style="bold",   max_width=60)
    table.add_column("Status",  style="yellow")
    table.add_column("Shadow",  style="cyan",   no_wrap=True)

    for e in reversed(entries):
        ts    = e.get("ts", "")[:19].replace("T", " ")
        ptext = e.get("prompt", "")[:58] + ("…" if len(e.get("prompt", "")) > 58 else "")
        table.add_row(ts, ptext, e.get("status", "?"), e.get("shadow_id", "—")[:14] or "—")
    console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
#  Phase S2 — daemon
# ─────────────────────────────────────────────────────────────────────────────

@click.group("daemon")
def daemon_group():
    """Control the Systemu background daemon (scheduler + web dashboard)."""


def _print_daemon_build(build_match, build_note: str) -> None:
    """F13 — disclose WHICH systemu build the daemon is executing.

    Every surface that says the daemon is up says this too. ``build_match`` is
    tri-state and UNVERIFIED (``None``) is rendered as its own state, never as
    agreement: a daemon that recorded no build is an older/other build, which is
    the skew itself. A mismatch is LOUD but never fatal — a user mid-upgrade
    must still be able to reach ``daemon stop``.

    D2 — IT GOES OUT UNWRAPPED, ONE LINE PER PATH. This used ``console.print``,
    and Rich folds a paragraph at the console width wherever the break lands:
    at 80 columns the witnessed line ``same build on both sides: systemu
    0.10.29 from C:\\...`` arrived in four fragments. ``_print_daemon_where``
    a few lines below already carries the ruling this broke — *a path wrapped
    at column 80 is not a path*, because it can be neither pasted nor searched
    for — and the build line is the one that matters MID-UPGRADE, where the
    whole point of naming two installations is that the operator can go and
    look at them. ``click.secho`` keeps the colour and does not wrap.

    The marker is ASCII (DEC-32c): a cp1252 console cannot encode a warning
    glyph, and on a line that carries a verdict the failure would be a
    UnicodeEncodeError in place of the verdict. ``click.secho`` also prints the
    note LITERALLY, so a build path containing ``[`` is no longer eaten as Rich
    markup.
    """
    if not build_note:
        return
    if build_match is False:
        click.secho(f"  !! {build_note}", fg="red")
    elif build_match is True:
        click.secho(f"  {build_note}", dim=True)
    else:
        click.secho(f"  !! {build_note}", fg="yellow")


@daemon_group.command("start")
@click.option("--port", default=8765, show_default=True, help="Port for the web dashboard.")
@click.option("--foreground", is_flag=True, help="Run in foreground (blocking).")
@click.option("--wait", "wait_s", type=float, default=None,
              help="Seconds to wait for the daemon to actually accept connections "
                   "before giving up (default 60, or SYSTEMU_DAEMON_START_TIMEOUT).")
@click.pass_context
def daemon_start(ctx, port: int, foreground: bool, wait_s):
    """Start the Systemu background daemon.

    Spawning is a CLAIM, not proof. This command does not report success -- and
    does not exit 0 -- until a real connection to the dashboard port has been
    observed to succeed. The wait is BOUNDED; a daemon that never becomes ready
    is reported honestly with a nonzero exit instead of hanging.

    The dashboard ships as an optional extra. Without it nothing binds the port,
    so this command refuses at once and prints the command that installs it.
    """
    config, vault = _get_vault_and_config(ctx)
    from systemu.scheduler import daemon as _daemon_mod
    from systemu.scheduler.daemon import start_daemon

    # ── F21: the [dashboard] group gate, BEFORE anything is spawned ─────────
    # `daemon start`'s readiness witness IS the dashboard socket (DEC-41: the
    # spawn is a claim, and `await_readiness` polls for a real connection on
    # `port`). Without nicegui nothing ever binds that port, so the command
    # would spawn a daemon, poll for the full 60s timeout, and then report
    # "timed out — nothing is accepting on 127.0.0.1:8765" — true, but a whole
    # minute spent to arrive at a diagnosis that names no cause and no cure.
    #
    # Refused here instead, in under a second, with the command that fixes it.
    # This does not remove a capability: there has never been a headless daemon
    # mode (the task API registers its routes on the NiceGUI app too), so the
    # alternative was not "keep working" but "invent an unwitnessed mode",
    # which is exactly what DEC-41 forbids.
    from systemu.runtime import optional_deps as _od
    if _od.missing_groups(("nicegui",)):
        from rich.markup import escape as _esc
        _lead, _cmd, _then = _od.unavailable_reason_parts(("nicegui",))
        console.print(
            f"[red]ERROR: cannot start the daemon -- the web dashboard is "
            f"not installed.[/red]\n"
            f"  {_esc(_lead.rstrip())}"
        )
        # F29/D9: THE COMMAND GOES OUT UNWRAPPED, ON ITS OWN LINE. Rich breaks a
        # paragraph at the terminal width wherever the break lands, and at 80
        # columns this remedy came out as `Install it \n with: pip install
        # "systemu[dashboard]"`. A command split across a line break cannot be
        # copied, and the half that survives a copy (`pip`) runs and does nothing.
        # `click.echo` does not wrap -- the same reason the `roots` group is
        # line-oriented, where the thing that must not be broken is a path.
        click.echo(f"    {_cmd}")
        if _then:
            console.print(f"  {_esc(_then.strip())}")
        console.print(
            f"[dim]  `daemon start` reports success only when a real connection "
            f"to the dashboard port succeeds, so without it there is nothing to "
            f"witness. Everything else -- recording, analysis, tools, the whole "
            f"CLI -- works on the default install.[/dim]"
        )
        ctx.exit(1)           # DEC-41: not started != success

    # First-run guard: NO provider usable → run setup now (interactive TTY) or
    # point at it (headless). Booting with nothing configured only yields a dead
    # dashboard that fails every task — exactly the pip-install pitfall this
    # closes, and the refusal stays.
    #
    # F19 — WHAT CHANGED IS *WHICH* MACHINES COUNT AS "NOTHING". `key_present`
    # now consumes `provider_status` (see its docstring for why the NAME stays),
    # so a machine with only a Google/Anthropic/OpenAI key, or with Ollama
    # actually answering, BOOTS. It used to be turned away and told to go and
    # get an OpenRouter key. A machine with nothing is still refused, and is now
    # told about all five ways to fix it instead of one.
    import sys as _sys

    from sharing_on.setup_flow import key_present, run_setup
    if not key_present():
        if _sys.stdin.isatty():
            console.print("[yellow]No LLM provider is usable yet — let's set "
                          "one up before starting.[/yellow]")
            run_setup(interactive=True, print_fn=lambda s: console.print(s))
            if not key_present():
                console.print("[yellow]Still no usable provider — start "
                              "aborted. Run [bold]systemu setup[/bold] when "
                              "ready.[/yellow]")
                ctx.exit(1)   # DEC-41: aborted != success
            # Reload config so the freshly-written key/preset take effect.
            config, vault = _get_vault_and_config(ctx)
        else:
            console.print(f"[red]{_no_provider_message(config)}[/red]")
            ctx.exit(1)       # DEC-41: aborted != success

    console.print(f"\n[cyan].. Starting Systemu daemon on port {port} ...[/cyan]")
    verdict = start_daemon(
        vault_dir=config.vault_dir,
        config=config,
        vault=vault,
        port=port,
        foreground=foreground,
        wait_timeout_s=wait_s,
    )
    if foreground:
        return

    # DEC-41 / DEC-43: the claim below is gated on the MINTED witness, never on
    # the fact that Popen returned. `daemon status` consumes the same mint, so
    # the two surfaces cannot contradict each other.
    # The vault-root fence (DEC-32) is a REFUSAL, not a failed start: nothing
    # was spawned, nothing was written, and the remedy is a directory change —
    # not `daemon stop`. Reported on its own branch so the operator is not sent
    # to a daemon.log that was never opened.
    if verdict is not None and verdict.refused:
        from rich.markup import escape as _esc
        console.print(f"[red]{_esc(verdict.reason)}[/red]")
        ctx.exit(_daemon_mod.VAULT_ROOT_REFUSED_EXIT)

    # D5 / DEC-32c: the lifecycle VERDICT lines are ASCII. `OK` / `ERROR` / `..`
    # replace the check, cross and lightning glyphs these carried. A console
    # that cannot encode a verdict does not print a plainer one -- it raises a
    # UnicodeEncodeError instead of printing anything, and `daemon start` and
    # `daemon stop` are the first two commands a new install runs. (The panel
    # bullets on `daemon status` are decoration on a bordered Rich panel, not
    # verdicts, and stay as they are.)
    if verdict is not None and verdict.ready:
        console.print("[green]OK Daemon ready.[/green]")
        console.print(f"  Accepting connections on {verdict.url}")
        _print_daemon_build(verdict.build_match, verdict.build_note)
        console.print("  Use [bold]systemu daemon status[/bold] to check.")
        # The MINTED verdict is handed back so a caller (`systemu start`) can
        # gate on the same witness instead of re-deriving one. Click ignores a
        # command callback's return value, so nothing about `daemon start` as
        # an operator sees it changes here.
        return verdict

    console.print("[red]ERROR Daemon did not become ready.[/red]")
    reason = verdict.reason if verdict is not None else "no readiness verdict was produced"
    console.print(f"  {reason}")
    console.print(f"  Log: {Path(config.vault_dir) / 'daemon.log'}")
    console.print("  Stop the stuck process with: [bold]systemu daemon stop[/bold]")
    ctx.exit(1)


@daemon_group.command("stop")
@click.option("--all", "stop_all", is_flag=True, default=False,
              help="Kill ALL systemu daemon processes "
                   "(incl. orphans from prior runs that aren't in the pidfile).")
@click.pass_context
def daemon_stop(ctx, stop_all: bool):
    """Stop the running Systemu daemon.

    By default stops only the daemon tracked in the pidfile.  Use --all to
    sweep up orphan daemon processes (e.g. when an old daemon survived a
    crash or was spawned by a different installation).
    """
    if stop_all:
        # v0.8.0.2: kill every python process whose cmdline mentions our
        # daemon module.  This sidesteps the pidfile and catches orphans.
        import psutil
        killed = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "systemu.scheduler.daemon" in cmdline:
                    proc.kill()
                    killed.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            console.print(
                f"[green]Killed {len(killed)} daemon process(es): "
                f"{', '.join(str(p) for p in killed)}[/green]"
            )
        else:
            console.print("[dim]No systemu daemon processes found.[/dim]")
        return

    # Default path: pidfile-based single-daemon stop (preserve original logic)
    config, _ = _get_vault_and_config(ctx)
    from systemu.scheduler.daemon import stop_daemon

    stopped = stop_daemon(config.vault_dir)
    if stopped:
        console.print("[green]OK Daemon stopped.[/green]")
    else:
        console.print("[yellow]Daemon is not running.[/yellow]")


#: D6 -- the process exit codes `daemon status` answers with. Named here rather
#: than written as bare integers at the three `ctx.exit` sites so the values a
#: script depends on and the values `--help` documents are ONE definition.
#:
#: 1 for "not running" follows the shell convention an operator already relies
#: on (`systemu daemon status && open <url>`); 2 for "starting" is a distinct,
#: non-zero, RETRYABLE state -- a wait loop must be able to tell "not up yet"
#: from "not there at all" without parsing a Rich panel.
_STATUS_EXIT_READY = 0
_STATUS_EXIT_STARTING = 2
_STATUS_EXIT_NOT_RUNNING = 1


@daemon_group.command("status")
@click.option("--port", default=None, type=int,
              help="Port to witness. Defaults to the port the running daemon recorded.")
@click.pass_context
def daemon_status(ctx, port):
    """Show the Systemu daemon status.

    The verdict is the SAME witness `daemon start` waits on -- a real connection
    to the dashboard port. A live PID is not a listening socket, so a daemon
    that is still migrating is reported as STARTING, not as running.

    Every verdict also names the vault it is about and where the port number
    came from, each on its own unwrapped line.

    The EXIT CODE names the verdict, so a script never has to read the panel:

    \b
      0  Ready       -- the dashboard port accepted a connection
      2  Starting    -- the tracked process is alive, nothing is listening yet
      1  Not running -- nothing this vault tracks is serving that port; this
         includes the case where something IS listening on the port but no
         daemon is tracked for this vault, which is a foreign program on the
         socket and not your dashboard

    A build skew recolours the panel but never changes the exit code.
    """
    config, _ = _get_vault_and_config(ctx)
    from systemu.scheduler.daemon import get_status

    status = get_status(config.vault_dir, port=port)
    # F13: WHICH build is part of the report whether or not there is a problem —
    # "Ready" alone is exactly the surface that let a stale daemon pass for
    # twenty minutes. A skew recolours the panel but never changes the exit code.
    _bmatch = status["build_match"]
    _bnote = status["build_note"]
    # D6: the exit code IS the verdict. Every branch below used to fall off the
    # end of the function and leave 0, so `systemu daemon status && open <url>`
    # opened a dashboard that was not there -- the one bit a script, a CI step
    # or a health check can act on said "up" for all three answers. The code is
    # taken from the SAME branch that renders the panel, so the two can never
    # disagree; deriving it a second time from `status` would be the defect
    # class, not the fix.
    if status["ready"]:
        exit_code = _STATUS_EXIT_READY
        console.print(Panel(
            f"[green]● Ready[/green]  (PID {status['pid']})\n"
            f"{status['url']}",
            title="⚡ Systemu Daemon",
            border_style=("red" if _bmatch is False else "green"),
        ))
        _print_daemon_build(_bmatch, _bnote)
    elif status["process_alive"]:
        exit_code = _STATUS_EXIT_STARTING
        console.print(Panel(
            f"[yellow]◐ Starting[/yellow]  (PID {status['pid']})\n"
            f"{status['reason']}\n"
            "Nothing can reach the dashboard yet.",
            title="⚡ Systemu Daemon", border_style="yellow"
        ))
        _print_daemon_build(_bmatch, _bnote)
    else:
        exit_code = _STATUS_EXIT_NOT_RUNNING
        console.print(Panel(
            "[dim]○ Not running[/dim]\n"
            f"{status['reason']}\n"
            "Start with: [bold]systemu daemon start[/bold]",
            title="⚡ Systemu Daemon", border_style="dim"
        ))

    # Consumed HERE, by name, from the mint's projection -- so that deleting the
    # consumption deletes it from this function's source, which is what the
    # reachability pin in tests/test_e2e28_daemon_status_every_verdict_names_the
    # _vault.py watches. `.get` rather than `[...]`: a status dict from an older
    # daemon build is missing keys, and `daemon status` must still print.
    _print_daemon_where(status.get("vault_root"), status.get("port_provenance"))

    # LAST, so a non-zero verdict never costs the operator the report: the
    # panel, the vault line and the provenance line are all on the terminal
    # before the code is named. The code is an ADDITION to the report, not a
    # replacement for it.
    ctx.exit(exit_code)


def _print_daemon_where(vault_root, port_provenance) -> None:
    """The two facts that make ANY verdict readable: which vault, whose port.

    N3 -- these used to ride only on ``status["reason"]``, which the renderer
    above prints on the Starting and Not-running panels and NOT on Ready. So the
    Ready panel was PID + URL + build line, and "Ready" for WHICH vault, on WHOSE
    port, was unanswerable on a machine with two of either.

    N7 -- and they are emitted with ``click.echo`` OUTSIDE the panel, because
    Rich folds a panel body to the console width: the witnessed Not-running panel
    split an absolute vault path across two lines (`...objectiv` / `e-wescoff...`).
    The `roots` group above already carries the ruling this broke -- *a path
    wrapped at column 80 is not a path* -- and a folded path can be neither
    pasted nor searched for. The panel keeps the verdict, the PID and the URL,
    which fit; the path-shaped facts go where nothing wraps them.

    Both values are CONSUMED from the mint's projection (``vault_root`` and the
    provenance clause ``get_status`` carries). Re-deriving either here would put
    a second answer on the surface, which is the defect class, not the fix.
    """
    from systemu.scheduler.daemon import vault_note

    if type(vault_root) is str and vault_root:
        click.echo(vault_note(vault_root))
    if type(port_provenance) is str and port_provenance:
        click.echo(port_provenance)


# -----------------------------------------------------------------------------
#  Phase 4 / R-A4 -- granted roots (the REVOKE half)
# -----------------------------------------------------------------------------
#  `systemu/runtime/granted_roots.py` is the filesystem confinement set: the
#  folders Systemu is allowed to look inside. Its READ side has been wired into
#  production since G2 --- `situational_inventory.build_roots` surveys every
#  granted root, and `requirement_binder` re-gates resolver source #1 through
#  `is_within_granted`. Its WRITE side had no operator surface at all, so a
#  grant could be made but never taken back.
#
#  This group is that missing half, and ONLY that half. Output is line-oriented
#  (click.echo, unwrapped) rather than a rich table on purpose: these lines carry
#  absolute paths, and a path wrapped at column 80 is not a path.

@click.group("roots")
def roots_group():
    """Grant, list and revoke the folders Systemu is allowed to look inside.

    A granted root is a directory Systemu may survey and read files from; every
    path it resolves is checked against this set after canonicalization, so a
    folder that is not in it is out of reach even when named absolutely.

    A grant is EXPLICIT OPERATOR CONSENT and is never automatic: nothing in
    Systemu can add a root on its own, no task can widen its own reach mid-run,
    and `roots grant` tells you exactly what it means before it asks. Consent
    given here is not permanent either --- `roots revoke` ends it, and the next
    survey stops reading.
    """


def _granted_roots_store(ctx):
    """The one GrantedRootsStore this CLI touches, on the live vault root.

    The same base_dir `situational_inventory` and `requirement_binder` construct
    theirs from --- that agreement is what makes a revoke here visible to the
    survey there, and it is pinned end to end in tests/test_p4_roots_cli_revoke.py.
    """
    _config, vault = _get_vault_and_config(ctx)
    from systemu.runtime.granted_roots import GrantedRootsStore
    return GrantedRootsStore(base_dir=vault.root)


@roots_group.command("list")
@click.pass_context
def roots_list(ctx):
    """Show every folder currently granted, one canonical path per line."""
    store = _granted_roots_store(ctx)
    roots = store.list_roots()

    if not roots:
        click.echo("No folders are granted.")
        click.echo(
            "Systemu can only look inside folders you have granted it, and nothing "
            "grants one on its own. Grant one with: systemu roots grant <path> "
            "- it states exactly what granting means and asks you to confirm first."
        )
        return

    click.echo(f"{len(roots)} granted folder(s):")
    for root in roots:
        click.echo(f"  {root}")
    # Honest about what is NOT here: the store persists a bare path set
    # ({"version": 1, "roots": [...]}), so there is no per-grant time to show.
    # The file's mtime would be the time of the last WRITE --- most likely a
    # revoke of some other root --- which is a plausible-looking wrong answer.
    click.echo("")
    click.echo("Paths are shown canonicalized (symlinks and .. resolved), as stored.")
    click.echo("The store records the path only - no grant timestamp is kept.")
    click.echo("Revoke one with: systemu roots revoke <path>")


def _print_grant_consent(canon: str) -> None:
    """State LITERALLY what granting this folder does, before anything is written.

    Written to be true rather than reassuring. The survey reads metadata, not file
    contents, so this says metadata --- but it does not stop there, because a file
    NAME is content: `resignation-letter-final.docx` tells the story before
    anything is opened, and those names travel into prompts sent to a third party.
    Saying "Systemu will index this folder" would be accurate and would hide
    exactly the part a person would want to weigh.
    """
    click.echo(f"About to grant: {canon}")
    click.echo("")
    click.echo("What granting this folder means, literally:")
    click.echo(
        "  - The situational inventory will SURVEY it: the names, sizes and "
        "modification times of the files inside, and the shape of the directory "
        "tree. It reads file metadata this way, not file contents, and the scan "
        "is bounded to the most recently changed files rather than the whole tree."
    )
    click.echo(
        "  - Those file names and that structure can be placed INSIDE PROMPTS "
        "SENT TO YOUR MODEL PROVIDER. A file name is content: it can disclose a "
        "diagnosis, an employer, a lawyer or a plan before any file is opened."
    )
    click.echo(
        "  - Paths inside this folder become resolvable by the agent, so a task "
        "can reach a file here that it could not reach before."
    )
    click.echo(
        "  - This is the whole folder, including everything added to it later."
    )
    click.echo("")


@roots_group.command("grant")
@click.argument("path")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Confirm without prompting (for scripts). Still prints what is "
                   "being agreed to.")
@click.pass_context
def roots_grant(ctx, path: str, assume_yes: bool):
    """Grant Systemu access to a folder, after telling you what that means.

    Prints the consequences, then asks. The default is NO.

    \b
    Exit codes:
      0  granted
      1  you were asked and declined (a bare Enter counts as no)
      2  there was no terminal to ask on -- nothing was granted; re-run with
         --yes once you have read what is printed above the question

    `--yes` is the deliberate script path: it means you have read the
    disclosure, which is printed either way. A `y` arriving on a pipe is not
    consent and is refused with exit 2 -- the same rule `census grant` uses.

    The argument is canonicalized by the STORE'S OWN `canonicalize`, the same
    function `roots revoke` and the confinement check use, so the root recorded
    is the root those two will later compare against.

    Refuses a path that is not an existing directory --- before asking for
    consent, because there is nothing to consent to. Recording a "root" the
    survey can never walk would leave a grant that reads as live and grants
    reach to nothing.
    """
    from systemu.runtime.granted_roots import canonicalize

    store = _granted_roots_store(ctx)
    canon = canonicalize(path)

    # --- refuse before asking, before consent is even mentioned ---
    if not os.path.exists(canon):
        click.echo(f"Refused: that path does not exist: {canon}")
        click.echo("Nothing was granted.")
        ctx.exit(1)
    if not os.path.isdir(canon):
        click.echo(f"Refused: that path is not a directory: {canon}")
        click.echo("A grant names a folder, not a file. Nothing was granted.")
        ctx.exit(1)

    # --- already granted: nothing changes, so nothing to consent to ---
    # Re-asking here would teach the operator to type `y` at a prompt that means
    # nothing, which is how a consent prompt stops being read.
    if canon in store.list_roots():
        click.echo(f"Already granted: {canon}")
        click.echo("Nothing changed. Revoke it with: systemu roots revoke <path>")
        return

    _print_grant_consent(canon)

    # D12: THE shared consent rule, not a second private copy of it. This
    # command used to accept a piped `y` as consent and exit 1 on EOF, while
    # `census grant` -- the other standing-permission surface, same shape --
    # required a terminal and exited 2. The census rule is the one that
    # survived: a byte off a pipe is not a person who read the paragraph above.
    answer = consent_prompt.ask_for_consent(
        "Grant access to this folder?", assume_yes=assume_yes)
    if answer == consent_prompt.CONSENT_NO_TERMINAL:
        # A prompt into a stdin nobody can answer surfaces as a bare abort with
        # no way forward, so the refusal names the flag that unblocks it.
        click.echo("")
        click.echo("Refused: there is no terminal to ask on "
                   "(Docker / CI / a service).")
        click.echo(f"Re-run with --yes once you have read the above: "
                   f"systemu roots grant {canon} --yes")
        click.echo("Nothing was granted.")
        ctx.exit(consent_prompt.CONSENT_NO_TERMINAL)
    if answer != consent_prompt.CONSENT_GRANTED:
        click.echo("Refused: not granted. Nothing was changed.")
        ctx.exit(consent_prompt.CONSENT_DECLINED)

    store.grant(path)
    click.echo(f"Granted: {canon}")
    click.echo("Revoke it at any time with: systemu roots revoke <path>")


@roots_group.command("revoke")
@click.argument("path")
@click.pass_context
def roots_revoke(ctx, path: str):
    """Revoke a granted folder, so Systemu stops reading inside it.

    Exit code 0 if a grant was removed, 1 if the path was not a granted root ---
    so a script can tell a revoke that worked from one that silently missed.

    The argument is canonicalized by the STORE'S OWN `canonicalize` and by
    nothing else: symlinks, junctions and `..` are resolved, and on Windows the
    case and any 8.3 alias are folded, exactly as they were when the grant was
    recorded. A second normalization here would be free to disagree with the
    store's, and the failure it produces is the quiet one --- this command
    reporting a revoke that never happened.

    Note this takes a GRANTED ROOT, not any path inside one. A file within a
    granted folder is reachable BECAUSE of that folder's grant; revoking the
    enclosing folder on the strength of a file named inside it would withdraw
    more than was asked, so that is reported as "not granted" instead.
    """
    from systemu.runtime.granted_roots import canonicalize

    store = _granted_roots_store(ctx)
    # Display form only; `revoke` re-derives it from the same function, so the
    # store stays the single authority on what this path IS.
    canon = canonicalize(path)

    if store.revoke(path):
        click.echo(f"Revoked: {canon}")
        click.echo("The next survey will stop reading inside it.")
        return

    click.echo(f"Not granted: {canon}")
    click.echo("Nothing was changed. Run `systemu roots list` to see what is granted.")
    ctx.exit(1)


# -----------------------------------------------------------------------------
#  `systemu start` -- the one-command golden path
# -----------------------------------------------------------------------------
#  One command for a first run: start the daemon, then put the operator in
#  front of the dashboard. It is a THIN CALLER of `daemon start` --- the
#  provider gate, the interactive setup fallback, the vault-root refusal
#  (DEC-32) and the DEC-41 readiness witness are that command's code, reached
#  through `ctx.invoke`, never a second copy that can drift out of agreement
#  with the first.
#
#  DEC-41 IN UX FORM. Opening a browser is a CLAIM that something is serving at
#  that URL. So the browser is gated on the SAME minted witness the exit code is
#  gated on: no ready verdict, no browser, and the nonzero exit propagates
#  untouched. A browser pointed at a daemon that never bound its port is the
#  false assertion `daemon start` already refuses to make in words.

def should_open_browser(verdict, *, no_browser: bool, interactive: bool) -> bool:
    """PURE. The minted readiness verdict (plus the two suppressors) decides.

    Answers the question only --- it opens nothing, prints nothing, and reads
    no environment --- so the rule can be tested without a daemon anywhere near
    it.

    Fail-closed on every axis (DEC-36: the concrete type is pinned in this
    frame, because `or` / `!=` / `bool()` all dispatch to the operand):

      * no verdict, or a REFUSED one --- nothing was spawned; there is no URL
      * `ready` that is not the literal bool `True` --- a truthy stand-in is
        not the witness `probe_readiness` mints
      * ``no_browser`` --- the operator said not to
      * a non-interactive session --- `start` must then do exactly what
        `daemon start` does today, which is nothing. Not a style choice: on a
        DISPLAY-less box `webbrowser` can fall through to a console browser it
        runs with `p.wait()`, hanging a CI job on a daemon that is genuinely up.
    """
    if type(no_browser) is not bool or no_browser:
        return False
    if type(interactive) is not bool or not interactive:
        return False
    if verdict is None:
        return False
    refused = getattr(verdict, "refused", False)
    if type(refused) is not bool or refused:
        return False
    ready = getattr(verdict, "ready", None)
    return type(ready) is bool and ready


def _is_interactive() -> bool:
    """Is there a human at a terminal? Same predicate `daemon start` uses for
    its interactive setup fallback, so the two agree on what "headless" means."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _open_dashboard(url: str) -> bool:
    """Best effort, and it SAYS which. Never raises, never changes the exit.

    The daemon is up and the URL was printed by `daemon start` either way, so a
    box with no browser is a note --- turning it into a failure would red a
    working install for a cosmetic reason.
    """
    try:
        import webbrowser
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if type(opened) is bool and opened:
        console.print(f"  Opened {url} in your browser.")
        return True
    console.print(f"  [dim]No browser opened here - visit {url}[/dim]")
    return False


@click.command("start")
@click.option("--port", default=8765, show_default=True,
              help="Port for the web dashboard.")
@click.option("--no-browser", "no_browser", is_flag=True, default=False,
              help="Start the daemon but do not open a browser "
                   "(headless boxes, scripts, CI).")
@click.pass_context
def start_cmd(ctx, port: int, no_browser: bool):
    """Start Systemu and open the dashboard.

    The whole first run in one command: it does exactly what
    `systemu daemon start` does - same provider gate, same setup fallback,
    same refusals, same exit codes - and then, once a real connection to the
    dashboard has been witnessed, opens it in your browser.

    The dashboard ships as an optional extra, so a default install does not
    have it and this command refuses at once. Add it with:

    
        pip install "systemu[dashboard]"

    (The quotes matter on zsh, which reads the brackets as a file pattern.)
    Everything else - recording, analysis, tools, the rest of the CLI - works
    on the default install.

    No witness, no browser: a start that did not become ready keeps its own
    nonzero exit and its own diagnosis. Use --no-browser on a headless box.
    """
    verdict = ctx.invoke(daemon_start, port=port, foreground=False, wait_s=None)
    if should_open_browser(verdict, no_browser=no_browser,
                           interactive=_is_interactive()):
        _open_dashboard(verdict.url)


# ─────────────────────────────────────────────────────────────────────────────
#  doctor — self-diagnosis (R-UX1 / SPEC §15-UX UX-4)
# ─────────────────────────────────────────────────────────────────────────────
#  A NEW top-level command (distinct from `tools deps doctor`, which scans
#  cross-tool dep conflicts). When a user sees "nothing happening", `doctor`
#  answers WHY — killed/absent provider, locked keyring, dead daemon — and
#  renders the ONE deterministic platform capability profile. Exits NONZERO
#  when a real (blocking) problem is present.

def _doctor_daemon_build_text(build: dict) -> str:
    """F13 — the "Daemon build" cell. UNVERIFIED is its own answer, never blank
    and never mistakable for agreement."""
    if type(build) is not dict:
        return "unknown"
    if not build.get("observed") and build.get("match") is None:
        return "— (no daemon observed)"
    if build.get("match") is False:
        return (f"MISMATCH — daemon {build.get('daemon_version')} from "
                f"{build.get('daemon_path')}")
    if build.get("match") is True:
        return f"{build.get('daemon_version')} (same build as this CLI)"
    return "UNVERIFIED — the daemon did not record which build it loaded"


def _render_doctor_report(report: dict) -> None:
    """Paint the self-diagnosis report (never prints a secret VALUE)."""
    # F28: hoisted to the TOP of the function. It used to be imported inside the
    # `if og:` block near the end, which covered the Optional-groups table only —
    # so the Diagnosis table 80 lines above, the first thing an operator reads,
    # printed `pip install "systemu"` with the extra eaten by Rich markup while
    # the table below it printed the same remedy correctly. Every cell carrying
    # operator text in this function needs it.
    from rich.markup import escape as _esc

    prof = report["profile"]

    # -- headline status --------------------------------------------------
    if report["ok"]:
        if any(p["severity"] == "warning" for p in report["problems"]):
            console.print("[yellow]▲ Systemu is usable, with warnings.[/yellow]")
        else:
            console.print("[green]● Systemu looks healthy.[/green]")
    else:
        console.print("[red]✗ Systemu has a blocking problem — see below.[/red]")

    # -- problems ---------------------------------------------------------
    if report["problems"]:
        tbl = Table(title="Diagnosis", show_header=True, header_style="bold")
        tbl.add_column("", width=3)
        tbl.add_column("Problem")
        tbl.add_column("Fix")
        for p in report["problems"]:
            mark = "[red]✗[/red]" if p["blocking"] else "[yellow]▲[/yellow]"
            # F28: ESCAPE. Square brackets are Rich markup, so an unescaped
            # remedy lost its extra: this table printed `pip install "systemu"`
            # while the Optional-groups table below (which does escape) printed
            # `pip install "systemu[browser]"`. The headline fix an operator
            # reads first was a command that installs nothing.
            tbl.add_row(mark, _esc(p["message"]), _esc(p.get("cta", "")))
        console.print(tbl)

    # -- live status ------------------------------------------------------
    prov = report["provider"]
    prov_txt = ("not configured" if not prov["configured"]
                else ("unreachable" if prov["reachable"] is False else "configured"))
    kr = report["keyring"]
    kr_txt = f"{kr['backend']}{' (LOCKED)' if kr['locked'] else ''}"
    dae = report["daemon"]["running"]
    dae_txt = "running" if dae else ("not running" if dae is False else "unknown")

    status = Table(show_header=True, header_style="bold", title="Status")
    status.add_column("Check")
    status.add_column("Value")
    status.add_row("LLM provider", prov_txt)
    status.add_row("Providers usable",
                   ", ".join(p["display"] for p in report.get("providers", ())
                             if p.get("satisfied")) or "none")
    status.add_row("Keyring backend", kr_txt)
    status.add_row("Daemon", dae_txt)
    # F13 / GATE-7a: a bare "systemu version" row sitting next to "Daemon:
    # running" reads as the DAEMON's version — and for twenty minutes of a real
    # session it was not. Both builds are named, and each says whose it is.
    status.add_row("systemu version (this CLI)",
                   report["versions"].get("systemu", "?"))
    _build = report["daemon"].get("build") or {}
    status.add_row("systemu path (this CLI)", _build.get("cli_path") or "?")
    status.add_row("Daemon build", _doctor_daemon_build_text(_build))
    status.add_row("python version", report["versions"].get("python", "?"))
    if report.get("last_error"):
        status.add_row("Last error", str(report["last_error"]))
    console.print(status)

    # -- F19: every provider, from the same mint the dashboard reads -------
    # `doctor` used to report a single "LLM provider: not configured" row
    # derived from OPENROUTER_API_KEY, on a machine where the dashboard was
    # simultaneously reporting a reachable Ollama. Same fact, two surfaces, two
    # answers. Presentation is keyed by STATE, never by provider.
    provs = report.get("providers") or ()
    if provs:
        pt = Table(show_header=True, header_style="bold",
                   title="Providers (credentials are never printed)")
        pt.add_column("Provider")
        pt.add_column("Status")
        pt.add_column("Detail")
        for p in provs:
            mark, word = _PROVIDER_MARK.get(p["state"], ("[dim]?[/dim]", "Unknown"))
            # F28: the provider detail carries remedies too ("add GOOGLE_API_KEY
            # to .env", "run `systemu setup ...`") and a future one could carry an
            # extra. `mark` is OUR OWN markup and stays unescaped deliberately.
            pt.add_row(_esc(p["display"]), f"{mark} {word}", _esc(p["detail"]))
        console.print(pt)

    # -- the platform capability profile (the ONE cross-OS map) -----------
    cap = Table(show_header=True, header_style="bold", title="Platform capability profile")
    cap.add_column("Capability")
    cap.add_column("Value")
    cap.add_row("OS / arch", f"{prof['os']} ({prof['os_family']}) / {prof['arch']}")
    cap.add_row("Docker mode", "yes" if prof["docker_mode"] else "no")
    cap.add_row("Capture available", "yes" if prof["capture_available"] else "no")
    cap.add_row("Keyring backend", prof["keyring_backend"])
    cap.add_row("Forged-network jail", prof["forged_net_jail"])
    cap.add_row("Provider configured", "yes" if prof["provider_configured"] else "no")
    console.print(cap)

    # -- F21 optional capability groups -----------------------------------
    # Rendered UNCONDITIONALLY, installed or not. A table that only appears
    # when something is missing teaches operators nothing about what exists,
    # and a "missing" row is only legible next to the rows that are present.
    og = report.get("optional_groups") or ()
    if og:
        # `_esc` is hoisted to the top of this function (F28) — the remedy
        # contains `systemu[dashboard]` and Rich would parse `[dashboard]` as a
        # style tag and drop it, printing a `pip install systemu` that installs
        # nothing. Same trap as `tools list`.
        ot = Table(show_header=True, header_style="bold",
                   title="Optional capability groups")
        ot.add_column("Capability")
        ot.add_column("State")
        ot.add_column("Covers")
        ot.add_column("Install")
        for g in og:
            state = ("[green]installed[/green]" if g["installed"]
                     else "[red]UNAVAILABLE[/red]")
            ot.add_row(_esc(g["label"]), state, _esc(g["covers"]),
                       _esc(g["remedy"]) or "—")
        console.print(ot)

    # -- DEP-10 host-capability honesty rows ------------------------------
    hc = Table(show_header=True, header_style="bold",
               title="Host capabilities (DEP-10 — never faked in a container)")
    hc.add_column("Capability")
    hc.add_column("Available")
    hc.add_column("Via")
    hc.add_column("Note")
    for row in prof["host_capabilities"]:
        hc.add_row(row["label"], "yes" if row["available"] else "no",
                   row["via"], row["note"] or "—")
    console.print(hc)


def run_self_diagnosis() -> int:
    """Build + render the whole-system self-diagnosis and return its exit code
    (nonzero on a blocking problem). Shared by ``doctor_cmd`` here and the
    bare ``sharing_on doctor`` (no scope_id) entry point."""
    from systemu.runtime import platform_profile as pp

    report = pp.build_doctor_report()
    _render_doctor_report(report)
    return pp.report_exit_code(report)


@click.command("doctor")
def doctor_cmd():
    """Self-diagnosis: WHY is nothing happening? Reports provider / keyring /
    daemon health and the platform capability profile. Exits nonzero on a real
    problem (killed provider, locked keyring)."""
    code = run_self_diagnosis()
    if code != 0:
        import sys as _sys
        _sys.exit(code)


# ─────────────────────────────────────────────────────────────────────────────
#  debug group (v0.4.0-0)
# ─────────────────────────────────────────────────────────────────────────────
#  Operator-facing diagnostics for failure-mode analysis.  Lives under a
#  separate group so it's visually distinct from production commands.

@click.group("debug")
def debug_group():
    """Diagnostics and failure-mode analysis."""


@debug_group.command("suggest-specialty")
@click.argument("shadow_id")
@click.pass_context
def debug_suggest_specialty(ctx, shadow_id: str):
    """Analyse a shadow's memory and suggest a specialty tag (v0.4.4-c).

    Reads SHADOW_MEMORY.md + memory_buffer.jsonl and counts curated
    keyword matches.  Suggests a specialty when one tag has 5 or more hits
    and at least 40% of total matched hits.  Operator applies via Workshop
    edit dialog -- this command is inspection-only.
    """
    _, vault = _get_vault_and_config(ctx)
    from systemu.runtime.specialty_suggester import suggest_specialty
    result = suggest_specialty(shadow_id, vault=vault)

    if not result.total_hits:
        console.print(
            f"[dim]No domain-keyword matches in shadow {shadow_id}'s memory "
            f"(scanned {result.sources_scanned} source(s)).  Operator should "
            f"set specialty manually via Workshop.[/dim]"
        )
        return

    if not result.suggested_specialty:
        console.print(
            f"[yellow]Found {result.total_hits} keyword matches but no clear "
            f"winner (confidence {result.confidence:.0%} below 40% threshold)."
            f"[/yellow]"
        )
    else:
        console.print(
            f"[green]Suggested specialty:[/green] "
            f"[bold]{result.suggested_specialty}[/bold]  "
            f"[dim](confidence {result.confidence:.0%}, {result.total_hits} hits)[/dim]"
        )

    table = Table(title="Keyword hit breakdown", show_lines=False)
    table.add_column("Specialty", style="cyan")
    table.add_column("Hits",      justify="right", style="bold")
    for specialty, count in sorted(
        result.by_specialty.items(), key=lambda kv: -kv[1],
    ):
        table.add_row(specialty, str(count))
    console.print(table)


@debug_group.command("tool-metrics")
@click.option("--low-success", is_flag=True,
              help="Only show tools with success_rate below --threshold.")
@click.option("--threshold", default=0.5, type=float,
              help="Success-rate cutoff for --low-success (default 0.5).")
@click.option("--min-calls", default=5, type=int,
              help="Minimum attributable calls before a tool can be flagged.")
def debug_tool_metrics(low_success: bool, threshold: float, min_calls: int):
    """Per-tool success rate + failure breakdown (v0.4.4-a).

    Reads ``data/tool_metrics.json``.  Tools sorted by lowest success
    rate first so flaky tools surface immediately.  Dependency-blocked
    failures (missing pip packages awaiting approval) are tracked
    separately and don't penalise the tool's success rate.
    """
    from systemu.runtime.tool_metrics import get_tool_metrics
    store = get_tool_metrics()
    rows = (
        store.low_success_tools(threshold=threshold, min_calls=min_calls)
        if low_success else store.list_all()
    )
    if not rows:
        console.print("[dim]No tool metrics recorded yet.[/dim]")
        return

    table = Table(
        title=("⚠️ Low-success tools" if low_success else "🔧 Tool metrics"),
        show_lines=False,
    )
    table.add_column("Tool ID",   style="cyan")
    table.add_column("Calls",     justify="right")
    table.add_column("OK",        justify="right", style="green")
    table.add_column("Fail",      justify="right", style="red")
    table.add_column("DepBlock",  justify="right", style="yellow")
    table.add_column("Timeout",   justify="right", style="magenta")
    table.add_column("Rate",      justify="right", style="bold")
    table.add_column("Last failure", style="dim")
    for r in rows:
        rate_str = (f"{r['success_rate']:.2f}" if r["has_history"] else "—")
        table.add_row(
            r["tool_id"] or "—",
            str(r["calls"]),
            str(r["successes"]),
            str(r["failures"]),
            str(r["dependency_blocked"]),
            str(r["timeouts"]),
            rate_str,
            (r.get("last_failure_at") or "—")[:16],
        )
    console.print(table)


@debug_group.command("s4-shadow-meter")
@click.option("--min-runs", default=20, type=int,
              help="Coverage threshold for the Stage-3 arm-gate (default 20).")
@click.pass_context
def debug_s4_shadow_meter(ctx, min_runs: int):
    """SHADOW park-surface report for the external-verification net.

    Reads the record-only ``s4_shadow`` meter (``<vault>/metrics/metrics.json``) and
    renders, per effect-class, ``would_stamp / would_credit / would_park / park_rate``,
    then prints the pure Stage-3 arm-gate verdict (READY / NOT_READY + reasons).

    READ-ONLY diagnostics -- this command never writes the meter, the credit path, or any
    store. The verdict is NOT ``would_park==0``: it requires coverage >= --min-runs, no
    benign class stamping (effect_tags mis-wire => spurious park), and every stamped class
    with an actual credit channel (a stamp-only class => dead channel => spurious park).
    """
    from systemu.runtime.metrics_store import MetricsStore
    from systemu.runtime.s4_activation import (
        arm_verdict_line, format_shadow_meter_rows,
    )
    _, vault = _get_vault_and_config(ctx)
    snapshot = MetricsStore(Path(vault.root) / "metrics").shadow_meter_snapshot()
    if not snapshot:
        console.print(
            "[dim]no shadow-meter data yet — run in SHADOW mode "
            "(SYSTEMU_S4_STAMP=shadow) to accumulate.[/dim]"
        )
        return

    table = Table(title="🛰️  S4 SHADOW park-surface meter", show_lines=False)
    table.add_column("Effect class", style="cyan")
    table.add_column("Stamp",  justify="right")
    table.add_column("Credit", justify="right", style="green")
    table.add_column("Park",   justify="right", style="yellow")
    table.add_column("Park rate", justify="right", style="bold")
    for r in format_shadow_meter_rows(snapshot):
        table.add_row(
            r["effect_class"],
            str(r["would_stamp"]),
            str(r["would_credit"]),
            str(r["would_park"]),
            f"{r['park_rate']:.2f}",
        )
    console.print(table)
    # verdict via click.echo (unwrapped plain stdout so operators + tests read it whole).
    click.echo(arm_verdict_line(snapshot, min_runs=min_runs))


@debug_group.command("avoidable-forge")
@click.pass_context
def debug_avoidable_forge(ctx):
    """The avoidable-forge rate over the vault's forged tools.

    Deterministic post-hoc replay (never an LLM judge): for every forged tool,
    re-runs the capability-slot query -- does an EXISTING tool already occupy its
    slot (would it have bound instead of forging a duplicate)? It is the tripwire
    on whether keyword matching alone finds a tool you already have, and it is
    reported beside the avoidable-ask rate. READ-ONLY -- computes over the live
    capability index, writes nothing.
    """
    from systemu.runtime.replay_metrics import avoidable_forge_report, format_avoidable_forge
    _, vault = _get_vault_and_config(ctx)
    for line in format_avoidable_forge(avoidable_forge_report(vault)):
        click.echo(line)


def _persisted_requirement_rows(data_dir=None):
    """R-B5 / T5 (spec section 10) -- every persisted RequirementReport's requirements,
    flattened into ONE list for the inventory-hit metric.

    WHERE THE INPUT COMES FROM. A run's ``RequirementReport`` survives the run in
    exactly one place: ``ExecutionSnapshot.requirement_report``
    (``systemu/runtime/execution_snapshot.py``:102, serialised at :272), on disk at
    ``<data_dir>/audit/exec_<execution_id>/resume_snapshot.json`` (:54). Everywhere
    else it is held only on ``context._requirement_report`` for the life of the
    process (``shadow_runtime.py``:1545), so there is nothing else durable to read.
    The directory scan mirrors the one existing precedent for finding runs this way,
    ``scheduler/jobs.py``:806 (``_scan_wait_execution_ids``); ``data_dir`` defaults to
    ``execution_snapshot.audit_data_root()``, the SAME mint ``write_snapshot`` defaults
    to, so the reader and the writer resolve the same directory without a second
    derivation -- and without either of them resolving it against its own cwd.

    THE POPULATION IS SPARSE, AND THAT IS A PROPERTY OF THE INPUT, NOT A BUG HERE: a
    snapshot is written when a run suspends and DELETED when the resume consumes it
    (``delete_snapshot``, :230). The metric therefore reads the runs that persisted a
    report, never "all runs ever". The caller renders NOT MEASURED -- never 0% -- when
    the set is empty, because an unmeasured population is a different claim from a
    measured zero.

    READ-ONLY, and deliberately NOT via ``read_snapshot``: that entry point migrates
    and may raise ``SnapshotRefused`` on a newer schema (DEC-9), which is correct for
    a RESUME (it must not re-execute effects against a shape it cannot read) and wrong
    for a printout, which may never adjudicate a run. Anything unreadable is skipped;
    nothing here raises and nothing here writes.

    The flatten is ``table_payoff._requirements`` -- the metric's OWN projection,
    imported rather than re-implemented. Re-reading ``per_objective`` locally would be
    a second copy of the projection that module documents as single-owner (see its
    note on the deleted ``_is_ask`` mirror): it would drift silently, and the count
    would keep rendering while measuring something else.
    """
    from systemu.runtime import table_payoff
    from systemu.runtime.execution_snapshot import audit_data_root

    base = audit_data_root(data_dir)
    rows = []
    try:
        audit = base / "audit"
        if not audit.is_dir():
            return rows
        exec_dirs = sorted(audit.glob("exec_*"))
    except Exception:
        return rows

    for exec_dir in exec_dirs:
        try:
            snap = exec_dir / "resume_snapshot.json"
            if not snap.is_file():
                continue
            data = json.loads(snap.read_text(encoding="utf-8"))
        except Exception:
            continue                       # corrupt / unreadable => not counted
        if not isinstance(data, dict):
            continue
        report = data.get("requirement_report")
        if not isinstance(report, dict):
            continue
        rows.extend(table_payoff._requirements(report))
    return rows


@debug_group.command("avoidable-ask")
@click.pass_context
def debug_avoidable_ask(ctx):
    """Why did it ask? Four signals over the asks this vault has recorded.

    Deterministic (never a model judgement) and READ-ONLY -- running this
    command records nothing and changes nothing. Each signal is a different kind
    of evidence and they are LABELLED APART rather than blended: a proxy and a
    definitive count averaged together is neither.

    A signal with nothing behind it reports NOT MEASURED and NO RATE. That is
    not a zero, and it is never rendered as one.

    \b
      * NO-PRIOR-ATTEMPT asks -- a DIRECTIONAL proxy, counted when the ask was
        made: it asked you without any recorded attempt to resolve the answer
        first. Two-sided (it misses an avoidable ask that logged a failed
        attempt, and counts a necessary ask that had nothing to try), so it
        points a direction and does not settle anything.
      * ANSWER-LINKED asks -- counted when you ANSWER, so each row knows what
        you chose. Its resolvable-confirmed case is DEFINITIVE rather than a
        proxy: it already held that value and asked only to have it confirmed,
        and you changed nothing. Reported apart from the cases it could NOT have
        produced. Includes the ask-to-resolve trend. Credential asks are never
        recorded.
      * INVENTORY-HIT -- the same question from the other end: how often what it
        already knew (including your own table) turned a from-scratch gap into a
        pre-filled one-click confirm. The silent binds and the pre-filled
        confirms are separate counts and are never summed. Read from the
        requirement reports left behind in execution snapshots
        (``data/audit/exec_*/resume_snapshot.json``), so a vault whose runs
        never suspended has none, and the unmeasured line names the directory it
        searched.
      * QUICK-LANE asks -- the only one of the four about the quick lane, which
        writes none of the corpora above. One row per question, so how often the
        lane's own question cap ended a run, and how much of that traffic was
        it asking you the same thing twice, become visible. Questions are held
        as non-reversible references and secret-class asks are never recorded.
    """
    from systemu.runtime.replay_metrics import (
        avoidable_ask_report, format_avoidable_ask, format_quick_lane_ask,
        quick_lane_ask_report)
    from systemu.runtime.table_payoff import format_inventory_hit, inventory_hit_report
    _, vault = _get_vault_and_config(ctx)
    for line in format_avoidable_ask(avoidable_ask_report(vault)):
        click.echo(line)

    # R-B5 / T5 (section 10, section 5.10.e AC7) -- the inventory-hit payoff, printed
    # beside the avoidable-ask lines because the two answer the same operator question
    # from opposite ends: how often did it ask, and how often did the inventory spare
    # the ask. AC7 is a SPLIT, not a total -- format_inventory_hit renders `silent` and
    # `prefilled_confirm` as separate counts, and they must never be summed here or a
    # collapse in one would hide behind the other.
    #
    # READ-ONLY by construction: this reads inventory_hit_report / format_inventory_hit
    # only. The section 5.10.c chips are NOT rendered here -- answered_from_table WRITES
    # the novelty ledger, so a printout that called it would burn an item's novelty
    # every time an operator asked for metrics.
    from systemu.runtime.execution_snapshot import audit_data_root
    _audit_dir = audit_data_root() / "audit"
    rows = _persisted_requirement_rows()
    click.echo("")
    if not rows:
        # An unmeasured population is a different claim from a measured zero; the
        # zeros the formatter would print here would read as "the table never paid
        # off". Same rule (and wording) as resolver_replay's "this is NOT 0%".
        #
        # AND IT NAMES THE DIRECTORY IT SEARCHED. The sentence used to read "no run
        # has persisted a RequirementReport yet" -- a claim about every run on the
        # machine, printed on a machine that held one, because the search root came
        # off the operator's cwd. An unmeasured verdict is only honest when the
        # reader can see WHERE it looked and check.
        click.echo("Inventory-hit: NOT MEASURED -- no persisted RequirementReport "
                   "was found; NO RATE (this is NOT 0%)")
        click.echo(f"  (searched: {_audit_dir}{os.sep}exec_*"
                   f"{os.sep}resume_snapshot.json)")
        click.echo("  (the per-run report is cached in the execution snapshot there, "
                   "written when a run")
        click.echo("   suspends and consumed when it resumes.)")
    else:
        for line in format_inventory_hit(inventory_hit_report(rows)):
            click.echo(line)

    # R-QL1 (DEC-7) -- the QUICK-LANE ask surface. DEC-7's amended criterion is about
    # the quick lane's operator-question cap, and BOTH blocks above are deep-lane: they
    # read corpora the quick lane does not write, so neither can ever decide it. Until
    # this shipped, nothing recorded a quick-lane ask at all.
    #
    # Rendered HERE rather than behind its own command because an operator asking "why
    # does it keep asking me?" must see both lanes in one place -- and because a
    # separate command is a surface nobody runs.
    #
    # NEVER a fabricated 0%: below DEC-7's measurement window (30 asks across >=10
    # distinct runs) the block prints NOT MEASURED with the count it has and the floor
    # it needs, and no percentage at all. Same rule as the inventory-hit block above.
    # READ-ONLY, like the rest of this command -- a printout may not accrete the corpus
    # it reports on.
    for line in format_quick_lane_ask(quick_lane_ask_report(vault)):
        click.echo(line)


@debug_group.command("resolver-replay")
@click.option("--corpus", default=None, type=click.Path(),
              help="Scenario corpus directory (default: fixtures/field).")
def debug_resolver_replay(corpus):
    """The DEFINITIVE avoidable-ask rate, by resolver replay.

    This is the ask-side twin of ``avoidable-forge``, and it is a genuine replay:
    for each labelled scenario in ``fixtures/field/`` it re-runs the REAL resolver
    (``requirement_binder.compute_requirements`` -- the same function the runtime
    calls) over the scenario's recorded inventory, with the operator's answer known,
    and asks whether a source would have bound that answer. Never an LLM judge.

    \b
    HOW THIS DIFFERS FROM ``avoidable-ask``:
      * ``avoidable-ask`` AGGREGATES the live ``ask_corpus.jsonl``. Those rows carry
        no situation snapshot -- not even a schema_path -- so nothing can replay them.
        It is a directional proxy and says so.
      * this command REPLAYS. It can tell "the binder held exactly this value" apart
        from "the binder held a DIFFERENT value", which no attempt-counting signal
        can, at any sample size.

    Reads the checked-in fixture corpus, not the vault: no vault is required and
    nothing is written. A rate of "no assessable asks" is reported as such and is
    NEVER rendered as 0%.

    \b
    EXIT STATUS:
      0  DEFINITIVE -- the corpus reconciled against its roster (a rate, or an
         honest "no assessable asks")
      1  NOT definitive -- a scenario failed to load/replay, or the corpus drifted
         from roster.json (a rostered fixture is missing, or an unrostered one is
         present)

    The non-zero status is the point of this being a command rather than a
    printout: a scripted caller reads ``$?``, not prose, and the one state this
    command exists to surface is exactly the one where the printed rate is
    absent. Exiting 0 there hands the caller a clean run over a corpus the
    harness could not vouch for.
    """
    from pathlib import Path
    from systemu.runtime.resolver_replay import (
        format_resolver_replay, resolver_replay_report)
    report = resolver_replay_report(Path(corpus) if corpus else None)
    for line in format_resolver_replay(report):
        click.echo(line)
    # `definitive` is a DERIVED property — declared>0, no errors, no roster drift.
    # A dict `.get("complete")` cannot express it; the dataclass property can.
    if not report.definitive:
        raise SystemExit(1)


@debug_group.command("rejection-log")
@click.option("--clear", is_flag=True, help="Wipe the rejection store after listing.")
@click.option("--window-hours", default=None, type=int,
              help="Only show rejections from the last N hours.")
def debug_rejection_log(clear: bool, window_hours):
    """List operator-dismissed supervisor proposals (v0.4.1-c).

    Reads ``data/rejection_store.json`` (populated by the Systemu Chat
    dismiss handler).  The Intelligent Supervisor consults this store
    before re-proposing similar interventions.
    """
    from systemu.runtime.rejection_store import get_rejection_store
    store = get_rejection_store()
    rejections = store.list_rejections(window_hours=window_hours)
    if not rejections:
        console.print("[dim]No rejections recorded.[/dim]")
        if clear:
            console.print("Nothing to clear.")
        return

    table = Table(title=f"🚫 Operator rejections ({len(rejections)})", show_lines=False)
    table.add_column("Pattern signature", style="bold")
    table.add_column("First", style="dim")
    table.add_column("Last action", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for r in rejections:
        table.add_row(
            r.pattern_signature,
            (r.first_rejected_at or "")[:16],
            r.last_action or "—",
            str(r.reject_count),
        )
    console.print(table)

    if clear:
        n = store.clear()
        console.print(f"[yellow]Cleared {n} rejections.[/yellow]")


@debug_group.command("failure-histogram")
@click.option("--group-by", "-g", default="event_type,error_type,tool_name",
              help="Comma-separated fields to bucket on. "
                   "Available: event_type, error_type, tool_name, status, "
                   "failure_category, shadow_id, scroll_id.")
@click.option("--event-types", "-e", default=None,
              help="Restrict to specific event_types (comma-separated). "
                   "Available: tool_failure, execution_terminal, supervisor_diagnosis.")
@click.option("--top", "-n", default=20, type=int,
              help="Show top N rows by count.")
def debug_failure_histogram(group_by: str, event_types: Optional[str], top: int):
    """Print a histogram of recorded failure events.

    Reads ``data/failure_telemetry.jsonl`` (populated automatically by the
    runtime and supervisor since v0.4.0-0).  Useful for understanding what
    actually fails in this deployment before tuning the recovery layer.
    """
    from systemu.runtime.failure_telemetry import compute_histogram

    keys = [k.strip() for k in group_by.split(",") if k.strip()]
    types = [t.strip() for t in event_types.split(",")] if event_types else None
    rows = compute_histogram(group_by=keys, event_types=types)

    if not rows:
        console.print("[dim]No failure events recorded yet.[/dim]")
        console.print(
            "Trigger any shadow execution that fails to populate "
            "data/failure_telemetry.jsonl, then re-run this command."
        )
        return

    table = Table(
        title=f"📊 Failure histogram — top {min(top, len(rows))} of {len(rows)}",
        show_lines=False,
    )
    for k in keys:
        table.add_column(k, style="cyan")
    table.add_column("count", style="bold magenta", justify="right")

    for row in rows[:top]:
        table.add_row(
            *[str(row.get(k, "") or "—") for k in keys],
            str(row["count"]),
        )
    console.print(table)


# ── decisions group (v0.8.0 Pattern 1: OperatorDecisionQueue) ────────────────

@click.group("decisions")
def decisions_group():
    """Manage the OperatorDecisionQueue -- operator decisions awaiting resolution.

    The queue is the v0.8.0 operator-decision surface for non-TTY contexts.
    When a dashboard-spawned CLI subprocess needs operator input and
    SYSTEMU_DECISION_QUEUE=true is set, it posts a decision here for the
    operator to resolve via the dashboard /insights?tab=actions page or
    via these CLI commands.
    """


@decisions_group.command("list")
@click.pass_context
def decisions_list(ctx):
    """Show pending OperatorDecision records."""
    _, vault = _get_vault_and_config(ctx)
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)

    pending = queue.list_pending()
    if not pending:
        console.print("[dim]No pending decisions.[/dim]")
        return

    table = Table(title="Pending Operator Decisions", show_lines=True)
    table.add_column("ID",                style="dim",   no_wrap=True)
    table.add_column("Title",             style="bold")
    table.add_column("Risk",              style="yellow")
    table.add_column("Options",           style="cyan")
    table.add_column("What Approve does", style="green")
    table.add_column("Dedup key",         style="dim")
    table.add_column("Created",           style="dim")
    for d in pending:
        ts = d.created_at.isoformat(timespec="seconds") if d.created_at else ""
        ctx = getattr(d, "context", None) or {}
        risk = str(ctx.get("risk", "") or "").upper()
        what = str(ctx.get("what_approve_does", "") or "")[:60]
        table.add_row(
            d.id,
            (d.title or "")[:50],
            risk,
            ", ".join(d.options),
            what,
            d.dedup_key or "",
            ts,
        )
    # Render to a wide console so the extra columns don't wrap/truncate the
    # risk + what-Approve-does cells in non-TTY contexts (default width 80).
    from rich.console import Console as _Console
    _Console(width=200).print(table)


@decisions_group.command("mode")
@click.option("--set", "set_mode", default=None,
              type=click.Choice(["bypass", "risk_tiered", "approve_only"]),
              help="Set the gate mode dial (persisted to .env).")
@click.pass_context
def decisions_mode(ctx, set_mode):
    """Show or set the gate-mode dial.

    Modes:
      bypass        auto-grant every gate EXCEPT the safety floor (dep/recovery
                    + floor capabilities). DANGEROUS -- most gates run unattended.
      risk_tiered   the Governor (default): auto-grant low risk, ask otherwise.
      approve_only  always ask the operator.

    With no --set, prints the current mode + per-type overrides + floor state.
    """
    from systemu.runtime.gate_mode_settings import (
        get_gate_mode_settings, save_gate_mode_settings)

    if set_mode is not None:
        try:
            save_gate_mode_settings(mode=set_mode)
        except ValueError as exc:
            console.print(f"[red]Invalid mode:[/red] {exc}")
            ctx.exit(2)
            return
        console.print(f"[green]Gate mode set to[/green] [bold]{set_mode}[/bold].")

    state = get_gate_mode_settings()
    mode = state["mode"]
    overrides = state.get("overrides") or {}
    no_floor = bool(state.get("no_floor"))

    if set_mode is None:
        console.print(f"Gate mode: [bold]{mode}[/bold]")
    overrides_str = (
        ", ".join(f"{k}={v}" for k, v in sorted(overrides.items()))
        if overrides else "(none)")
    console.print(f"  Per-type overrides: {overrides_str}")
    console.print(
        f"  Safety floor: {'DISABLED (no_floor)' if no_floor else 'ON'} "
        f"— dep/recovery + floor capabilities always ask"
        + (" UNLESS no_floor is set" if no_floor else ""))

    # Console twin of the persistent banner — never silent about Bypass.
    if mode == "bypass":
        console.print(
            "[bold red]DANGER:[/bold red] Bypass auto-grants gates without "
            "asking (except the safety floor"
            + (", which is DISABLED" if no_floor else "")
            + "). Most actions will run unattended.")


def _reclassify_needs_the_inbox(choice: str) -> bool:
    """True iff this choice is IMPL-2's "Reclassify effect…" remedy, which this CLI
    cannot deliver.

    Reclassifying is not a plain option pick: it assigns an effect class under a TYPED
    confirmation and resolves through ``resolve_with_context_patch`` so the class rides
    along in the decision context. ``decisions resolve`` has neither — it would resolve
    the card with a bare label, the recorder would find no ``assigned_class`` and no
    ``typed_confirmed`` and store nothing, and the re-run would re-DENY. Net effect: the
    operator spends their one card and stays exactly where they were, which is the dead
    end IMPL-2 removes. Refuse and point at the Inbox instead.
    """
    return str(choice or "").strip().lower().startswith("reclassify")


@decisions_group.command("resolve")
@click.argument("decision_id")
@click.option("--choice", "-c", required=True, help="One of the decision's options.")
@click.pass_context
def decisions_resolve(ctx, decision_id: str, choice: str):
    """Resolve a pending decision with the operator's chosen option."""
    if _reclassify_needs_the_inbox(choice):
        console.print(
            "[red]Not available here.[/red] Reclassifying an effect requires a typed "
            "confirmation of the class you are assigning, which this command cannot "
            "collect — resolving it here would record nothing and re-refuse the "
            "action.\nOpen the dashboard [bold]Inbox[/bold] and use "
            "'Reclassify effect…' on the card.")
        ctx.exit(2)
        return
    _, vault = _get_vault_and_config(ctx)
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    try:
        resolved = queue.resolve(decision_id, choice=choice)
    except KeyError as exc:
        console.print(f"[red]Not found:[/red] {exc}")
        ctx.exit(1)
        return
    except ValueError as exc:
        console.print(f"[red]Invalid choice:[/red] {exc}")
        ctx.exit(2)
        return
    console.print(f"[green]Resolved {decision_id} -> {resolved.choice}[/green]")

    # Approve EXECUTES (spec §4.3): for Inbox gate rows, run the authorized
    # action. queue.resolve() returns the decision with .choice already set, so
    # resolve_gate's Approve-label check sees the operator's choice. Non-gate
    # (legacy harness/credential/etc.) rows are untouched — resolve_gate is only
    # invoked for context kind=="gate", so their resolve path is byte-identical.
    try:
        if (getattr(resolved, "context", None) or {}).get("kind") == "gate":
            from systemu.interface.command.inbox import resolve_gate
            result = resolve_gate(resolved, vault=vault)
            console.print(result.to_rich())
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "[decisions] resolve_gate execution failed for %s", decision_id)


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.0 (Layer 1): user profile + facts
# ─────────────────────────────────────────────────────────────────────────────

@click.group("user")
def user_group():
    """Manage your persistent user profile (name, location, timezone, output dir)
    and the freeform fact log systemu uses to personalize tasks."""


def _profile_defaults() -> dict:
    """The values the wizard offers — also what headless mode falls back to.

    ONE source for both paths (F3): a headless install must not silently get a
    different profile shape from a TTY install.
    """
    import getpass
    try:
        default_name = getpass.getuser()
    except Exception:
        default_name = "operator"
    try:
        from time import tzname
        default_tz = tzname[0] or "UTC"
    except Exception:
        default_tz = "UTC"
    return {
        "name": default_name,
        "location_text": "",
        "timezone": default_tz,
        "default_output_dir": str(Path.home() / "systemu-output"),
    }


_HEADLESS_INIT_HINT = (
    "`user init` needs a terminal to ask its four questions, and this process "
    "has no usable stdin (Docker / CI / a service).\n"
    "Run it non-interactively instead — no TTY required:\n"
    "  systemu user init --non-interactive "
    "[--name NAME] [--location TEXT] [--timezone IANA] [--output-dir PATH]\n"
    "Anything you omit takes the same default the wizard would have offered. "
    "`systemu user set <field> <value>` also creates the profile."
)


@user_group.command("init")
@click.option("--name", default=None, help="Your name (skips that question).")
@click.option("--location", default=None,
              help="Where you are, e.g. 'Springfield, USA' (skips that question).")
@click.option("--timezone", "timezone_", default=None,
              help="IANA timezone, e.g. 'Asia/Kolkata' (skips that question).")
@click.option("--output-dir", default=None,
              help="Default output directory (skips that question).")
@click.option("--non-interactive", "-y", is_flag=True, default=False,
              help="Never prompt: take the defaults for anything not passed. "
                   "The headless (Docker/CI/no-TTY) path.")
@click.pass_context
def user_init(ctx, name, location, timezone_, output_dir, non_interactive):
    """First-run wizard: capture name, location, timezone, output dir.

    With a terminal this is the same four-question wizard it has always been.
    With ``--non-interactive`` (or any of the value flags, or
    SYSTEMU_NON_INTERACTIVE=true / SYSTEMU_HEADLESS=1) it never prompts, so a
    headless box can complete setup from argv alone.
    """
    from systemu.core.models import UserProfile
    from systemu.interface.notifications import headless_declared
    _cfg, vault = _get_vault_and_config(ctx)
    existing = vault.get_user_profile()
    if existing is not None:
        click.echo("A user profile already exists. Use `systemu user show` to view "
                   "or `systemu user set <field> <value>` to update.")
        return

    d = _profile_defaults()
    given = {"name": name, "location_text": location,
             "timezone": timezone_, "default_output_dir": output_dir}
    # Non-interactive when the operator asked for it, supplied any value, or
    # declared this run non-interactive via the documented env switches.
    quiet = non_interactive or headless_declared() or any(
        v is not None for v in given.values())

    if quiet:
        values = {k: (v if v is not None else d[k]) for k, v in given.items()}
    else:
        # Byte-identical wizard for a promptable operator. A closed/EOF stdin
        # used to surface as a bare "Aborted!" with no way forward — F3.
        try:
            values = {
                "name": click.prompt("Your name", default=d["name"]),
                "location_text": click.prompt("Where are you? (e.g. 'Springfield, USA')"),
                "timezone": click.prompt("Your timezone (IANA, e.g. 'Asia/Kolkata')",
                                         default=d["timezone"]),
                "default_output_dir": click.prompt("Default output directory",
                                                   default=d["default_output_dir"]),
            }
        except (click.Abort, EOFError):
            click.echo("")
            raise click.ClickException(_HEADLESS_INIT_HINT)

    prof = UserProfile(**values)
    vault.save_user_profile(prof)
    click.echo(f"✓ Profile saved to {Path(vault.root) / 'user_profile.json'}")
    click.echo("\nNext: `systemu chat submit \"...\"` — systemu now knows you.")


@user_group.command("show")
@click.pass_context
def user_show(ctx):
    """Display the current profile + a summary of facts."""
    _cfg, vault = _get_vault_and_config(ctx)
    prof = vault.get_user_profile()
    if prof is None:
        click.echo("No profile set. Run `systemu user init` to create one.")
        return
    click.echo("─ User profile ───────────────────────────")
    click.echo(f"  name:              {prof.name}")
    click.echo(f"  location_text:     {prof.location_text}")
    click.echo(f"  timezone:          {prof.timezone}")
    click.echo(f"  default_output_dir: {prof.default_output_dir}")
    facts = vault.load_user_facts()
    click.echo(f"\n─ Facts ({len(facts)} active) ─────────────")
    for f in facts[-5:]:
        click.echo(f"  [{f.id}] ({f.source}) {f.fact}")
    if len(facts) > 5:
        click.echo(f"  ... ({len(facts) - 5} more — `systemu user facts list` for all)")


@user_group.command("set")
@click.argument("field", type=click.Choice(["name", "location_text", "timezone",
                                            "default_output_dir"]))
@click.argument("value")
@click.pass_context
def user_set(ctx, field: str, value: str):
    """Set one field on your user profile.

    FIELD is one of: name, location_text, timezone, default_output_dir.
    VALUE is stored exactly as given.

    If you have no profile yet, this creates one from the same defaults the
    setup wizard offers and applies FIELD to it -- so it works on a machine
    with no terminal to prompt at. Run `systemu user show` to review the
    result.
    """
    # F3/F23 -- the engineering history behind that last paragraph, kept here
    # because a code comment is where it belongs and `--help` is not. This
    # command used to refuse with "No profile set. Run `sharing_on user init`
    # first", which on a box with no TTY was a dead end: `init` could only
    # prompt. The old program name inside that quotation was deliberate --
    # rewriting a quotation makes it a misquotation -- and the whole note then
    # rode out to every operator who typed `user set --help`.
    from systemu.core.models import UserProfile
    _cfg, vault = _get_vault_and_config(ctx)
    prof = vault.get_user_profile()
    if prof is None:
        values = _profile_defaults()
        values[field] = value
        vault.save_user_profile(UserProfile(**values))
        click.echo(f"✓ created a profile (defaults for the other fields — "
                   f"`systemu user show` to review)")
        click.echo(f"✓ {field} = {value}")
        return
    updated = prof.model_copy(update={field: value})
    vault.save_user_profile(updated)
    click.echo(f"✓ {field} = {value}")


@user_group.command("remember")
@click.argument("fact_text")
@click.option("--tag", "-t", multiple=True, help="Tag for this fact (repeatable).")
@click.pass_context
def user_remember(ctx, fact_text: str, tag):
    """Add an explicit fact about you to the freeform fact log."""
    _cfg, vault = _get_vault_and_config(ctx)
    f = vault.append_user_fact(fact=fact_text, source="explicit_user",
                                tags=list(tag), source_ref="cli:user_remember")
    click.echo(f"✓ remembered: [{f.id}] {f.fact}")


@user_group.group("facts")
def user_facts_group():
    """Inspect the freeform fact log."""


@user_facts_group.command("list")
@click.option("--tag", "-t", multiple=True, help="Filter by tag (repeatable).")
@click.option("--recent", "-n", type=int, default=None,
              help="Show only the most recent N facts.")
@click.option("--include-superseded", is_flag=True, default=False,
              help="Include facts that were forgotten or replaced.")
@click.pass_context
def user_facts_list(ctx, tag, recent, include_superseded):
    """List facts, newest-last."""
    _cfg, vault = _get_vault_and_config(ctx)
    facts = vault.load_user_facts(tags=list(tag) or None, recent=recent,
                                  include_superseded=include_superseded)
    if not facts:
        click.echo("(no facts)")
        return
    for f in facts:
        marker = " [SUPERSEDED]" if f.superseded_by else ""
        tags_s = f" #{' #'.join(f.tags)}" if f.tags else ""
        click.echo(f"  [{f.id}] ({f.source}, conf={f.confidence:.2f}){tags_s}{marker}\n    {f.fact}")


@user_group.command("forget")
@click.argument("fact_id")
@click.pass_context
def user_forget(ctx, fact_id: str):
    """Mark a fact as superseded (the fact stays in the audit log)."""
    _cfg, vault = _get_vault_and_config(ctx)
    ok = vault.load_user_facts(include_superseded=True)
    if not any(f.id == fact_id for f in ok):
        click.echo(f"No fact with id {fact_id!r}.")
        ctx.exit(1)
    from systemu.runtime.user_profile import forget_fact
    forget_fact(vault, fact_id, reason="forgotten")
    click.echo(f"✓ forgot {fact_id}")


@user_group.command("wipe")
@click.option("--confirm", is_flag=True, default=False,
              help="Required. Without this, the command refuses.")
@click.pass_context
def user_wipe(ctx, confirm: bool):
    """Delete the profile + all facts. Irreversible."""
    if not confirm:
        click.echo("Refusing to wipe without --confirm.")
        ctx.exit(1)
    _cfg, vault = _get_vault_and_config(ctx)
    from systemu.runtime.user_profile import wipe
    wipe(vault)
    click.echo("✓ user profile and facts wiped")


# ─────────────────────────────────────────────────────────────────────────────
# F3: onboarding from argv — no TTY, no browser
#
# The dashboard is gated on `first_run.setup_status`. Every one of those gates
# used to be reachable only through a terminal prompt or a click in the web UI,
# which deadlocked Docker / CI / any headless server: you could not set a
# profile without `user init`, could not run `user init` without a TTY, could
# not finish the tour without the dashboard, and could not reach the dashboard
# without finishing onboarding. This group is the argv-only way out, and
# `status` prints the declared remedy for every gate that is still unmet.
#
# Output is deliberately ASCII: this is verdict-carrying text an operator reads
# over ssh / in a container log, where the console is often cp1252.
# ─────────────────────────────────────────────────────────────────────────────

@click.group("onboarding")
def onboarding_group():
    """Complete and inspect first-run setup without a terminal or a browser."""


@onboarding_group.command("status")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output for scripts and container probes.")
@click.pass_context
def onboarding_status(ctx, as_json: bool):
    """Print every setup gate. Exits non-zero until the install is ready."""
    import json as _json
    from systemu.runtime.first_run import setup_status
    cfg, vault = _get_vault_and_config(ctx)
    checks = setup_status(cfg, vault)
    ready = all(c["ok"] for c in checks if c["required"])

    if as_json:
        click.echo(_json.dumps({"ready": ready, "checks": checks}, indent=2,
                               default=str))
    else:
        click.echo("- systemu onboarding ----------------------------------")
        for c in checks:
            mark = "[ok]" if c["ok"] else ("[--]" if c["required"] else "[..]")
            req = "" if c["required"] else "  (optional)"
            click.echo(f"  {mark} {c['label']}{req}")
            if c["detail"]:
                click.echo(f"         {c['detail']}")
            if not c["ok"]:
                hint = (c.get("headless") or {}).get("hint")
                if hint:
                    click.echo(f"         headless fix: {hint}")
        click.echo("")
        click.echo("READY" if ready else
                   "NOT READY - the [--] gates above still block the dashboard.")
    ctx.exit(0 if ready else 1)


@onboarding_group.command("complete-tour")
@click.pass_context
def onboarding_complete_tour(ctx):
    """Satisfy the guided-tour gate without a browser (idempotent).

    The tour is a browser affordance; a headless install has no browser to run
    it in, so recording that it does not apply is the honest resolution. The
    recorded fact says exactly that rather than claiming it was watched.
    """
    from systemu.interface.tour import mark_tour_completed
    from systemu.runtime.first_run import tour_completed
    _cfg, vault = _get_vault_and_config(ctx)
    if tour_completed(vault):
        click.echo("Guided tour already recorded as finished - nothing to do.")
        return
    mark_tour_completed(
        vault,
        note="guided tour waived from the CLI (headless install - no browser)")
    click.echo("OK: guided tour gate satisfied (waived, headless).")


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.2: session episodic-memory CLI
# ─────────────────────────────────────────────────────────────────────────────

@click.group(name="session")
def session_cli():
    """Inspect the freeform episodic-memory log (cross-session recall)."""
    pass


@session_cli.command("list")
@click.option("--limit", default=20, show_default=True)
def session_list(limit):
    """List recent sessions from the episodic-memory log."""
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    summaries = v.query_session_summaries(limit=limit)
    if not summaries:
        click.echo("(no sessions yet)")
        return
    for s in summaries:
        click.echo(f"  {s.completed_at.strftime('%Y-%m-%d %H:%M')} "
                   f"[{s.status:7}] {s.session_id}: {s.intent[:60]}")


@session_cli.command("show")
@click.argument("session_id")
def session_show(session_id):
    """Show full detail of an episodic-memory session record."""
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from systemu.runtime.tools.session_tools import session_recall
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    result = session_recall(vault=v, session_id=session_id)
    if result is None:
        click.echo(f"No session found with id={session_id!r}")
        return
    click.echo(f"session_id:   {result['session_id']}")
    click.echo(f"status:       {result['status']}")
    click.echo(f"started_at:   {result['started_at']}")
    click.echo(f"completed_at: {result['completed_at']}")
    click.echo(f"intent:       {result['intent']}")
    click.echo(f"outcome:      {result['outcome_summary']}")
    if result['tags']:
        click.echo(f"tags:         {', '.join(result['tags'])}")
    if result['key_facts_learned']:
        click.echo("facts learned:")
        for f in result['key_facts_learned']:
            click.echo(f"  - {f}")
    if result['files_produced']:
        click.echo("files produced:")
        for f in result['files_produced']:
            click.echo(f"  - {f}")


@session_cli.command("search")
@click.argument("query")
@click.option("--limit", default=5, show_default=True)
def session_search_cmd(query, limit):
    """Search the episodic-memory log by keyword."""
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from systemu.runtime.tools.session_tools import session_search
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    results = session_search(vault=v, query=query, limit=limit)
    if not results:
        click.echo(f"No sessions match query {query!r}")
        return
    for r in results:
        click.echo(f"  [{r['status']:7}] {r['session_id']}: {r['intent'][:60]}")
        click.echo(f"    outcome: {r['outcome_summary'][:80]}")


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.3: capability ledger CLI
# ─────────────────────────────────────────────────────────────────────────────

@click.group(name="capability")
def capability_cli():
    """Inspect the capability ledger (what systemu knows it can do)."""
    pass


@capability_cli.command("list")
@click.option("--kind", default=None, help="Filter by 'tool' or 'skill'")
def capability_list(kind):
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from systemu.runtime.tools.capability_tools import capability_list_my_capabilities
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    results = capability_list_my_capabilities(vault=v, kind=kind)
    if not results:
        click.echo("(no capabilities registered yet)")
        return
    for c in results:
        last = c.get("last_used_at") or "(never)"
        click.echo(f"  [{c['kind']:5}] {c['name']:30}  invocations={c['invocations']:5}  last_used={last}")


@capability_cli.command("show")
@click.argument("name")
def capability_show(name):
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from systemu.runtime.tools.capability_tools import capability_get_stats
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    stats = capability_get_stats(vault=v, name=name)
    if stats is None:
        click.echo(f"No capability found with name={name!r}")
        return
    click.echo(f"name:         {stats['name']}")
    click.echo(f"kind:         {stats['kind']}")
    click.echo(f"invocations:  {stats['invocations']}")
    click.echo(f"successes:    {stats['successes']}")
    click.echo(f"failures:     {stats['failures']}")
    click.echo(f"success_rate: {stats['success_rate']:.1%}")
    click.echo(f"last_used_at: {stats['last_used_at'] or '(never)'}")
    if stats['last_error']:
        click.echo(f"last_error:   {stats['last_error']}")


@capability_cli.command("stats")
def capability_stats():
    """Aggregate stats across all registered capabilities."""
    from sharing_on.config import Config
    from systemu.vault.vault import Vault
    from systemu.runtime import capability_ledger as cl
    from pathlib import Path
    cfg = Config.from_env()
    v = Vault(root=Path(cfg.vault_dir))
    caps = cl.list_capabilities(v)
    if not caps:
        click.echo("(no capabilities registered yet)")
        return
    total_inv = sum(c.invocations for c in caps)
    total_succ = sum(c.successes for c in caps)
    total_fail = sum(c.failures for c in caps)
    by_kind = {}
    for c in caps:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    click.echo(f"capabilities:    {len(caps)}")
    for k, n in sorted(by_kind.items()):
        click.echo(f"  {k:8} {n}")
    click.echo(f"invocations:     {total_inv}")
    click.echo(f"successes:       {total_succ}")
    click.echo(f"failures:        {total_fail}")
    if total_inv:
        click.echo(f"overall_rate:    {total_succ / total_inv:.1%}")


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.4: skill recipe CLI — browse bundled + user-installed SKILL.md recipes
# ─────────────────────────────────────────────────────────────────────────────

@click.group(name="skill")
def skill_cli():
    """Inspect bundled + user-installed SKILL.md recipes."""
    pass


@skill_cli.command("list")
def skill_list():
    """List all loadable skills with their description and required toolsets."""
    from sharing_on.config import Config
    from systemu.runtime.tools.skill_tools import skill_list_skills
    cfg = Config.from_env()
    skills = skill_list_skills(config=cfg)
    if not skills:
        click.echo("(no skills found — check SYSTEMU_SKILLS_BUNDLED_DIR / SYSTEMU_SKILLS_USER_DIR)")
        return
    for s in skills:
        toolsets = ", ".join(s.get("requires_toolsets") or []) or "—"
        click.echo(f"  [{s['version']:6}] {s['name']:30}  toolsets={toolsets}")
        if s.get("description"):
            click.echo(f"     {s['description'][:80]}")


@skill_cli.command("view")
@click.argument("name")
def skill_view(name):
    """Show the full SKILL.md body + metadata for one named skill."""
    from sharing_on.config import Config
    from systemu.runtime.tools.skill_tools import skill_view_skill
    cfg = Config.from_env()
    result = skill_view_skill(name=name, config=cfg)
    if result is None:
        click.echo(f"No skill found with name={name!r}")
        return
    click.echo(f"name:        {result['name']}")
    click.echo(f"description: {result['description']}")
    click.echo(f"version:     {result['version']}")
    if result.get("tags"):
        click.echo(f"tags:        {', '.join(result['tags'])}")
    if result.get("requires_toolsets"):
        click.echo(f"toolsets:    {', '.join(result['requires_toolsets'])}")
    if result.get("prerequisites_commands"):
        click.echo(f"prereqs:     {', '.join(result['prerequisites_commands'])}")
    if result.get("related_skills"):
        click.echo(f"related:     {', '.join(result['related_skills'])}")
    if result.get("source_path"):
        click.echo(f"source:      {result['source_path']}")
    click.echo("")
    click.echo("=" * 60)
    click.echo(result.get("body", ""))


def run_find_tools(vault, query: str, limit: int = 15) -> int:
    """R-CAP1 · CAP-4c — print the deterministic capability search for ``query``.

    Uses ``capability_index.find_tools(live=True)`` (derives in memory — NEVER
    writes the index, so the daemon reconciler stays its sole writer, CAP-0.1).
    Ranks the COMPLETE store, so every tool is listed (never-subtract). No LLM,
    no harness-request budget. Returns a process exit code (always 0 — a search
    with no matches is a valid, non-error result)."""
    from systemu.runtime import capability_index as _ci
    q = (query or "").strip()
    rows = _ci.find_tools(vault, q, limit=(None if not limit else int(limit)), live=True)
    if not rows:
        click.echo("No tools on your table yet. Forge or connect one and it'll appear here.")
        return 0
    click.echo(f"Tools for {q!r} — ranked, {len(rows)} shown:")
    for r in rows:
        slot = ", ".join(r.get("slots") or []) or "-"
        click.echo(f"  {str(r.get('name', '')):<28} [{slot}]  ({r.get('origin', '')})")
        # F21: a tool whose optional dependency is absent is LISTED (never
        # subtract) but never listed as if it would run. The remedy is printed
        # on the row, not left for the operator to find in `doctor`.
        if not r.get("available", True):
            click.echo(f"      {r.get('unavailable_reason', '')}")
    return 0


# ── R-P3b · spend caps (view / set / clear) ──────────────────────────────────

def _fmt_money(m) -> str:
    if m is None:
        return "(no cap)"
    from systemu.runtime import costing
    return f"{costing.currency_symbol(m.currency)}{m.amount}"


def run_spend_caps_show(*, data_dir=None) -> int:
    """R-P3b — print the configured per-task / per-day spend caps + today's spend.

    Caps are OFF by default. A run that REACHES its cap halts honestly (no silent
    overrun) — raise the cap and re-run to continue. Read-only; exit code 0."""
    from systemu.runtime import costing, spend_caps
    caps = spend_caps.load_caps(data_dir=data_dir)
    day = costing.daily_total()
    click.echo("Spend caps (a run that reaches its cap halts; raise + re-run to continue):")
    click.echo(f"  per-task: {_fmt_money(caps.get('task'))}")
    click.echo(f"  per-day:  {_fmt_money(caps.get('day'))}")
    if getattr(day, "total_known", False) and day.total is not None:
        click.echo(f"  today so far: {_fmt_money(day.total)}")
    else:
        click.echo("  today so far: (unknown — some models are unpriced)")
    return 0


def run_spend_caps_set(kind: str, amount, *, data_dir=None) -> int:
    """Set the per-``kind`` ('task'|'day') cap to ``amount`` (in the default currency)."""
    from systemu.runtime import spend_caps
    try:
        spend_caps.set_cap(kind, amount, data_dir=data_dir)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        return 2
    click.echo(f"Set {kind} spend cap to {amount}.")
    return 0


def run_spend_caps_clear(kind: str, *, data_dir=None) -> int:
    """Remove the per-``kind`` ('task'|'day') cap."""
    from systemu.runtime import spend_caps
    try:
        spend_caps.clear_cap(kind, data_dir=data_dir)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        return 2
    click.echo(f"Cleared {kind} spend cap.")
    return 0


def run_world(vault, query: str = "", limit: int = 30) -> int:
    """R-W1 (W-A slice-1) — show what the world model believes (§5.11.a/.b).

    READ-ONLY: reads the ``FactStore`` and never writes it (sole-writer discipline,
    E5). With no ``query`` it summarises the store — facts grouped by kind + active
    (unexpired) negative facts. With a ``query`` it runs ``world.query.about`` — the
    never-subtract escape hatch. The store is empty until slice-2 populates it from
    the live inventory, so today this is the operator's read surface + the API proof.
    Always returns 0 (an empty world is a valid, non-error result)."""
    from systemu.runtime import world_model as _wm
    store = _wm.FactStore(vault)
    survey = store.latest_survey()
    #: how a derived staleness reads to an operator. "not surveyed" is deliberately NOT
    #: called stale — the last survey did not cover that scope, so absence is not evidence.
    _MARK = {"confirmed": "", "unconfirmed": "  ⚠ not re-confirmed by the last survey",
             "not_surveyed": "  (scope not surveyed)", "unknown": ""}
    # R-W2 (WM-7 / M3): the STANDING-SCAN surface. Consenting to a census category is an
    # ONGOING permission, not a one-off snapshot, so the operator has to be able to see
    # what is still being watched and when it last ran — a standing scan you cannot see
    # is not meaningfully revocable.
    #
    # Rendered BEFORE the `if q:` branch, not after it. After it, the whole disclosure
    # sat behind that branch's `return 0`, so `sharing_on world <query>` — the form an
    # operator uses most — silently never showed what was being watched. It is also
    # before the empty-store early-return, because "granted but has found nothing yet"
    # is exactly the state most worth showing and the state that return would hide.
    #
    # Renders nothing on a FRESH install: no grant exists until the operator creates one
    # with `systemu census grant`, so census_status is []. It is reachable now — this is
    # the "you are already being watched" reminder on a command the operator runs for
    # other reasons, which is why it stays here rather than living only under
    # `census status` (a standing permission you have to go looking for is not one the
    # operator can be said to be aware of).
    try:
        from systemu.runtime.ambient_census import census_status
        _grants = census_status(vault)
    except Exception:
        _grants = []
    if _grants:
        click.echo(f"Ambient census — STANDING permission to scan this machine, "
                   f"{len(_grants)} categor(y/ies):")
        for g in _grants:
            click.echo(f"  {g['category']:<18} "
                       f"{'PAUSED ' if g['paused'] else 'active '}"
                       f" last ran: {g['last_ran_at'] or 'never'}")
        # Says what is TRUE, not what would be reassuring. An earlier wording — "these
        # re-run until revoked" — offered a control that did not exist in that build,
        # which is the shape R-W2 was held for. P4-B2 shipped the controls, so the line
        # NAMES them: an operator reading "revoking deletes the facts" must be able to
        # act on it in the same breath, or it is still a promise rather than a control.
        click.echo("  These re-run on later runs. What they find is included in the "
                   "planning prompt sent to systemu's model provider.")
        click.echo("  Revoking a category also DELETES the facts it produced:")
        click.echo("    systemu census revoke <category>   (or `pause` to stop scanning "
                   "but keep what it found)")
        click.echo("    systemu census status              (what is watched, and what "
                   "this build can grant)")

    q = (query or "").strip()
    if q:
        hits = _wm.about(store, q, limit=(None if not limit else int(limit)))
        # R-W1 slice-2c: cite any unexpired "searched and did not find" note for this
        # query FIRST (§5.11 AC2 — a precise "I looked, here is what I probed and when"
        # beats both silence and a bare "nothing known"). Read-only; never raises.
        try:
            from systemu.runtime import world_model_discovery as _wmd
            _miss = _wmd.recent_discovery_miss(vault, q)
        except Exception:
            _miss = None
        if _miss is not None:
            click.echo(f"Searched for {q!r} on {_miss.recorded_at} and found nothing.")
            click.echo(f"  probed: {', '.join(_miss.probes) or '-'}")
        if not hits:
            if _miss is None:
                click.echo(f"Nothing known about {q!r} yet.")
            return 0
        click.echo(f"About {q!r} — {len(hits)} fact(s):")
        for f in hits:
            mark = _MARK.get(_wm.staleness_of(f, survey), "")
            click.echo(f"  [{f.kind}] {f.value}  (origin={f.origin_class}, conf={f.confidence:.2f}){mark}")
        return 0
    facts = store.all_facts()
    negatives = [n for n in store.all_negatives() if not n.is_expired()]
    if not facts and not negatives:
        click.echo("The world model is empty. It fills as systemu surveys your setup and works.")
        return 0
    by_kind = {}
    for f in facts:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    click.echo(f"World model — {len(facts)} fact(s) across {len(by_kind)} kind(s):")
    for kind in sorted(by_kind):
        click.echo(f"  {kind:<16} {by_kind[kind]}")
    if survey is not None:
        unconfirmed = [f for f in facts if _wm.staleness_of(f, survey) == "unconfirmed"]
        click.echo(f"Last survey {survey.at} covered: "
                   f"{', '.join(survey.kinds_surveyed) or '(nothing)'}"
                   f"{' [file coverage truncated]' if survey.data_location_cap_hit else ''}")
        if unconfirmed:
            click.echo(f"Not re-confirmed by it — {len(unconfirmed)} (may be gone):")
            for f in unconfirmed[:10]:
                click.echo(f"  [{f.kind}] {f.value}")
    if negatives:
        click.echo(f"Active 'searched, not found' notes — {len(negatives)}:")
        for n in negatives:
            click.echo(f"  {n.scope}  (probed: {', '.join(n.probes) or '-'})")
    return 0


# ===========================================================================
# R-W2 CENSUS CONSENT SURFACE :: REGION START
# ===========================================================================
# The WM-7 ambient census (spec 5.11.c) reads the OPERATOR'S OWN MACHINE, which is a
# privacy boundary nothing else in the inventory crosses. Everything below is the
# operator's consent surface for it: `systemu census status|grant|revoke|pause|resume`.
#
# THIS IS THE ONLY PLACE IN systemu/ THAT MAY CREATE OR WITHDRAW A CENSUS GRANT.
# `tests/test_rw2_ambient_census.py::test_the_census_grant_surface_is_confined_to_its_
# declared_region` scans the shipped tree for the census consent symbols and fails on any
# reference outside this region (and outside the matching region in sharing_on/cli.py).
# That guard is the reason the whole surface can be read in one sitting; its failure
# message lists what a widening must re-audit first. Do not scatter census calls above
# this line, and do not exempt a new file without doing that work.
#
# SCOPE (P4-B2): ONE category ships end to end -- `cloud_sync_roots`, the lowest privacy
# surface of the three. `census_consent.SURFACED_CATEGORIES` is the single source of
# truth; the consent card derives its `revocation_surface_shipped` flag and its
# revocation prose from the same constant, so this surface cannot claim a control it does
# not offer, nor deny one it does. All four mutating verbs are restricted to that set:
# there is nothing legitimate to revoke for a category no surface can grant, because the
# consent file is authenticated and a planted one grants nothing.

#: Exit codes, so a script can tell the three "did not grant" outcomes apart. A single
#: non-zero would make "the operator said no" indistinguishable from "this build cannot
#: ask you" -- and a wrapper that retries on the second must not retry on the first.
#: ALIASES of the shared consent outcomes (D12), not a second numbering. This
#: surface's rule is now the rule BOTH standing-permission commands obey, so the
#: three codes are declared once and named here for the scripts already reading
#: them.
CENSUS_EXIT_OK = consent_prompt.CONSENT_GRANTED
CENSUS_EXIT_DECLINED = consent_prompt.CONSENT_DECLINED
CENSUS_EXIT_NO_TERMINAL = consent_prompt.CONSENT_NO_TERMINAL
CENSUS_EXIT_BAD_CATEGORY = 3      # unknown, or not grantable from this build


def _census_stdin_is_a_terminal() -> bool:
    """Whether there is a human to ask -- a DELEGATE to the shared predicate.

    D12: the definition moved to ``interface/consent_prompt.stdin_is_a_terminal``
    when `roots grant` came onto this same rule. Two copies of "can I prompt" is
    how the two surfaces came to answer a pipe differently in the first place.

    The name is kept because it is this surface's injection seam and the tests
    that drive both sides of it patch here. It must stay a DELEGATE: an answer of
    its own would re-create the split it now exists to close.
    """
    return consent_prompt.stdin_is_a_terminal()


def _echo_consent_card(card: dict) -> None:
    """Render the FULL consent card the operator is being asked to agree to.

    Renders the card's OWN text, field by field, rather than a summary written here. A
    renderer that prints only `collects`/`excludes` drops the two disclosures that matter
    most -- that this is a STANDING permission, and that what it finds is sent to the
    model provider -- and those were false in this file's history in the permissive
    direction. Pinned by
    test_the_grant_command_shows_the_real_card_including_the_transmission_notice, which
    asserts every field of the real card appears in this output.
    """
    click.echo("")
    click.echo(f"  {card['title']}  ({card['category']})")
    click.echo("  " + "-" * 68)
    click.echo("  WHAT IT COLLECTS:")
    for item in card["collects"]:
        click.echo(f"    - {item}")
    click.echo("  WHAT IT DOES NOT TOUCH:")
    for item in card["excludes"]:
        click.echo(f"    - {item}")
    click.echo(f"  HOW:   {card['how']}")
    click.echo(f"  WHY:   {card['why']}")
    if card.get("sensitivity_notice"):
        click.echo(f"  NOTE:  {card['sensitivity_notice']}")
    click.echo(f"  STORED AT: {card['stored_at']}")
    click.echo(f"  LEAVES THIS MACHINE: {'YES' if card['leaves_this_machine'] else 'no'}")
    click.echo(f"    {card['transmission_notice']}")
    click.echo(f"  STANDING PERMISSION: {'YES' if card['standing_scan'] else 'no'}")
    click.echo(f"    {card['standing_scan_notice']}")
    click.echo(f"  REVOKING: {card['revocation_notice']}")
    click.echo("")


def _census_category_or_error(category: str, *, must_be_grantable: bool):
    """``(category, None)`` if usable here, else ``(None, exit_code)`` after saying why.

    REFUSES rather than no-ops, in both directions. A typo'd category that "succeeded"
    would read to the operator as a granted capability that is quietly dead; a
    not-yet-shipped category that silently did nothing would be the same lie with better
    manners.
    """
    from systemu.runtime.census_consent import CATEGORIES, SURFACED_CATEGORIES
    cat = str(category or "").strip()
    if cat not in CATEGORIES:
        click.echo(f"Unknown census category: {cat!r}")
        click.echo(f"  known categories: {', '.join(sorted(CATEGORIES))}")
        return None, CENSUS_EXIT_BAD_CATEGORY
    if must_be_grantable and cat not in SURFACED_CATEGORIES:
        click.echo(f"'{cat}' is not yet grantable from this build.")
        click.echo(f"  This build ships the full consent surface for: "
                   f"{', '.join(sorted(SURFACED_CATEGORIES))}.")
        click.echo("  The machinery for the others exists but has no operator controls "
                   "yet, and systemu will not take a standing permission it cannot "
                   "offer you a way to withdraw.")
        return None, CENSUS_EXIT_BAD_CATEGORY
    return cat, None


def run_census_status(vault) -> int:
    """`systemu census status` -- the M3 "see" half, over EVERY category.

    Lists all three declared categories, not only the granted ones: showing only grants
    would make an unconsented install look like the feature does not exist, and the
    operator cannot decide about something they cannot see. Read-only -- it never creates
    consent state, and on a fresh install it writes nothing at all.
    """
    from systemu.runtime.ambient_census import census_status
    from systemu.runtime.census_consent import CATEGORIES, SURFACED_CATEGORIES
    try:
        granted = {row["category"]: row for row in census_status(vault)}
    except Exception:
        granted = {}
    click.echo("Ambient census -- what systemu may look at on this machine "
               "(nothing, until you say so):")
    for cat in sorted(CATEGORIES):
        row = granted.get(cat)
        if row is None:
            state = "not granted"
        elif row["paused"]:
            state = f"GRANTED but PAUSED  (last ran: {row['last_ran_at'] or 'never'})"
        else:
            state = (f"GRANTED, active     (granted: {row['granted_at'] or '?'}, "
                     f"last ran: {row['last_ran_at'] or 'never'})")
        click.echo(f"  {cat:<18} {CATEGORIES[cat]['title']}")
        click.echo(f"  {'':<18}   {state}"
                   + ("" if cat in SURFACED_CATEGORIES
                      else "  [not yet grantable from this build]"))
    click.echo("")
    click.echo("A grant is a STANDING permission: the category is re-checked on later "
               "runs until you revoke it,")
    click.echo("and what it finds is included in the planning prompt systemu sends to "
               "its model provider.")
    click.echo(f"  systemu census grant <category>     "
               f"(available: {', '.join(sorted(SURFACED_CATEGORIES))})")
    # Spelled out rather than `pause|resume`: test_f6_command_names_are_invocable checks
    # every command string in shipped source against the real click tree, and a pipe
    # shorthand names no subcommand an operator can actually type.
    click.echo("  systemu census pause <category>    (stop scanning, keep what it found)")
    click.echo("  systemu census resume <category>")
    click.echo("  systemu census revoke <category>    (also DELETES the facts it "
               "produced)")
    return CENSUS_EXIT_OK


def run_census_grant(vault, category: str, assume_yes: bool = False) -> int:
    """`systemu census grant <category>` -- show the real card, then ask.

    THE CARD IS ALWAYS PRINTED, including under ``--yes``. ``--yes`` means "I have read
    this and I agree" for a script or a headless box; it does not mean "do not tell me".
    Suppressing the disclosure for the non-interactive path would make the transmission
    notice conditional on having a terminal, which is not a property consent should have.

    Default N. With no terminal and no ``--yes`` this REFUSES and names ``--yes``: a
    prompt into a closed stdin surfaces as a bare abort with no way forward (the F3 shape
    this CLI has been bitten by before), and defaulting to yes in a headless context would
    take a standing permission nobody granted.
    """
    from systemu.runtime.ambient_census import grant_category
    from systemu.runtime.census_consent import consent_card
    cat, err = _census_category_or_error(category, must_be_grantable=True)
    if err is not None:
        return err

    card = consent_card(cat)
    click.echo("systemu is asking to look at ONE thing on this machine, from now on.")
    _echo_consent_card(card)

    # D12: the DECISION comes from the shared helper -- the same one `roots grant`
    # now asks, so a pipe means the same thing on both surfaces. The COPY stays
    # here, where it can name the category and what a scan transmits.
    # `_census_stdin_is_a_terminal` is passed explicitly because it is this
    # surface's long-standing injection seam; it delegates to the shared
    # predicate, so patching either one steers this path.
    answer = consent_prompt.ask_for_consent(
        f"Grant this standing permission for '{cat}'?",
        assume_yes=assume_yes, is_a_terminal=_census_stdin_is_a_terminal)
    if answer == CENSUS_EXIT_NO_TERMINAL:
        click.echo("Refusing to record consent: there is no terminal to ask on "
                   "(Docker / CI / a service).")
        click.echo(f"  Re-run with --yes once you have read the above: "
                   f"systemu census grant {cat} --yes")
        click.echo("  Nothing was recorded and nothing will be scanned.")
        return CENSUS_EXIT_NO_TERMINAL
    if answer != CENSUS_EXIT_OK:
        click.echo("Not granted. Nothing was recorded and nothing will be scanned.")
        return CENSUS_EXIT_DECLINED

    try:
        grant_category(vault, cat)
    except Exception as exc:
        # The store REFUSES to write a consent file it cannot sign, so a failure here
        # means no grant exists -- say so rather than leaving the operator believing a
        # "yes" was recorded.
        click.echo(f"Could not record consent for '{cat}': {exc}")
        click.echo("  Nothing was recorded and nothing will be scanned.")
        return CENSUS_EXIT_BAD_CATEGORY
    click.echo(f"Granted: '{cat}'. It will be scanned on the next run, and re-checked on "
               f"later runs.")
    click.echo(f"  Stop it any time:  systemu census revoke {cat}   "
               f"(this also deletes what it found)")
    return CENSUS_EXIT_OK


def run_census_revoke(vault, category: str) -> int:
    """`systemu census revoke <category>` -- withdraw consent AND delete the facts.

    Calls ``ambient_census.revoke_category``, the one entry point that does BOTH halves:
    withdrawing consent alone would leave the store asserting what the operator just
    withdrew, and purging alone would leave a scanner that re-populates it on the next
    run. No confirmation prompt -- revoke is the safe direction, and a control you have
    to argue with is one operators stop reaching for.
    """
    from systemu.runtime.ambient_census import revoke_category
    cat, err = _census_category_or_error(category, must_be_grantable=True)
    if err is not None:
        return err
    out = revoke_category(vault, cat)
    if out.get("revoked"):
        click.echo(f"Revoked: '{cat}'. It will not be scanned again.")
    else:
        click.echo(f"'{cat}' was not granted; nothing to withdraw.")
    removed = int(out.get("facts_removed") or 0)
    detached = int(out.get("facts_detached") or 0)
    click.echo(f"  Facts deleted: {removed}"
               + (f" (and {detached} kept because another source also asserts them, with "
                  f"the census evidence removed)" if detached else ""))
    return CENSUS_EXIT_OK


def _census_set_paused(vault, category: str, paused: bool) -> int:
    # Through `ambient_census.pause_category`, NOT by constructing a CensusConsentStore
    # here. Obtaining a mutable handle on consent stays confined to that module, which is
    # what keeps the CONC-MAP writer-ownership guard on `CensusConsentStore(` tight: this
    # surface needs the VERB, not the handle.
    from systemu.runtime.ambient_census import pause_category
    cat, err = _census_category_or_error(category, must_be_grantable=True)
    if err is not None:
        return err
    if not pause_category(vault, cat, paused):
        click.echo(f"'{cat}' is not granted, so there is nothing to "
                   f"{'pause' if paused else 'resume'}.")
        click.echo(f"  systemu census grant {cat}")
        return CENSUS_EXIT_BAD_CATEGORY
    if paused:
        click.echo(f"Paused: '{cat}' will not be scanned until you resume it.")
        click.echo(f"  What it already found is KEPT. To delete that too: "
                   f"systemu census revoke {cat}")
    else:
        click.echo(f"Resumed: '{cat}' will be scanned again on the next run.")
    return CENSUS_EXIT_OK


def run_census_pause(vault, category: str) -> int:
    """`systemu census pause <category>` -- stop scanning, KEEP the facts.

    The one difference from revoke, and the reason both exist: a pause is not a
    withdrawal of consent, so purging the facts would make it indistinguishable.
    """
    return _census_set_paused(vault, category, True)


def run_census_resume(vault, category: str) -> int:
    """`systemu census resume <category>` -- undo a pause."""
    return _census_set_paused(vault, category, False)

# R-W2 CENSUS CONSENT SURFACE :: REGION END
