"""— sharing_on skills deprecate CLI."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_deprecate_sets_score_to_zero():
    from sharing_on.cli import cli

    fake_vault = MagicMock()
    fake_skill = MagicMock(
        id="skill_a6f6087b", name="weather_report_creation",
        effectiveness_score=1.0, evolution_history=[],
    )
    fake_vault.get_skill.return_value = fake_skill

    with patch("systemu.vault.factory.open_vault", return_value=fake_vault):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "skills", "deprecate", "skill_a6f6087b",
            "--reason", "gui_codification",
        ])

    assert result.exit_code == 0, result.output
    assert fake_skill.effectiveness_score == 0.0
    assert len(fake_skill.evolution_history) == 1
    assert fake_skill.evolution_history[0]["reason"] == "gui_codification"
    fake_vault.save_skill.assert_called_once()


def test_reactivate_flips_back():
    from sharing_on.cli import cli

    fake_vault = MagicMock()
    fake_skill = MagicMock(
        id="skill_x", name="t",
        effectiveness_score=0.0, evolution_history=[],
    )
    fake_vault.get_skill.return_value = fake_skill

    with patch("systemu.vault.factory.open_vault", return_value=fake_vault):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "skills", "deprecate", "skill_x",
            "--reason", "broken", "--reactivate",
        ])

    assert result.exit_code == 0, result.output
    assert fake_skill.effectiveness_score == 1.0
    assert fake_skill.evolution_history[0]["action"] == "reactivate"


def test_invalid_reason_rejected():
    from sharing_on.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, [
        "skills", "deprecate", "skill_x",
        "--reason", "bogus_reason",
    ])
    assert result.exit_code != 0


def test_missing_skill_exits_nonzero():
    from sharing_on.cli import cli

    fake_vault = MagicMock()
    fake_vault.get_skill.side_effect = KeyError("not found")

    with patch("systemu.vault.factory.open_vault", return_value=fake_vault):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "skills", "deprecate", "skill_unknown",
            "--reason", "outdated",
        ])

    assert result.exit_code != 0
