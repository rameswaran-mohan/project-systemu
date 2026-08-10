"""PACKET B / DEFECT F3 — the headless setup deadlock.

PROPERTY (class-level, quantified over every gate — not over one function)
    Every gate reported by ``systemu.runtime.first_run.setup_status`` can be
    satisfied with **no TTY** and **without the dashboard**.

FENCE (three layers, all committed, all mechanical)
    1. COMPLETENESS — ``first_run.HEADLESS_REMEDIES`` is a declarative registry
       mapping every check id to the non-interactive command that satisfies it.
       ``setup_status`` attaches the entry to each check as
       ``check["headless"]``.  ``test_every_setup_check_declares_a_headless_path``
       asserts a BIJECTION between the registry and the live check ids, for
       EVERY check — required or not.  Covering non-required checks too closes
       the "flip ``required`` to False to dodge the fence" loophole.
    2. RESOLVABILITY — a registry entry is a *claim about the CLI*, and a claim
       is worthless if nothing checks it (DEC-34: a false assertion of
       enforcement is itself a defect).  ``test_declared_headless_commands_resolve``
       walks the REAL click command tree of ``sharing_on.cli.cli`` and fails if a
       declared command or option does not exist.  A typo'd or aspirational
       remedy cannot pass.
    3. EFFICACY — ``test_full_onboarding_completes_with_stdin_closed`` drives the
       real CLI in subprocesses with ``stdin=DEVNULL`` (a genuine headless box),
       executing the remedies *read out of the registry* one at a time and
       asserting the corresponding gate flips to ok=True after each.  A remedy
       that exists but does not actually satisfy its gate goes red here.
       Being a separate process, it also bypasses the DEC-44
       ``_stub_situation_survey`` autouse fixture entirely — this is real
       runtime behaviour, not a stubbed world.

WITNESS (what goes red when the defect returns)
    * Drop ``--non-interactive`` from ``user init``      -> layer 2 red
      (option gone from the click tree) AND layer 3 red (profile_present stays
      False after its declared remedy runs).
    * Restore the ``user set`` "No profile set" refusal  -> test_user_set_* red.
    * Delete ``onboarding complete-tour``                -> layer 2 + 3 red.
    * Add a 7th gate to ``setup_status`` with no remedy  -> layer 1 red.
    * Declare a remedy that runs but doesn't satisfy it  -> layer 3 red.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import click
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER_RE = re.compile(r"^<[A-Z_]+>$")


# ── helpers ──────────────────────────────────────────────────────────────────

def _headless_env(vault_dir: Path, **extra: str) -> dict:
    """A clean child environment: no inherited systemu / provider config.

    Deliberately does NOT set OPENROUTER_API_KEY or SYSTEMU_OUTPUT_DIR — the
    e2e must reach a ready install through the declared *commands*, not through
    a hand-set environment.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENROUTER_", "SYSTEMU_", "SHARING_ON_",
                                "ANTHROPIC_", "OPENAI_", "OLLAMA_"))}
    env["PYTHONIOENCODING"] = "utf-8"
    # Self-sufficient: never depend on an editable install of the right tree.
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["SYSTEMU_VAULT_DIR"] = str(vault_dir)
    env.update(extra)
    return env


def _run(args, cwd: Path, env: dict, timeout: int = 300):
    """Run the real CLI with stdin CLOSED — the headless case, exactly."""
    return subprocess.run(
        [sys.executable, "-m", "sharing_on", *args],
        cwd=str(cwd), env=env,
        stdin=subprocess.DEVNULL,          # <- the crucial part
        capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace",
        timeout=timeout,
    )


def _status(cwd: Path, env: dict) -> dict:
    """Read the gate table. rc 0 = ready, rc 1 = not ready; anything else = broken."""
    r = _run(["onboarding", "status", "--json"], cwd, env)
    assert r.returncode in (0, 1), (
        f"onboarding status crashed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}")
    payload = json.loads(r.stdout)
    assert payload["ready"] is (r.returncode == 0), (
        "exit code and the reported `ready` flag disagree — a script cannot "
        "trust either")
    return payload


def _bind(argv, bindings: dict) -> list:
    """Substitute ``<PLACEHOLDER>`` tokens; fail loudly on an unbound one."""
    out = []
    for tok in argv[1:]:                    # argv[0] is the program name
        if _PLACEHOLDER_RE.match(tok):
            assert tok in bindings, (
                f"registry placeholder {tok} has no binding in this test — a new "
                "placeholder was introduced without proving the path works")
            out.append(bindings[tok])
        else:
            out.append(tok)
    return out


class _StubVault:
    root = ""

    def load_index(self, _kind):
        return []

    def get_user_profile(self):
        return None


class _StubConfig:
    openrouter_api_key = ""
    output_dir = ""


# ── layer 1: completeness ────────────────────────────────────────────────────

