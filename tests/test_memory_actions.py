"""P11: the ONE off-loop consolidation action — pure result-logic tests.

The threading + ui.timer marshal-back shell (`_run_off_loop`/`run_*_async`) is
a thin NiceGUI wrapper; the decision logic lives in the pure
`consolidate_*_result` functions, tested here without NiceGUI or threads.
"""
from unittest.mock import MagicMock

import systemu.scheduler.jobs as jobs
from systemu.interface.memory_actions import (
    consolidate_all_result,
    consolidate_one_result,
)


def test_all_result_positive_when_updated(monkeypatch):
    monkeypatch.setattr(jobs, "run_consolidation_for_all", lambda c, v: 3)
    typ, msg = consolidate_all_result(MagicMock(), MagicMock())
    assert typ == "positive" and "3 shadow" in msg


def test_all_result_info_when_none(monkeypatch):
    monkeypatch.setattr(jobs, "run_consolidation_for_all", lambda c, v: 0)
    typ, msg = consolidate_all_result(MagicMock(), MagicMock())
    assert typ == "info"


def test_one_result_valid_saves_and_clears(monkeypatch):
    monkeypatch.setattr(jobs, "_consolidate_one",
                        lambda sh, md, buf, cfg: "---\nname: x\n---\nbody")
    monkeypatch.setattr(jobs, "_graduate_memory_to_skills", lambda sh, md, v: None)
    vault = MagicMock()
    shadow = MagicMock(); shadow.name = "alpha"
    typ, msg = consolidate_one_result(shadow, "old md", [{"x": 1}], MagicMock(), vault, "sh_1")
    assert typ == "positive" and "alpha" in msg
    vault.save_shadow_memory.assert_called_once()
    vault.clear_memory_buffer.assert_called_once_with("sh_1")


def test_one_result_invalid_llm_output_leaves_buffer_intact(monkeypatch):
    # No leading '---' frontmatter → invalid → buffer must NOT be cleared.
    monkeypatch.setattr(jobs, "_consolidate_one", lambda sh, md, buf, cfg: "garbage no frontmatter")
    vault = MagicMock()
    shadow = MagicMock(); shadow.name = "beta"
    typ, msg = consolidate_one_result(shadow, "old md", [{"x": 1}], MagicMock(), vault, "sh_2")
    assert typ == "negative"
    vault.save_shadow_memory.assert_not_called()
    vault.clear_memory_buffer.assert_not_called()


def test_one_result_empty_llm_output_is_invalid(monkeypatch):
    monkeypatch.setattr(jobs, "_consolidate_one", lambda sh, md, buf, cfg: "")
    vault = MagicMock()
    typ, _ = consolidate_one_result(MagicMock(), "md", [{"x": 1}], MagicMock(), vault, "sh_3")
    assert typ == "negative"
    vault.clear_memory_buffer.assert_not_called()


def test_one_result_graduation_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(jobs, "_consolidate_one",
                        lambda sh, md, buf, cfg: "---\nok\n---\n")
    def _boom(sh, md, v):
        raise RuntimeError("graduation blew up")
    monkeypatch.setattr(jobs, "_graduate_memory_to_skills", _boom)
    vault = MagicMock()
    shadow = MagicMock(); shadow.name = "gamma"
    typ, _ = consolidate_one_result(shadow, "md", [{"x": 1}], MagicMock(), vault, "sh_4")
    assert typ == "positive"            # still saved+cleared despite graduation error
    vault.save_shadow_memory.assert_called_once()
    vault.clear_memory_buffer.assert_called_once_with("sh_4")
