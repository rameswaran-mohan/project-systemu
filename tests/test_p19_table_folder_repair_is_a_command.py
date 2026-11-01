"""A broken folder card's repair action is the ruled CLI command, not a dead link.

THE DEFECT. ``/table`` routed a broken ``data_root`` card's PRIMARY action to
``("/settings", "Re-grant")`` and its removal notice to "manage it in Connections". Neither
page has a folder-grant control, so the operator was sent somewhere they could not act.
The ruled folder-grant surface is the CLI: ``systemu roots grant <path>`` (it explains the
grant and asks y/N, default N) and ``systemu roots revoke <path>``.

THE PROPERTY. A broken / stale folder card shows that exact command, with the card's own
recorded path, as copyable text -- and no link to a page that cannot do it. The removal
notice names the revoke command. A content-derived card does not get its path pre-filled
(§5.10.b#4: never pre-fill a root path that came from content).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shlex
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from systemu.interface.pages import table
from systemu.runtime import table_store as ts

PATH = "D:\\Work Files\\2026"


def _folder_card(status="broken", origin=None):
    item = ts.make_operator_item("data_root", PATH)
    item.status = status
    item.broken_reason = "folder grant revoked" if status == "broken" else ""
    if origin is not None:
        item.origin_class = origin
    return item


# ── the route is gone, the command is there ──────────────────────────────────

@pytest.mark.parametrize("status", ["broken", "stale", "unavailable"])
def test_a_broken_folder_card_has_no_link_to_a_page_without_a_grant_control(status):
    assert table.repair_route("data_root", status) == ("", "")


@pytest.mark.parametrize("status", ["broken", "stale", "unavailable"])
def test_a_broken_folder_card_shows_the_exact_roots_grant_command(status):
    cmd = table.repair_command("data_root", status, {"root_path": PATH}, "operator")
    assert cmd == f'systemu roots grant "{PATH}"'
    # One argument after `grant`, and it is the card's recorded path, spaces and all.
    assert shlex.split(cmd) == ["systemu", "roots", "grant", PATH]


def test_a_healthy_or_declared_folder_card_has_no_repair_command():
    for status in ("ready", "declared", "suggested", "configuring"):
        assert table.repair_command("data_root", status, {"root_path": PATH}) == ""
    assert table.repair_command("mcp_server", "broken", {"server": "x"}) == ""


def test_a_content_derived_folder_path_is_never_pre_filled():
    """§5.10.b#4: the operator types a root path that came from content."""
    cmd = table.repair_command("data_root", "broken", {"root_path": PATH},
                               "content_derived")
    assert PATH not in cmd
    assert cmd == f"systemu roots grant {table.ROOTS_PATH_PLACEHOLDER}"


def test_a_path_with_no_value_never_renders_an_empty_argument():
    assert table.roots_command("grant", "") == \
        f"systemu roots grant {table.ROOTS_PATH_PLACEHOLDER}"
    assert table.roots_command("grant", None) == \
        f"systemu roots grant {table.ROOTS_PATH_PLACEHOLDER}"


def test_the_named_commands_are_real_and_grant_asks_first():
    """Ground truth is the click tree the operator runs, not a string in this file."""
    from sharing_on.cli import cli
    runner = CliRunner()
    grant = runner.invoke(cli, ["roots", "grant", "--help"])
    revoke = runner.invoke(cli, ["roots", "revoke", "--help"])
    listing = runner.invoke(cli, ["roots", "list", "--help"])
    assert grant.exit_code == 0 and revoke.exit_code == 0 and listing.exit_code == 0
    assert "default is no" in grant.output.lower()


# ── the render: copyable text, no dead link ──────────────────────────────────

class _Node:
    def __getattr__(self, name):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingUI:
    def __init__(self, browser_accepts=True):
        self.calls = []
        self.scripts = []
        self.notes = []
        self._accepts = browser_accepts
        self.clipboard = SimpleNamespace(write=lambda text: None)

    def run_javascript(self, code, timeout=1.0):
        self.scripts.append(code)

        async def _result():
            return self._accepts
        return _result()

    def notify(self, msg, **k):
        self.notes.append((msg, k.get("type")))

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def args(self, kind):
        return [a for n, a, _k in self.calls if n == kind]

    def kwargs(self, kind):
        return [k for n, _a, k in self.calls if n == kind]