def test_every_setup_check_declares_a_headless_path():
    """No gate may exist without a declared non-interactive way to satisfy it.

    Covers EVERY check, not just required=True ones: otherwise a future author
    could dodge the fence by flipping ``required`` to False.
    """
    from systemu.runtime.first_run import HEADLESS_REMEDIES, setup_status

    checks = setup_status(_StubConfig(), _StubVault())
    assert checks, "setup_status returned nothing"

    undeclared = [c["id"] for c in checks
                  if not (c.get("headless") or {}).get("argv")]
    assert not undeclared, (
        f"setup gate(s) with no non-interactive command: {undeclared}. Every "
        "gate must be satisfiable without a TTY and without the dashboard — "
        "add an entry to first_run.HEADLESS_REMEDIES AND a real command that "
        "honours it.")

    # No drift the other way either: a stale entry is a lie about a dead gate.
    assert set(HEADLESS_REMEDIES) == {c["id"] for c in checks}


def test_dashboard_redirect_gates_are_covered_by_the_registry():
    """The set that hard-redirects the dashboard cannot outgrow the registry.

    ``welcome._REDIRECT_REQUIRED`` is a hand-written tuple. Adding an id to it
    that ``setup_status`` never produces would gate the dashboard on something
    with no declared headless path — the exact deadlock, reintroduced from the
    other end.
    """
    from systemu.interface.pages.welcome import _REDIRECT_REQUIRED
    from systemu.runtime.first_run import HEADLESS_REMEDIES, setup_status

    live = {c["id"] for c in setup_status(_StubConfig(), _StubVault())}
    orphans = sorted(set(_REDIRECT_REQUIRED) - live)
    assert not orphans, (
        f"_REDIRECT_REQUIRED names gate(s) setup_status never reports: {orphans}")
    uncovered = sorted(set(_REDIRECT_REQUIRED) - set(HEADLESS_REMEDIES))
    assert not uncovered, (
        f"the dashboard redirects on {uncovered}, which have no declared "
        "non-interactive path")


# ── layer 2: the declared commands really exist ──────────────────────────────

def _resolve_in_cli(argv):
    """Walk the real click tree. Returns (command, leftover_tokens)."""
    from sharing_on.cli import cli as root

    assert argv[0] == "sharing_on", f"remedy argv must start with the program name: {argv}"
    cmd = root
    ctx = click.Context(root)
    tokens = list(argv[1:])
    while tokens and isinstance(cmd, click.Group) and not tokens[0].startswith("-"):
        nxt = cmd.get_command(ctx, tokens[0])
        assert nxt is not None, (
            f"HEADLESS_REMEDIES declares `{' '.join(argv)}` but there is no "
            f"`{tokens[0]}` command under `{cmd.name}` in the real CLI")
        cmd = nxt
        ctx = click.Context(cmd, parent=ctx)
        tokens.pop(0)
    return cmd, tokens


def test_declared_headless_commands_resolve():
    """Every declared remedy names a command + options that actually exist.

    Layer 1 only proves *something* was written down. This proves the writing
    is not fiction.
    """
    from systemu.runtime.first_run import HEADLESS_REMEDIES

    for check_id, remedy in HEADLESS_REMEDIES.items():
        argv = remedy["argv"]
        cmd, leftover = _resolve_in_cli(argv)
        assert not isinstance(cmd, click.Group), (
            f"{check_id}: `{' '.join(argv)}` stops on the group `{cmd.name}` — "
            "a group is not runnable, name the leaf command")
        opts = set()
        for p in cmd.params:
            opts.update(getattr(p, "opts", None) or [])
            opts.update(getattr(p, "secondary_opts", None) or [])
        for tok in leftover:
            if tok.startswith("-"):
                assert tok in opts, (
                    f"{check_id}: `{' '.join(argv)}` passes {tok}, which is not "
                    f"an option of `{cmd.name}` (has: {sorted(opts)})")


# ── layer 3: efficacy — real CLI, real vault, stdin closed ───────────────────

