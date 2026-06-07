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
            f"   or:  [bold]sharing_on decisions resolve {pd.decision_id} --choice <option>[/bold]\n"
            f"\n"
            f"   Re-run this command after resolving to pick up the operator's choice."
        )
        ctx.exit(75)  # EX_TEMPFAIL


# ─────────────────────────────────────────────────────────────────────────────
#  scrolls group
# ─────────────────────────────────────────────────────────────────────────────

@click.group("scrolls")
def scrolls_group():
    """Manage Scrolls — refined SOPs extracted from capture sessions."""


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
            instructions = generate_instructions(
                steps=steps,
                session_name=meta.get("name", session_path.name),
                platform_info=meta.get("platform", "Unknown"),
                duration_seconds=0.0,
                api_key=config.openrouter_api_key,
                base_url=config.openrouter_base_url,
                model=config.tier3_model,
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
    """Manage the Shadow Army — autonomous agent personas."""


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

    table = Table(title="🔧 Tool Registry", show_lines=True)
    table.add_column("ID",     style="cyan",   no_wrap=True)
    table.add_column("Name",   style="bold")
    table.add_column("Type",   style="dim")
    table.add_column("Status", style="yellow")
    table.add_column("Description")

    for t in tools:
        table.add_row(
            t["id"], t["name"], t.get("tool_type", "—"),
            t["status"],
            (t.get("description", "") or "")[:60] + "…"
                if len(t.get("description", "")) > 60
                else t.get("description", "—"),
        )
    console.print(table)


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
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    _, vault = _get_vault_and_config(ctx)
    try:
        skill = vault.get_skill(skill_id)
    except Exception as exc:
        console.print(f"[red]× skill {skill_id} not found: {exc}[/red]")
        ctx.exit(1)

    new_score = 1.0 if reactivate else 0.0
    action = "reactivate" if reactivate else "deprecate"

    skill.effectiveness_score = new_score
    if not hasattr(skill, "evolution_history") or skill.evolution_history is None:
        skill.evolution_history = []
    skill.evolution_history.append({
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "action": action,
        "reason": reason,
    })
    vault.save_skill(skill)

    # Audit log — append to data/skill_deprecations.jsonl
    try:
        log_path = Path("data/skill_deprecations.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "skill_id": skill_id,
                "name": getattr(skill, "name", ""),
                "action": action,
                "reason": reason,
                "ts": datetime.now(tz=timezone.utc).isoformat(),
            }) + "\n")
    except Exception:
        pass  # audit is best-effort

    icon = "▲" if reactivate else "▼"
    console.print(
        f"[green]{icon} {action.title()}d {skill_id} "
        f"({getattr(skill, 'name', '?')}) — effectiveness_score={new_score}[/green]"
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

@click.command("settings")
@click.pass_context
def settings_cmd(ctx):
    """Show current Systemu configuration (models, vault dir, etc.)."""
    config, vault = _get_vault_and_config(ctx)

    console.print(Panel(
        f"[bold]LLM Tiers[/bold]\n"
        f"  Tier 1 (deep reasoning):    [cyan]{config.tier1_model}[/cyan]\n"
        f"  Tier 2 (structured/code):   [cyan]{config.tier2_model}[/cyan]\n"
        f"  Tier 3 (fast/formatting):   [cyan]{config.tier3_model}[/cyan]\n\n"
        f"[bold]Behaviour[/bold]\n"
        f"  Non-interactive mode:       [yellow]{config.non_interactive}[/yellow]\n"
        f"  Vault directory:            [dim]{config.vault_dir}[/dim]\n\n"
        f"[bold]OpenRouter[/bold]\n"
        f"  API key set:                 {'[green]Yes[/green]' if config.openrouter_api_key else '[red]No — set OPENROUTER_API_KEY in .env[/red]'}",
        title="⚙️  Systemu Settings",
        border_style="blue",
    ))


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
            "\n[dim]Approve with:[/dim] sharing_on tools deps approve <package>"
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
    retroactively benefit — restart the daemon or re-trigger the activity.
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

    Does not uninstall the package — that's a separate decision.  In-process
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
    the resolved InstallMode — when mode=OFF nothing happens; when
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
@click.option("--creativity",      type=int, default=50, show_default=True, help="Creativity level 0–100.")
@click.option("--professionalism", type=int, default=50, show_default=True, help="Professionalism level 0–100.")
@click.option("--techie",          type=int, default=50, show_default=True, help="Techie depth 0–100.")
@click.option("--thinking",        type=int, default=50, show_default=True, help="Thinking depth 0–100.")
@click.pass_context
def army_awaken(ctx, name: str, activity: Optional[str],
                creativity: int, professionalism: int, techie: int, thinking: int):
    """Manually create (awaken) a new Shadow persona.

    If --activity is provided, the shadow is assigned to that activity.
    Persona dimension sliders (0–100) adjust the shadow's system prompt tone.
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

    Uses the ShadowRuntime ReAct loop: Reason → Tool Call → Observe → repeat.
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
    """Direct chat tasks — skip the capture/record flow."""


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


@daemon_group.command("start")
@click.option("--port", default=8765, show_default=True, help="Port for the web dashboard.")
@click.option("--foreground", is_flag=True, help="Run in foreground (blocking).")
@click.pass_context
def daemon_start(ctx, port: int, foreground: bool):
    """Start the Systemu background daemon."""
    config, vault = _get_vault_and_config(ctx)
    from systemu.scheduler.daemon import start_daemon

    console.print(f"\n[cyan]⚡ Starting Systemu daemon on port {port} ...[/cyan]")
    start_daemon(
        vault_dir=config.vault_dir,
        config=config,
        vault=vault,
        port=port,
        foreground=foreground,
    )
    if not foreground:
        console.print("[green]✓ Daemon started in background.[/green]")
        console.print("  Use [bold]sharing_on daemon status[/bold] to check.")


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
        console.print("[green]✓ Daemon stopped.[/green]")
    else:
        console.print("[yellow]Daemon is not running.[/yellow]")


@daemon_group.command("status")
@click.pass_context
def daemon_status(ctx):
    """Show the Systemu daemon status."""
    config, _ = _get_vault_and_config(ctx)
    from systemu.scheduler.daemon import get_status

    status = get_status(config.vault_dir)
    if status["running"]:
        console.print(Panel(
            f"[green]● Running[/green]  (PID {status['pid']})",
            title="⚡ Systemu Daemon", border_style="green"
        ))
    else:
        console.print(Panel(
            "[dim]○ Not running[/dim]\n"
            "Start with: [bold]sharing_on daemon start[/bold]",
            title="⚡ Systemu Daemon", border_style="dim"
        ))


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
    keyword matches.  Suggests a specialty when one tag has ≥5 hits and
    ≥40% of total matched hits.  Operator applies via Workshop edit
    dialog — this command is inspection-only.
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
    """Manage the OperatorDecisionQueue — operator decisions awaiting resolution.

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
    table.add_column("ID",         style="dim",   no_wrap=True)
    table.add_column("Title",      style="bold")
    table.add_column("Options",    style="cyan")
    table.add_column("Dedup key",  style="dim")
    table.add_column("Created",    style="dim")
    for d in pending:
        ts = d.created_at.isoformat(timespec="seconds") if d.created_at else ""
        table.add_row(
            d.id,
            (d.title or "")[:60],
            ", ".join(d.options),
            d.dedup_key or "",
            ts,
        )
    console.print(table)


@decisions_group.command("resolve")
@click.argument("decision_id")
@click.option("--choice", "-c", required=True, help="One of the decision's options.")
@click.pass_context
def decisions_resolve(ctx, decision_id: str, choice: str):
    """Resolve a pending decision with the operator's chosen option."""
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


# ─────────────────────────────────────────────────────────────────────────────
# v0.9.0 (Layer 1): user profile + facts
# ─────────────────────────────────────────────────────────────────────────────

@click.group("user")
def user_group():
    """Manage your persistent user profile (name, location, timezone, output dir)
    and the freeform fact log systemu uses to personalize tasks."""


@user_group.command("init")
@click.pass_context
def user_init(ctx):
    """First-run wizard: capture name, location, timezone, output dir."""
    import getpass
    from systemu.core.models import UserProfile
    _cfg, vault = _get_vault_and_config(ctx)
    existing = vault.get_user_profile()
    if existing is not None:
        click.echo("A user profile already exists. Use `sharing_on user show` to view "
                   "or `sharing_on user set <field> <value>` to update.")
        return
    default_name = getpass.getuser()
    name = click.prompt("Your name", default=default_name)
    location = click.prompt("Where are you? (e.g. 'Bangalore, India')")
    try:
        from time import tzname
        default_tz = tzname[0] or "UTC"
    except Exception:
        default_tz = "UTC"
    tz = click.prompt("Your timezone (IANA, e.g. 'Asia/Kolkata')", default=default_tz)
    default_out = str(Path.home() / "systemu-output")
    out = click.prompt("Default output directory", default=default_out)
    prof = UserProfile(name=name, location_text=location, timezone=tz,
                       default_output_dir=out)
    vault.save_user_profile(prof)
    click.echo(f"✓ Profile saved to {Path(vault.root) / 'user_profile.json'}")
    click.echo("\nNext: `sharing_on chat submit \"...\"` — systemu now knows you.")


@user_group.command("show")
@click.pass_context
def user_show(ctx):
    """Display the current profile + a summary of facts."""
    _cfg, vault = _get_vault_and_config(ctx)
    prof = vault.get_user_profile()
    if prof is None:
        click.echo("No profile set. Run `sharing_on user init` to create one.")
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
        click.echo(f"  ... ({len(facts) - 5} more — `sharing_on user facts list` for all)")


@user_group.command("set")
@click.argument("field", type=click.Choice(["name", "location_text", "timezone",
                                            "default_output_dir"]))
@click.argument("value")
@click.pass_context
def user_set(ctx, field: str, value: str):
    """Update one typed field on the profile."""
    _cfg, vault = _get_vault_and_config(ctx)
    prof = vault.get_user_profile()
    if prof is None:
        click.echo("No profile set. Run `sharing_on user init` first.")
        ctx.exit(1)
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
