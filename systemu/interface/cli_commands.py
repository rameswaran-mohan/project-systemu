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
    try:
        scroll = refine_scroll(session_path, config, vault, auto_proceed=auto)
        console.print(f"\n[green]✓ Scroll created:[/green] {scroll.id} — status: {scroll.status}")
    except Exception as exc:
        console.print(f"\n[red]Error:[/red] {exc}")
        import traceback; traceback.print_exc()
        sys.exit(1)


@scrolls_group.command("approve")
@click.argument("scroll_id")
@click.pass_context
def scrolls_approve(ctx, scroll_id: str):
    """Approve a PENDING_APPROVAL scroll and trigger activity extraction (Stages 3-6)."""
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
    """deprecate (effectiveness_score=0.0) or reactivate a skill.

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
    """Export a Systemu Skill as a portable Anthropic Agent Skill bundle.

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
    config, vault = _get_vault_and_config(ctx)
    from systemu.pipelines.tool_forge import forge_tool_by_name

    console.print(f"\n[cyan]🔧 Forging tool:[/cyan] {name}\n")
    result = forge_tool_by_name(name, config, vault, context_hint=context)
    if result:
        console.print(f"[green]✓ Tool '{result.name}' forged successfully (status: {result.status})[/green]")
    else:
        console.print("[yellow]Forge skipped or failed.[/yellow]")


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
@click.pass_context
def army_execute(ctx, shadow_id: str, scroll_id: str, dry_run: bool):
    """Execute a Scroll via a Shadow (agentic runtime).

    Uses the ShadowRuntime ReAct loop: Reason → Tool Call → Observe → repeat.
    Requires the Shadow to have at least one DEPLOYED tool. Use --dry-run to
    preview the execution plan without invoking real tools (all PROPOSED tools allowed).
    """
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

    result = asyncio.run(runtime.execute(shadow, activity, dry_run=dry_run))

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
@click.pass_context
def daemon_stop(ctx):
    """Stop the running Systemu daemon."""
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