@pytest.mark.slow
def test_full_onboarding_completes_with_stdin_closed(tmp_path):
    """Empty vault -> every gate ok=True, no TTY, no browser.

    The command list is READ OUT OF THE REGISTRY, so a remedy that is declared
    but does not work cannot hide behind a hand-written happy path here.
    """
    from systemu.runtime.first_run import HEADLESS_REMEDIES

    workdir = tmp_path / "work"
    workdir.mkdir()
    out_dir = workdir / "out"
    # Deliberately NOT the built-in default (<cwd>/systemu/vault): a container
    # mounts its vault somewhere else via SYSTEMU_VAULT_DIR, and a remedy that
    # writes to the hardcoded default while the gate reads the configured
    # location is exactly the kind of "declared but ineffective" path this
    # layer exists to catch.
    env = _headless_env(workdir / "mounted" / "vault")
    bindings = {
        "<OPENROUTER_KEY>": "sk-or-v1-headless-fence-placeholder-not-a-real-key",
        "<NAME>": "Headless Bob",
        "<PATH>": str(out_dir),
    }

    before = _status(workdir, env)
    assert before["ready"] is False, (
        "a scratch vault should not already be ready — the test proves nothing")

    for check_id, remedy in HEADLESS_REMEDIES.items():
        args = _bind(remedy["argv"], bindings)
        r = _run(args, workdir, env)
        assert r.returncode == 0, (
            f"declared remedy for {check_id} failed: "
            f"`{' '.join(args)}`\nrc={r.returncode}\n{r.stdout}\n{r.stderr}")
        assert "Aborted" not in (r.stdout + r.stderr), (
            f"{check_id}: remedy aborted on a closed stdin:\n{r.stdout}\n{r.stderr}")
        after = {c["id"]: c for c in _status(workdir, env)["checks"]}
        assert after[check_id]["ok"] is True, (
            f"`{' '.join(args)}` is declared as the headless remedy for "
            f"{check_id}, ran successfully, and the gate is STILL not "
            f"satisfied: {after[check_id]}")

    final = _status(workdir, env)
    unmet = sorted(c["id"] for c in final["checks"]
                   if c["required"] and not c["ok"])
    assert not unmet, (
        f"required gate(s) unmet after a complete headless onboarding: {unmet}"
        f"\n{json.dumps(final, indent=2)}")
    assert final["ready"] is True

    # And the two gates that actually redirect the dashboard are satisfied.
    by_id = {c["id"]: c for c in final["checks"]}
    assert by_id["profile_present"]["ok"] is True
    assert by_id["tour_completed"]["ok"] is True


# ── per-leg regressions (each was a live failure) ────────────────────────────

@pytest.mark.slow
def test_user_init_non_interactive_uses_defaults_without_flags(tmp_path):
    """``--non-interactive`` alone must succeed on defaults, never abort."""
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = _headless_env(workdir / "vault")
    r = _run(["user", "init", "--non-interactive"], workdir, env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "Aborted" not in r.stdout + r.stderr
    r = _run(["user", "show"], workdir, env)
    assert r.returncode == 0
    assert "No profile set" not in r.stdout


@pytest.mark.slow
def test_user_init_headless_without_the_flag_explains_itself(tmp_path):
    """No TTY, no flag: a named, actionable error — not a bare ``Aborted!``."""
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = _headless_env(workdir / "vault")
    r = _run(["user", "init"], workdir, env)
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "--non-interactive" in combined, combined
    assert "Aborted!" not in combined, combined


@pytest.mark.slow
def test_user_set_creates_the_profile_instead_of_refusing(tmp_path):
    """`user set` on a fresh vault must work end to end with no TTY."""
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = _headless_env(workdir / "vault")
    r = _run(["user", "set", "name", "Headless Bob"], workdir, env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "No profile set" not in r.stdout + r.stderr
    r = _run(["user", "show"], workdir, env)
    assert "Headless Bob" in r.stdout, r.stdout


@pytest.mark.slow
def test_complete_tour_is_idempotent(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = _headless_env(workdir / "vault")
    for _ in range(2):
        r = _run(["onboarding", "complete-tour"], workdir, env)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    checks = {c["id"]: c for c in _status(workdir, env)["checks"]}
    assert checks["tour_completed"]["ok"] is True


@pytest.mark.slow
def test_onboarding_status_exit_code_reports_readiness(tmp_path):
    """`onboarding status` is scriptable: non-zero until the install is ready."""
    workdir = tmp_path / "w"
    workdir.mkdir()
    out_dir = workdir / "out"
    env = _headless_env(workdir / "vault")
    r = _run(["onboarding", "status"], workdir, env)
    assert r.returncode != 0, "a fresh install must not report ready"
    assert "onboarding" in (r.stdout + r.stderr).lower()

    _run(["setup", "--key", "sk-or-v1-fence", "--no-validate"], workdir, env)
    _run(["setup", "--output-dir", str(out_dir), "--no-validate"], workdir, env)
    _run(["init"], workdir, env)
    _run(["user", "init", "--non-interactive", "--name", "Bob"], workdir, env)
    _run(["onboarding", "complete-tour"], workdir, env)
    r = _run(["onboarding", "status"], workdir, env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


# ── the interactive wizard must keep working exactly as it did ───────────────

def test_user_init_still_prompts_when_interactive(tmp_path, monkeypatch):
    """A promptable operator gets the identical four-question wizard."""
    from click.testing import CliRunner

    from sharing_on.config import Config
    from systemu.interface.cli_commands import user_group
    from systemu.vault.factory import open_vault

    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    cfg = Config.from_env()
    vault = open_vault(cfg)

    runner = CliRunner()
    result = runner.invoke(
        user_group, ["init"],
        obj={"config": cfg, "vault": vault},
        input="Ada\nLondon, UK\nEurope/London\n" + str(tmp_path / "out") + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "Your name" in result.output
    assert "Where are you?" in result.output
    assert "Your timezone" in result.output
    assert "Default output directory" in result.output
    prof = vault.get_user_profile()
    assert prof is not None and prof.name == "Ada"
    assert prof.location_text == "London, UK"
    assert prof.timezone == "Europe/London"
