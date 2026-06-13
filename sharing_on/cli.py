"""sharing_on CLI — the main entry point.

Commands:
  record   Start recording computer activity
  analyze  Re-analyze a completed session without re-recording
  info     Show platform capabilities

Systemu commands (agent factory):
  scrolls  Manage Scrolls — refined SOPs from capture sessions
  army     Manage the Shadow Army — autonomous agent personas
  tools    Manage the Tool registry
  skills   Manage the Skills registry
  settings Show Systemu configuration
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Load .env BEFORE any module that reads env vars — Config, vault factory,
# task queue protocol all check os.environ at import time.  Without this,
# native-local-mode users following the README literally see file-backend
# defaults instead of the sqlite/huey setup install.py wrote to .env.
# Docker modes use compose's env_file: directive and don't need this; loading
# is a no-op when there's no .env on disk (override=False = won't clobber
# already-set env from the parent shell).
try:
    from dotenv import load_dotenv as _load_dotenv
    # v0.7.3 Bug #2 fix: check CWD FIRST so pip-install users' working-dir
    # .env wins over anything next to the installed package (which only
    # makes sense for git-clone / editable installs).
    _here = Path(__file__).resolve().parent
    _candidates = [Path.cwd(), _here, _here.parent, _here.parent.parent]
    _seen = set()
    for _candidate in _candidates:
        _candidate = _candidate.resolve()
        if _candidate in _seen:
            continue
        _seen.add(_candidate)
        _env_path = _candidate / ".env"
        if _env_path.exists():
            _load_dotenv(_env_path, override=False)
            break
except ImportError:
    # python-dotenv missing — the CLI still works, just without .env
    # auto-loading.  Operators can pre-source manually.
    pass

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import print as rprint

from sharing_on import __version__ as _sharing_on_version
from sharing_on.analyzer.generator import generate_instructions
from sharing_on.analyzer.step_detector import StepDetector
from sharing_on.analyzer.unifier import unify_events
from sharing_on.config import Config
from sharing_on.output.markdown import render_markdown
from sharing_on.platform_info import check_dependencies, detect_platform
from sharing_on.session import CaptureSession

console = Console()
logger = logging.getLogger(__name__)


def _append_intent_trace(scroll, intent) -> None:
    """v0.6.5-b: append a TraceEvent to scroll.pipeline_trace summarising Stage 1.

    Called from _run_analysis after refine_scroll returns a scroll.  Adds a
    single event whose level reflects the extraction outcome:
      - ``error``: extraction failed entirely
      - ``warn``:  ``confidence=low`` (downstream uses narrative-only fallback)
      - ``info``:  ``confidence=medium|high`` (intent is used downstream)
    """
    from systemu.core.models import TraceEvent

    if getattr(intent, "error", None):
        scroll.pipeline_trace.append(TraceEvent(
            stage="intent",
            level="error",
            message=f"intent extraction failed: {str(intent.error)[:80]}",
            detail={"error": intent.error, "confidence": getattr(intent, "confidence", "?")},
        ))
    elif not getattr(intent, "is_usable", False):
        conf = getattr(intent, "confidence", "?")
        scroll.pipeline_trace.append(TraceEvent(
            stage="intent",
            level="warn",
            message=f"confidence={conf}; downstream uses narrative-only fallback",
            detail={"confidence": conf, "intent": str(getattr(intent, "intent", ""))[:80]},
        ))
    else:
        conf = getattr(intent, "confidence", "?")
        scroll.pipeline_trace.append(TraceEvent(
            stage="intent",
            level="info",
            message=f"intent extracted ({conf})",
            detail={"confidence": conf, "intent": str(getattr(intent, "intent", ""))[:80]},
        ))


def main():
    """sharing_on CLI entry point."""
    cli(obj={})


@click.group()
@click.version_option(_sharing_on_version, prog_name="sharing_on")
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx, debug: bool):
    """
    sharing_on — Record computer activity and generate step-by-step instructions.

    \b
    Quick start:
      1. Copy .env.example to .env and add your OpenRouter API key
      2. Run:  sharing_on record --name "My Task"
      3. Perform your task, then press Ctrl+C to stop
      4. Find your instructions.md in the captures/ directory
    """
    ctx.ensure_object(dict)
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# record command
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--name", "-n",
    default="",
    help="Name for this capture session (e.g. 'Deploy to production').",
    prompt="Session name (describe the task you're about to perform)",
)
@click.option(
    "--watch", "-w",
    multiple=True,
    help="Directory to watch for file changes. Repeat for multiple dirs.",
)
@click.option(
    "--output", "-o",
    default="",
    help="Output directory (default: ./captures/<name>_<session_id>/).",
)
@click.option(
    "--screenshots",
    is_flag=True,
    default=False,
    help="Enable periodic screenshot capture (opt-in; screenshots are not used by the LLM pipeline).",
)
@click.option(
    "--screenshot-interval",
    default=3.0,
    show_default=True,
    help="Seconds between screenshots (only applies when --screenshots is set).",
    type=float,
)
@click.option(
    "--model",
    default="",
    help="OpenRouter model (e.g. openai/gpt-4o, anthropic/claude-3-haiku).",
)
@click.option(
    "--no-analyze",
    is_flag=True,
    default=False,
    help="Skip LLM analysis after recording. Just save raw events.",
)
@click.pass_context
def record(
    ctx,
    name: str,
    watch: tuple,
    output: str,
    screenshots: bool,
    screenshot_interval: float,
    model: str,
    no_analyze: bool,
):
    """
    Record computer activity for a task and generate step-by-step instructions.

    \b
    Examples:
      sharing_on record --name "Set up Python project"
      sharing_on record --name "Deploy app" --watch ./src --watch ./config
      sharing_on record --name "Fix bug" --screenshot-interval 5
    """
    # Load config from .env
    config = Config.from_env()

    # Apply CLI overrides
    if model:
        config.llm_model = model
    config.capture_screenshots = screenshots
    if screenshots and screenshot_interval:
        config.screenshot_interval = screenshot_interval
    if watch:
        config.watch_dirs = list(watch)
    if output:
        config.output_base_dir = output

    # Validate config (only needed for analysis step)
    if not no_analyze:
        errors = config.validate()
        if errors:
            console.print("\n[bold red]Configuration errors:[/bold red]")
            for e in errors:
                console.print(f"  ✗ {e}")
            console.print(
                "\n[dim]Tip: Copy .env.example to .env and add your OpenRouter key.[/dim]"
            )
            sys.exit(1)

    # Check dependencies
    missing = check_dependencies()
    if missing:
        console.print("\n[bold red]Missing dependencies:[/bold red]")
        for dep in missing:
            console.print(f"  ✗ {dep}")
        console.print("\nRun: [bold]pip install -r requirements.txt[/bold]")
        sys.exit(1)

    # Detect and display platform info
    platform = detect_platform()
    _print_startup_banner(name, platform, config)

    # Build the capture session
    output_path = Path(output) if output else None
    session = CaptureSession(
        name=name,
        config=config,
        output_dir=output_path,
    )

    # Set up Ctrl+C → graceful stop
    stop_requested = [False]

    def _handle_interrupt(sig, frame):
        if not stop_requested[0]:
            stop_requested[0] = True
            console.print("\n\n[bold yellow]⏹  Stopping capture...[/bold yellow]")

    signal.signal(signal.SIGINT, _handle_interrupt)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_interrupt)

    # Start recording
    session.start()

    # --- Live status display ---
    _run_live_display(session, stop_requested)

    # Stop collectors and flush
    session.stop()

    console.print()
    console.print(Panel.fit(
        f"[bold green]✓ Capture complete[/bold green]\n"
        f"[dim]Events stored: {session.event_count:,}[/dim]\n"
        f"[dim]Output dir: {session.output_dir}[/dim]",
        border_style="green",
    ))

    if no_analyze:
        console.print(
            f"\n[dim]Skipping analysis. "
            f"Run [bold]sharing_on analyze {session.output_dir}[/bold] later.[/dim]"
        )
        return

    # --- Analyze in the background ---
    _run_analysis_in_background(session.output_dir, config)


def _run_analysis_in_background(output_dir: Path, config: Config) -> None:
    """Spawn the 'analyze' command as a detached background process."""
    import os
    log_file = output_dir / "analysis.log"

    # Build the command that calls the existing 'analyze' subcommand
    cmd = [sys.executable, "-m", "sharing_on", "analyze", str(output_dir)]
    if config.llm_model:
        cmd += ["--model", config.llm_model]

    # Force UTF-8 encoding in the child process so Rich can write to the log file
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    # Background process has no TTY — mark headless so notify_user() auto-approves
    # prompts (scroll approval, shadow creation) instead of blocking indefinitely.
    child_env["SYSTEMU_HEADLESS"] = "1"
    
    try:
        import systemu
        child_env["PYTHONPATH"] = str(Path(systemu.__file__).parent.parent.absolute())
    except ImportError:
        pass

    try:
        with open(log_file, "w", encoding="utf-8") as log_fp:
            if sys.platform == "win32":
                DETACHED_PROCESS = 0x00000008
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_fp,
                    stderr=log_fp,
                    creationflags=DETACHED_PROCESS,
                    close_fds=True,
                    env=child_env,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_fp,
                    stderr=log_fp,
                    start_new_session=True,
                    close_fds=True,
                    env=child_env,
                )

        console.print()
        console.print(Panel.fit(
            f"[bold green]Analysis running in background[/bold green] (PID {proc.pid})\n\n"
            f"[dim]Instructions will be saved to:[/dim]\n"
            f"  {output_dir / 'instructions.md'}\n\n"
            f"[dim]Follow progress:[/dim]\n"
            f"  [bold]Get-Content -Wait \"{log_file}\"[/bold]  [dim](PowerShell)[/dim]\n"
            f"  [bold]tail -f \"{log_file}\"[/bold]          [dim](macOS/Linux)[/dim]",
            title="sharing_on",
            border_style="cyan",
        ))

    except Exception as e:
        # Background launch failed — fall back to inline analysis
        console.print(
            f"\n[yellow]! Could not spawn background process ({e}). "
            f"Running analysis inline...[/yellow]\n"
        )
        from sharing_on.session import CaptureSession
        # Re-use the session object path — load events from disk
        from sharing_on.events.store import EventStore
        dummy_store = EventStore(output_dir / "events.db")
        import json
        meta = json.loads((output_dir / "session.json").read_text())

        class _SessionProxy:
            event_count = dummy_store.event_count
            name = meta.get("name", "Recorded Task")
            session_id = meta.get("session_id", "unknown")
            start_time = None
            end_time = None

            class platform:
                @staticmethod
                def summary(): return meta.get("platform", "Unknown")

            def get_events(self):
                return dummy_store.get_all_events()

            @property
            def output_dir(self):
                return output_dir

        _run_analysis(_SessionProxy(), config)


def _run_live_display(session: "CaptureSession", stop_requested: list) -> None:
    """Show a live updating status panel while recording."""
    console.print()
    console.print(
        "[bold green]◉ Recording...[/bold green]  "
        "[dim]Press [bold]Ctrl+C[/bold] when done, or press [bold]M[/bold] "
        "then Enter to add a step marker.[/dim]"
    )
    console.print()

    start_time = datetime.now()

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Recording activity...", total=None)

        while not stop_requested[0]:
            elapsed = (datetime.now() - start_time).total_seconds()
            event_count = session.event_count

            progress.update(
                task,
                description=(
                    f"[green]●[/green] Recording  |  "
                    f"Events: [bold]{event_count:,}[/bold]  |  "
                    f"Collectors: [bold]{len([c for c in session.collector_status if c['running']])}[/bold] active"
                ),
            )
            time.sleep(0.5)


def _run_analysis(session: "CaptureSession", config: Config) -> None:
    """Run step detection and LLM instruction generation."""
    console.print()
    console.rule("[bold blue]Analysis[/bold blue]")

    # Step 1: Load events
    with console.status("[bold]Loading captured events...[/bold]"):
        events = session.get_events()

    console.print(f"  [green]v[/green] Loaded [bold]{len(events):,}[/bold] raw events")

    # Step 1b: Unify (deduplicate, filter noise, collapse repeats)
    with console.status("[bold]Unifying and deduplicating events...[/bold]"):
        events = unify_events(events)

    console.print(f"  [green]v[/green] Unified to [bold]{len(events):,}[/bold] clean events")

    # Step 2: Detect steps
    with console.status("[bold]Detecting step boundaries...[/bold]"):
        detector = StepDetector(idle_threshold=config.step_idle_threshold)
        steps = detector.detect_steps(events)

    console.print(f"  [green]✓[/green] Detected [bold]{len(steps)}[/bold] steps")

    if not steps:
        console.print(
            "\n[yellow]⚠ No steps detected. "
            "The session may have been too short or had no activity.[/yellow]"
        )
        return

    # Print step summary
    _print_step_table(steps)

    # Step 3: Generate instructions with LLM
    console.print()
    console.print(
        f"  [dim]Using model: [bold]{config.llm_model}[/bold][/dim]"
    )

    with console.status(
        f"[bold]Generating instructions via {config.tier3_model}...[/bold]"
    ):
        duration = 0.0
        if session.start_time and session.end_time:
            duration = (session.end_time - session.start_time).total_seconds()

        instructions = generate_instructions(
            steps=steps,
            session_name=session.name,
            platform_info=session.platform.summary(),
            duration_seconds=duration,
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            model=config.tier3_model,
        )

    console.print("  [green]v[/green] Instructions generated")

    # Step 4: Render Markdown
    with console.status("[bold]Rendering Markdown document...[/bold]"):
        output_path = render_markdown(
            instructions=instructions,
            steps=steps,
            session_name=session.name,
            session_id=session.session_id,
            platform_info=session.platform.summary(),
            start_time=session.start_time,
            end_time=session.end_time,
            output_dir=session.output_dir,
            event_count=session.event_count,
        )

    console.print(f"  [green]✓[/green] Markdown saved")

    # Final output
    console.print()
    console.print(Panel.fit(
        f"[bold green]Done![/bold green]\n\n"
        f"📄 [bold]Instructions:[/bold] [link={output_path}]{output_path}[/link]\n"
        f"📁 [bold]Full output:[/bold]  {session.output_dir}",
        title="sharing_on",
        border_style="green",
    ))

    # --- Systemu Stage 2: Post-capture hook ---
    try:
        from systemu.pipelines.scroll_refiner import refine_scroll
        from systemu.vault.factory import open_vault
        console.print()
        console.print("[bold]Systemu:[/bold] Processing capture session...")
        vault = open_vault(config)
        console.print(f"  [dim]Storage backend: {type(vault).__name__}[/dim]")
        refine_scroll(session.output_dir, config, vault)
        console.print("  [green]v[/green] Session handed off to Systemu")
    except ImportError:
        pass  # systemu package not available in this environment
    except Exception as e:
        console.print(f"  [yellow]! Scroll refinement failed: {e}[/yellow]")


# ---------------------------------------------------------------------------
# analyze command
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("session_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--model",
    default="",
    help="Override the LLM model.",
)
def analyze(session_dir: str, model: str):
    """
    Re-analyze a completed capture session.

    \b
    Use this to regenerate instructions without re-recording.

    \b
    Example:
      sharing_on analyze ./captures/my_task_cap_20260418_140000/
    """
    import json

    session_path = Path(session_dir)
    meta_file = session_path / "session.json"
    db_file = session_path / "events.db"

    if not meta_file.exists() or not db_file.exists():
        console.print(
            f"[red]x Invalid session directory: {session_dir}[/red]\n"
            "[dim]Expected session.json and events.db files.[/dim]"
        )
        sys.exit(1)

    with open(meta_file) as f:
        metadata = json.load(f)

    config = Config.from_env()
    if model:
        config.llm_model = model
        config.tier3_model = model

    errors = config.validate()
    if errors:
        for e in errors:
            console.print(f"[red]x {e}[/red]")
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold]Re-analyzing session:[/bold] {metadata.get('name', 'Unknown')}\n"
            f"[dim]{session_dir}[/dim]",
            border_style="blue",
        )
    )

    # Load events from the existing store
    from sharing_on.events.store import EventStore

    store = EventStore(db_file)
    events = store.get_all_events()

    console.print(f"  [green]v[/green] Loaded [bold]{len(events):,}[/bold] raw events")

    # Unify events
    with console.status("[bold]Unifying events...[/bold]"):
        events = unify_events(events)

    console.print(f"  [green]v[/green] Unified to [bold]{len(events):,}[/bold] clean events")

    # Detect steps
    with console.status("[bold]Detecting steps...[/bold]"):
        detector = StepDetector()
        steps = detector.detect_steps(events)

    console.print(f"  [green]v[/green] Detected [bold]{len(steps)}[/bold] steps")
    _print_step_table(steps)

    # v0.6.0-a Stage 1: pre-pass intent extraction.  Run BEFORE narrative
    # generation so the narrative LLM gets explicit outcome-oriented intent
    # rather than inferring it implicitly from a click-by-click log.  The
    # intent.json artifact is read by Stage 2 (scroll refiner).
    from sharing_on.analyzer.intent_extractor import extract_intent, write_intent_json

    with console.status("[bold]Inferring intent...[/bold]"):
        intent = extract_intent(
            steps=steps,
            events=events,
            session_name=metadata.get("name", "Recorded Task"),
            platform_info=metadata.get("platform", "Unknown platform"),
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            model=config.tier2_model,
        )
    write_intent_json(intent, session_path)
    if intent.is_usable:
        console.print(
            f"  [green]v[/green] Inferred intent (confidence={intent.confidence}): "
            f"[dim]{intent.intent[:80]}[/dim]"
        )
    else:
        console.print(
            f"  [yellow]i[/yellow] Intent inference low-confidence "
            f"(falling back to narrative-only mode)"
        )

    # Generate instructions
    with console.status(f"[bold]Generating via {config.tier3_model}...[/bold]"):
        instructions = generate_instructions(
            steps=steps,
            session_name=metadata.get("name", "Recorded Task"),
            platform_info=metadata.get("platform", "Unknown platform"),
            duration_seconds=0.0,
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            model=config.tier3_model,
            intent=intent if intent.is_usable else None,
        )

    # Render
    from datetime import timezone

    start_str = metadata.get("start_time")
    end_str = metadata.get("end_time")
    start_dt = datetime.fromisoformat(start_str) if start_str else None
    end_dt = datetime.fromisoformat(end_str) if end_str else None

    output_path = render_markdown(
        instructions=instructions,
        steps=steps,
        session_name=metadata.get("name", "Recorded Task"),
        session_id=metadata.get("session_id", "unknown"),
        platform_info=metadata.get("platform", ""),
        start_time=start_dt,
        end_time=end_dt,
        output_dir=session_path,
        event_count=len(events),
        intent=intent if intent.is_usable else None,
    )

    console.print(Panel.fit(
        f"[bold green]Done![/bold green]\n\n"
        f"Instructions: {output_path}\n"
        f"Full output:  {session_path}",
        border_style="green",
    ))

    # --- Systemu Stage 2: Post-capture hook ---
    try:
        from systemu.pipelines.scroll_refiner import refine_scroll
        from systemu.vault.factory import open_vault
        console.print()
        console.print("[bold]Systemu:[/bold] Processing capture session...")
        vault = open_vault(config)
        console.print(f"  [dim]Storage backend: {type(vault).__name__}[/dim]")
        refine_scroll(session_path, config, vault)
        console.print("  [green]v[/green] Session handed off to Systemu")

        # v0.6.5-b: record Stage 1 outcome on the scroll's pipeline_trace.
        # We look up the just-refined scroll by source_session_id and append
        # the intent extraction outcome so the operator sees it on /scrolls.
        try:
            sess_id = metadata.get("session_id")
            new_scroll = None
            for header in (vault.load_index("scrolls") or []):
                if header.get("source_session_id") == sess_id:
                    new_scroll = vault.get_scroll(header["id"])
                    break
            if new_scroll is not None:
                _append_intent_trace(new_scroll, intent)
                vault.save_scroll(new_scroll)
        except Exception:
            logger.debug("[v0.6.5] could not append intent trace", exc_info=True)
    except ImportError:
        pass  # systemu package not available in this environment
    except Exception as e:
        console.print(f"  [yellow]! Scroll refinement failed: {e}[/yellow]")


# ---------------------------------------------------------------------------
# info command
# ---------------------------------------------------------------------------

@cli.command()
def info():
    """Show platform capabilities and configuration status."""
    console.print()
    console.rule("[bold]sharing_on — System Info[/bold]")

    # Platform
    platform = detect_platform()
    console.print(f"\n[bold]Platform:[/bold] {platform.summary()}")

    # Capabilities table
    cap_table = Table(title="Capture Capabilities", show_header=True)
    cap_table.add_column("Capability", style="bold")
    cap_table.add_column("Status")

    all_caps = [
        ("screenshots", "[Screenshots]"),
        ("window_tracker", "[Window tracking]"),
        ("process_monitor", "[Process monitoring]"),
        ("file_watcher", "[File watching]"),
        ("clipboard", "[Clipboard]"),
        ("wayland_session", "[Wayland]"),
        ("ui_introspection", "[Native UI Introspection]"),
    ]

    for cap_id, cap_name in all_caps:
        if cap_id in platform.capabilities:
            cap_table.add_row(cap_name, "[green]v Available[/green]")
        else:
            cap_table.add_row(cap_name, "[dim]x Not available on this platform[/dim]")

    console.print()
    console.print(cap_table)

    # Dependency check
    missing = check_dependencies()
    console.print()
    if missing:
        console.print("[bold red]Missing dependencies:[/bold red]")
        for dep in missing:
            console.print(f"  x {dep}")
    else:
        console.print("[bold green]v All dependencies installed[/bold green]")

    # Config check
    config = Config.from_env()
    errors = config.validate()
    console.print()
    if errors:
        console.print("[bold yellow]Configuration issues:[/bold yellow]")
        for e in errors:
            console.print(f"  ! {e}")
    else:
        console.print(
            f"[bold green]v Configuration OK[/bold green]  "
            f"[dim](model: {config.llm_model})[/dim]"
        )

    console.print()


# ---------------------------------------------------------------------------
# init command (v0.7.4 Pattern 4)
# ---------------------------------------------------------------------------


_PROVIDER_CHOICES = ["auto", "openrouter", "google", "anthropic", "openai", "ollama"]


@cli.command()
@click.option("--key", default=None,
              help="OpenRouter key non-interactively (CI). Prefer the hidden "
                   "interactive prompt — argv lands in shell history.")
@click.option("--preset", type=click.Choice(["balanced", "quality", "budget"]),
              default=None, help="Model preset (default: ask).")
@click.option("--output-dir", default=None, help="Where produced files land.")
@click.option("--no-validate", is_flag=True, help="Skip the live key probe.")
@click.option("--tier1-provider", type=click.Choice(_PROVIDER_CHOICES), default=None)
@click.option("--tier2-provider", type=click.Choice(_PROVIDER_CHOICES), default=None)
@click.option("--tier3-provider", type=click.Choice(_PROVIDER_CHOICES), default=None)
@click.option("--tier1-model", default=None)
@click.option("--tier2-model", default=None)
@click.option("--tier3-model", default=None)
@click.option("--anthropic-key", default=None, help="ANTHROPIC_API_KEY (CI).")
@click.option("--openai-key", default=None, help="OPENAI_API_KEY (CI).")
@click.option("--ollama-url", default=None, help="OLLAMA_URL (CI).")
def setup(key, preset, output_dir, no_validate, tier1_provider, tier2_provider,
          tier3_provider, tier1_model, tier2_model, tier3_model,
          anthropic_key, openai_key, ollama_url):
    """Configure your API key(s) + models (run right after `pip install systemu`).

    Simple path: an OpenRouter key (entered hidden, validated, stored in a
    0600 .env) + a model preset. Advanced: pick a provider PER TIER with the
    --tier{N}-provider/--tier{N}-model flags (credentials via --anthropic-key
    / --openai-key / --ollama-url, or the per-provider env vars). Re-run any
    time. `daemon start` runs this for you the first time if no key is set.
    """
    import sys as _sys

    from sharing_on.setup_flow import _PROVIDER_CRED_ENV, run_setup

    # Assemble per-tier specs when any --tierN-provider was given (CI/advanced).
    tier_specs = None
    if any(v is not None for v in (tier1_provider, tier2_provider, tier3_provider)):
        _cred_by_prov = {"anthropic": anthropic_key, "openai": openai_key,
                         "ollama": ollama_url, "openrouter": key}
        tier_specs = []
        for prov, model in ((tier1_provider, tier1_model),
                            (tier2_provider, tier2_model),
                            (tier3_provider, tier3_model)):
            prov = (prov or "auto")
            tier_specs.append({"provider": prov, "model": model or "",
                               "credential": _cred_by_prov.get(prov) or ""})

    interactive = (key is None and tier_specs is None and _sys.stdin.isatty())
    if not interactive and key is None and tier_specs is None:
        console.print("[yellow]Non-interactive and no --key / --tier*-provider "
                      "given — nothing to configure. Pass flags, or run in a "
                      "terminal.[/yellow]")
        return
    console.print("\n[cyan]⚡ Systemu setup[/cyan]")
    summary = run_setup(
        interactive=interactive, key=key, preset=preset, tier_specs=tier_specs,
        output_dir=output_dir, validate=not no_validate,
        print_fn=lambda s: console.print(s),
    )
    console.print("")
    for msg in summary["messages"]:
        console.print(f"  • {msg}")
    if summary["key_set"]:
        console.print(f"\n[green]✓ Configured.[/green] "
                      f"Wrote {summary['env_path']}. "
                      f"Next: [bold]sharing_on daemon start[/bold]")
    else:
        console.print("\n[yellow]No API key set — Systemu can't run tasks "
                      "until you add one (`sharing_on setup`).[/yellow]")


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing vault entries (default: skip).")
@click.option("--no-seed", is_flag=True, help="Create vault structure only; skip starter catalog seeding.")
def init(force: bool, no_seed: bool):
    """Initialise the CWD vault from the package's starter catalog (v0.7.4).

    Run this once after `pip install systemu` in your working directory.
    It creates ``./systemu/vault/`` and copies the bundled starter tools,
    skills, and tool implementations so the system has a working catalog
    on first run.

    Idempotent — existing entries are kept unless ``--force`` is passed.
    Use ``--no-seed`` if you want only the directory structure.
    """
    import json as _json
    from importlib import resources
    from datetime import datetime as _dt, timezone as _tz

    cwd = Path.cwd()
    target_root = cwd / "systemu" / "vault"
    target_root.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Vault root: {target_root}[/dim]")

    if no_seed:
        console.print("[yellow]--no-seed: skipping starter catalog.[/yellow]")
        return

    try:
        pkg_vault = resources.files("systemu") / "vault"
    except Exception as exc:
        console.print(f"[red]Could not locate packaged vault: {exc}[/red]")
        raise click.Abort()

    seed_log = target_root / ".seed_log.json"
    prior = {}
    if seed_log.exists():
        try:
            prior = _json.loads(seed_log.read_text(encoding="utf-8"))
        except Exception:
            prior = {}

    copied = {"tools": 0, "skills": 0, "implementations": 0, "skipped": 0}

    def _copy_indexed(kind: str):
        src_idx = pkg_vault / kind / "index.json"
        dst_idx = target_root / kind / "index.json"
        dst_idx.parent.mkdir(parents=True, exist_ok=True)
        try:
            src_entries = _json.loads(src_idx.read_text(encoding="utf-8"))
        except Exception:
            console.print(f"[yellow]No starter {kind}/index.json in package — skipping.[/yellow]")
            return

        if dst_idx.exists() and not force:
            existing = _json.loads(dst_idx.read_text(encoding="utf-8"))
            existing_ids = {e.get("id") for e in existing}
            merged = list(existing)
            for entry in src_entries:
                if entry.get("id") in existing_ids:
                    copied["skipped"] += 1
                    continue
                merged.append(entry)
                copied[kind] += 1
        else:
            merged = list(src_entries)
            copied[kind] = len(merged)

        dst_idx.write_text(_json.dumps(merged, indent=2), encoding="utf-8")

        # Copy per-entry JSON files.
        #
        # v0.8.2 BUGFIX: Vault.save_tool / save_skill write to
        # ``{kind}/{kind-singular}_{entry.id}.json`` (e.g.  tool record id
        # ``tool_abc123`` is saved as ``tools/tool_tool_abc123.json`` — the
        # ``tool_`` prefix from save_tool plus the id which already starts with
        # ``tool_``).  The previous version of this loop looked for
        # ``{entry_id}.json`` (a single prefix) and silently skipped every file
        # because ``src_file.is_file()`` returned False for all 40 starter
        # tools.  Operators ended up with an index promising 40 tools and ZERO
        # body files; every ``get_tool()`` in the pipeline raised KeyError,
        # breaking activity extraction, shadow assignment, and execution.
        #
        # Fix: derive the prefix from ``kind`` ("tools" → "tool", "skills" →
        # "skill") and look for the actual filename Vault uses.
        kind_singular = kind.rstrip("s")  # "tools"→"tool", "skills"→"skill"
        for entry in src_entries:
            entry_id = entry.get("id")
            if not entry_id:
                continue
            src_file = pkg_vault / kind / f"{kind_singular}_{entry_id}.json"
            dst_file = target_root / kind / f"{kind_singular}_{entry_id}.json"
            if src_file.is_file():
                if dst_file.exists() and not force:
                    continue
                dst_file.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                # Surface the gap loudly — silent skip is what bit operators
                # in v0.7.4 through v0.8.1.
                logger.warning(
                    "[init] %s entry %s has no body file in package vault at %s — skipping",
                    kind_singular, entry_id, src_file,
                )

    _copy_indexed("tools")
    _copy_indexed("skills")

    # Copy tool implementations directory
    impl_src = pkg_vault / "tools" / "implementations"
    impl_dst = target_root / "tools" / "implementations"
    impl_dst.mkdir(parents=True, exist_ok=True)
    if impl_src.is_dir():
        for child in impl_src.iterdir():
            if not child.is_file():
                continue
            target = impl_dst / child.name
            if target.exists() and not force:
                continue
            target.write_text(child.read_text(encoding="utf-8"), encoding="utf-8")
            copied["implementations"] += 1

    # Copy skill SKILL.md files
    skill_src_root = pkg_vault / "skills"
    skill_dst_root = target_root / "skills"
    skill_dst_root.mkdir(parents=True, exist_ok=True)
    if skill_src_root.is_dir():
        for skill_dir in skill_src_root.iterdir():
            if not skill_dir.is_dir():
                continue
            dest_dir = skill_dst_root / skill_dir.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            src_md = skill_dir / "SKILL.md"
            if src_md.is_file():
                dest_md = dest_dir / "SKILL.md"
                if dest_md.exists() and not force:
                    continue
                dest_md.write_text(src_md.read_text(encoding="utf-8"), encoding="utf-8")

    # Write seed log
    prior["last_init_at"] = _dt.now(tz=_tz.utc).isoformat(timespec="seconds")
    prior["last_init_version"] = _sharing_on_version
    prior["last_init_copied"] = copied
    seed_log.write_text(_json.dumps(prior, indent=2), encoding="utf-8")

    console.print(
        f"[green]✓ init complete.[/green] "
        f"tools={copied['tools']} skills={copied['skills']} "
        f"implementations={copied['implementations']} skipped={copied['skipped']}"
    )


# ---------------------------------------------------------------------------
# doctor command (v0.6.8-a)
# ---------------------------------------------------------------------------
# Diagnoses pending gates/blockers for any scroll/activity/shadow/tool by
# delegating to systemu.recovery.engine.RecoveryEngine.  Operators run this
# when a scroll is stuck and they want a checklist of fixes — same data
# the dashboard's recovery panel shows, surfaced for the terminal.

def _infer_scope(scope_id: str):
    """Map an id prefix to the entity kind RecoveryEngine knows about.

    Accepts both the canonical underscored prefixes (``scr_``, ``act_``,
    ``sh_``, ``tool_``) and the unsuffixed short forms (``scr1``, ``act1``,
    ``sh1``, ``tool_a``) used by older fixtures and ad-hoc inserts.
    """
    if scope_id.startswith(("scr_", "scroll_")) or scope_id.startswith("scr"):
        return "scroll"
    if scope_id.startswith(("act_", "activity_")) or scope_id.startswith("act"):
        return "activity"
    if scope_id.startswith(("sh_", "shadow_")) or scope_id.startswith("sh"):
        return "shadow"
    if scope_id.startswith("tool"):
        return "tool"
    return None


@cli.command()
@click.argument("scope_id")
@click.option("--apply", "apply_mode", is_flag=True,
              help="Apply auto-recoverable actions (install-dep / enable-tool / "
                   "reset-memory) via the shared recovery dispatchers — the same "
                   "apply path the web recovery panel uses. Gate reviews are skipped.")
def doctor(scope_id: str, apply_mode: bool):
    """Diagnose pending gates for a scroll/activity/shadow/tool.

    \b
    Examples:
      sharing_on doctor scr_abc123
      sharing_on doctor tool_xyz789
    """
    import os
    from systemu.recovery.engine import RecoveryEngine
    from systemu.storage.sqlite.vault import SqliteVault

    db_url = os.environ.get("SYSTEMU_DATABASE_URL")
    if not db_url:
        click.echo("ERROR: SYSTEMU_DATABASE_URL not set", err=True)
        sys.exit(2)

    scope = _infer_scope(scope_id)
    if scope is None:
        click.echo(
            f"ERROR: cannot infer scope for id {scope_id!r} "
            f"(expected prefix scr_/act_/sh_/tool_)",
            err=True,
        )
        sys.exit(2)

    vault = SqliteVault(database_url=db_url)
    eng = RecoveryEngine(vault=vault)

    finder = {
        "scroll": vault.find_scroll,
        "activity": vault.find_activity,
        "shadow": vault.find_shadow,
        "tool": vault.find_tool,
    }[scope]

    if finder(scope_id) is None:
        click.echo(f"ERROR: {scope} {scope_id!r} not found in vault", err=True)
        sys.exit(3)

    method = {
        "scroll": eng.diagnose_scroll,
        "activity": eng.diagnose_activity,
        "shadow": eng.diagnose_shadow,
        "tool": eng.diagnose_tool,
    }[scope]

    actions = method(scope_id)
    click.echo(f"Diagnosing {scope_id} (scope: {scope})")
    if not actions:
        click.echo("OK no pending actions")
        sys.exit(0)
    click.echo(f"{len(actions)} action(s) pending:")
    for i, a in enumerate(actions, 1):
        click.echo(f"  [{i}] {a.scope_kind} {a.scope_id}: {a.kind} ({a.severity})")
        click.echo(f"      {a.reason}")
        click.echo(f"      Fix:  {a.fix_url}")
        if a.fix_command:
            click.echo(f"            OR: {a.fix_command}")

    if not apply_mode:
        sys.exit(0)

    # --apply: route through the SAME shared recovery dispatchers the web
    # recovery panel uses (verbs.doctor_apply -> recover.py:_handle_action),
    # threading this CLI's own vault (AppState is not initialised headless).
    from systemu.interface.command import verbs

    click.echo("Applying recoverable actions...")
    result = verbs.doctor_apply(actions, vault=vault)
    for line in result.data.get("log", []):
        click.echo(f"  {line}")
    click.echo(result.summary)
    sys.exit(result.exit_code)


# ---------------------------------------------------------------------------
# capture command group (v0.7.1)
# ---------------------------------------------------------------------------
# Capture-side commands operating on a recorded session directory.  The
# strategic-wedge command `export-skill` turns a finished recording into
# a portable Anthropic Agent Skill bundle in one step.

@cli.group()
def capture():
    """Capture-side commands operating on a recorded session directory."""


@capture.command("export-skill")
@click.argument("session", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output", "-o", required=True,
    type=click.Path(file_okay=False),
    help="Directory where the spec-conformant skill bundle is written.",
)
@click.option(
    "--auto-approve", is_flag=True, default=False,
    help="Bypass the scroll PENDING_APPROVAL gate. Still respects the "
         "tool-dep allow-list (v0.6.8-d) — same as analyze --auto-approve.",
)
def capture_export_skill(session: str, output: str, auto_approve: bool):
    """Record once, export as a portable Anthropic Agent Skill.

    \b
    Example:
      sharing_on capture export-skill ./captures/email_digest_cap_… \\
                 --output ./my-skill
    """
    from systemu.pipelines.capture_to_skill import export_skill_from_capture
    from systemu.vault.factory import open_vault

    config = Config.from_env()
    try:
        vault = open_vault(config)
    except Exception as e:
        click.echo(f"ERROR: could not open vault: {e}", err=True)
        sys.exit(1)

    try:
        out_path = export_skill_from_capture(
            session_dir=Path(session),
            target_dir=Path(output),
            config=config,
            vault=vault,
            auto_approve=auto_approve,
        )
    except FileNotFoundError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(2)
    except KeyError as e:
        click.echo(f"ERROR: skill not found in vault: {e}", err=True)
        sys.exit(3)
    except FileExistsError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(4)
    except RuntimeError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(5)

    click.echo(f"Exported -> {out_path}")
    click.echo(f"Validate: skills-ref validate {out_path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_startup_banner(name: str, platform, config: Config) -> None:
    """Print the startup banner with session details."""
    watch_dirs = config.watch_dirs or ["(none — add --watch <dir>)"]

    screenshots_label = (
        f"enabled (every {config.screenshot_interval}s)"
        if config.capture_screenshots
        else "off  [dim](use --screenshots to enable)[/dim]"
    )
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]sharing_on[/bold cyan]  [dim]v{_sharing_on_version}[/dim]\n\n"
        f"[bold]Task:[/bold]        {name}\n"
        f"[bold]Platform:[/bold]    {platform.summary()}\n"
        f"[bold]Model:[/bold]       {config.llm_model}\n"
        f"[bold]Watching:[/bold]    {', '.join(watch_dirs)}\n"
        f"[bold]Screenshots:[/bold] {screenshots_label}",
        border_style="cyan",
        title="◉ Starting",
    ))


def _print_step_table(steps) -> None:
    """Print a summary table of detected steps."""
    console.print()
    table = Table(title="Detected Steps", show_header=True, min_width=60)
    table.add_column("#", style="dim", width=4)
    table.add_column("App", style="cyan")
    table.add_column("Duration", style="dim")
    table.add_column("Events", justify="right")
    table.add_column("Label")

    for step in steps:
        counts = step.event_summary
        total = sum(v for k, v in counts.items() if k != "screen")
        duration = f"{step.duration_seconds:.0f}s" if step.duration_seconds else "—"
        label = step.label or "[dim]—[/dim]"

        table.add_row(
            str(step.step_number),
            step.primary_app or "—",
            duration,
            str(total),
            label,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Systemu command groups
# ---------------------------------------------------------------------------
# Import and register all Systemu CLI groups so they appear under
# `sharing_on <group> <command>` without modifying any pipeline code.

try:
    from systemu.interface.cli_commands import (
        scrolls_group,
        army_group,
        tools_group,
        skills_group,
        settings_cmd,
        evolve_group,
        daemon_group,
        chat_group,
        debug_group,
        decisions_group,
        user_group,
        session_cli,
        capability_cli,
        skill_cli,
    )
    cli.add_command(scrolls_group,   name="scrolls")
    cli.add_command(army_group,      name="army")
    cli.add_command(tools_group,     name="tools")
    cli.add_command(skills_group,    name="skills")
    cli.add_command(settings_cmd,    name="settings")
    cli.add_command(evolve_group,    name="evolve")
    cli.add_command(daemon_group,    name="daemon")
    cli.add_command(chat_group,      name="chat")
    cli.add_command(debug_group,     name="debug")
    cli.add_command(decisions_group, name="decisions")
    cli.add_command(user_group,      name="user")
    cli.add_command(session_cli,     name="session")
    cli.add_command(capability_cli,  name="capability")
    cli.add_command(skill_cli,       name="skill")
except ImportError:
    # systemu package not yet installed — sharing_on still works standalone
    pass