def _render(monkeypatch, item, browser_accepts=True):
    rec = _RecordingUI(browser_accepts)
    monkeypatch.setattr(table, "ui", rec)
    table._render_card(item)
    return rec


def test_the_broken_folder_card_renders_the_command_and_no_settings_link(monkeypatch):
    """THE REACHABILITY PIN. Restore the old ``/settings`` route, or drop the command
    from ``_render_card``, and this goes red."""
    rec = _render(monkeypatch, _folder_card())
    labels = [str(a[0]) for a in rec.args("label") if a]
    links = rec.args("link")
    assert f'systemu roots grant "{PATH}"' in labels
    assert table.REPAIR_COMMAND_LEAD in labels
    assert not any("/settings" in str(a) for a in links), links


def _press_copy(rec):
    copy_handlers = [k["on_click"] for k in rec.kwargs("button")
                     if "on_click" in k and k.get("icon") == "content_copy"]
    assert len(copy_handlers) == 1
    result = copy_handlers[0]()
    if inspect.isawaitable(result):
        asyncio.run(result)


def test_the_copy_button_copies_exactly_the_command(monkeypatch):
    rec = _render(monkeypatch, _folder_card())
    _press_copy(rec)
    assert len(rec.scripts) == 1
    assert json.dumps(f'systemu roots grant "{PATH}"') in rec.scripts[0]
    assert rec.notes == [(table.REPAIR_COMMAND_COPIED, "positive")]


def test_a_refused_copy_never_says_copied(monkeypatch):
    """THE LIVE-CHECK CATCH. The dashboard's browser refused the clipboard write
    ("Write permission denied") while the card toasted "Command copied." -- a success
    nobody witnessed. The toast now follows the browser's own answer."""
    rec = _render(monkeypatch, _folder_card(), browser_accepts=False)
    _press_copy(rec)
    assert [m for m, _t in rec.notes] == [table.REPAIR_COMMAND_NOT_COPIED]
    assert table.REPAIR_COMMAND_COPIED not in [m for m, _t in rec.notes]


def test_a_content_derived_card_says_why_the_path_is_blank(monkeypatch):
    rec = _render(monkeypatch, _folder_card(origin="content_derived"))
    labels = [str(a[0]) for a in rec.args("label") if a]
    assert table.REPAIR_COMMAND_UNFILLED in labels
    assert not any(PATH in label for label in labels if label.startswith("systemu"))


def test_a_healthy_folder_card_renders_no_command(monkeypatch):
    rec = _render(monkeypatch, _folder_card(status="ready"))
    labels = [str(a[0]) for a in rec.args("label") if a]
    assert not any(label.startswith("systemu roots") for label in labels)


# ── the removal notice ───────────────────────────────────────────────────────

def test_removing_a_folder_card_names_the_revoke_command_not_a_page():
    msg, still_active = table.removal_notice("data_root", PATH)
    assert still_active is True and "still" in msg.lower()
    assert f'systemu roots revoke "{PATH}"' in msg
    assert "systemu roots list" in msg
    assert "Connections" not in msg and "/settings" not in msg
    assert msg.isascii()


def test_the_other_kinds_still_name_their_pages():
    """Only the folder kind changed: the others keep their management surfaces."""
    for kind, where in (("credential_ref", "Credentials"), ("mcp_server", "Connections")):
        msg, still_active = table.removal_notice(kind, "thing")
        assert still_active is True and where in msg
    assert table.repair_route("mcp_server", "broken") == ("Reconnect", "/settings")


def test_every_new_copy_constant_is_ascii():
    for name in ("REPAIR_COMMAND_LEAD", "REPAIR_COMMAND_UNFILLED", "REPAIR_COMMAND_COPIED",
                 "REPAIR_COMMAND_NOT_COPIED", "ROOTS_PATH_PLACEHOLDER"):
        assert getattr(table, name).isascii(), name
