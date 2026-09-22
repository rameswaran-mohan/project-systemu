"""Tests for the v0.8.0 CLI wrapper that catches PendingOperatorDecision."""
import click
from click.testing import CliRunner


def test_wrapper_returns_normally_when_no_exception():
    """When work() returns normally, the wrapper returns its value."""
    from systemu.interface.cli_commands import _handle_pending_decision_or_run

    @click.command()
    @click.pass_context
    def dummy(ctx):
        result = _handle_pending_decision_or_run(ctx, lambda: "ok")
        click.echo(f"got:{result}")

    runner = CliRunner()
    res = runner.invoke(dummy)
    assert res.exit_code == 0, res.output
    assert "got:ok" in res.output


def test_wrapper_prints_message_and_exits_75_on_pending():
    """When work() raises PendingOperatorDecision, wrapper prints the queued
    message and exits with code 75 (EX_TEMPFAIL)."""
    from systemu.interface.cli_commands import _handle_pending_decision_or_run
    from systemu.approval.exceptions import PendingOperatorDecision

    @click.command()
    @click.pass_context
    def dummy(ctx):
        def _work():
            raise PendingOperatorDecision(
                decision_id="dec_abc",
                dedup_key="tool_forge:tool_x",
                options=["Skip", "Forge"],
            )
        _handle_pending_decision_or_run(ctx, _work)

    runner = CliRunner()
    res = runner.invoke(dummy)
    assert res.exit_code == 75, f"expected 75, got {res.exit_code}\noutput: {res.output}"
    assert "Queued for operator review" in res.output
    assert "dec_abc" in res.output
    assert "tool_forge:tool_x" in res.output
    assert "Skip" in res.output and "Forge" in res.output
    # F23: the program name is resolved, not spelled. The wheel installs TWO
    # equal console scripts for one entry point, so pinning either literal made
    # the coherence fix look like a regression -- while accepting a name pip
    # never puts on PATH. `<installed program> decisions resolve dec_abc`.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    from pathlib import Path
    _scripts = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml")
        .read_text(encoding="utf-8"))["project"]["scripts"]
    assert any(f"{p} decisions resolve dec_abc" in res.output for p in _scripts), (
        f"the remedy must name an installed console script {sorted(_scripts)}:\n"
        f"{res.output}")


def test_wrapper_lets_other_exceptions_propagate():
    """Wrapper only catches PendingOperatorDecision — other exceptions bubble."""
    from systemu.interface.cli_commands import _handle_pending_decision_or_run

    @click.command()
    @click.pass_context
    def dummy(ctx):
        def _work():
            raise RuntimeError("boom")
        _handle_pending_decision_or_run(ctx, _work)

    runner = CliRunner()
    res = runner.invoke(dummy)
    assert res.exit_code != 0
    assert res.exit_code != 75
    # The exception should appear in the output (Click reports unhandled exceptions)
    assert "boom" in str(res.exception) or "boom" in res.output
