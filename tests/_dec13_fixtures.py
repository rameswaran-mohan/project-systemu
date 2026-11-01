"""Shared fixtures for the DEC-13 badge / UX-10 receipt tests.

* :class:`FakeUI` -- records what a renderer emitted (the ``_ui`` test seam the
  shared component takes), so a render is asserted without a NiceGUI client.
* :data:`CASES` -- the fixture matrix every surface is walked over:
  verified / claimed / no evidence / measured / failed / handoff / unreadable.
* :func:`plant` -- writes a case's receipts where the production default reads
  them (the audit data root is pointed at ``tmp_path`` by the caller).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


class _El:
    def __init__(self, ui: "FakeUI", kind: str, text: Any = None, **kw):
        self.ui = ui
        self.rec: Dict[str, Any] = {"kind": kind, "text": text, "classes": "",
                                    "props": "", "tooltip": None, **kw}
        ui.log.append(self.rec)

    def classes(self, c):
        self.rec["classes"] = (self.rec["classes"] + " " + c).strip()
        return self

    def props(self, p):
        self.rec["props"] = (self.rec["props"] + " " + p).strip()
        return self

    def tooltip(self, t):
        self.rec["tooltip"] = t
        return self

    def style(self, _s):
        return self

    def on(self, *_a, **_k):
        return self

    def open(self):
        self.rec["opened"] = True

    def close(self, *_a):
        self.rec["closed"] = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUI:
    """Records (kind, text, classes, icon, ...) for every element emitted."""

    def __init__(self):
        self.log: List[Dict[str, Any]] = []

    def row(self):
        return _El(self, "row")

    def column(self):
        return _El(self, "column")

    def element(self, tag="div"):
        return _El(self, "element", tag)

    def card(self):
        return _El(self, "card")

    def dialog(self):
        return _El(self, "dialog")

    def label(self, text=""):
        return _El(self, "label", text)

    def icon(self, name, **_k):
        return _El(self, "icon", name)

    def expansion(self, text="", icon=None):
        return _El(self, "expansion", text, icon=icon)

    def button(self, text="", *, on_click=None, icon=None, **_k):
        return _El(self, "button", text, on_click=on_click, icon=icon)

    # ── queries ──────────────────────────────────────────────────────────────
    def texts(self) -> List[str]:
        return [str(r["text"]) for r in self.log if r["text"] is not None]

    def all_text(self) -> str:
        parts = []
        for r in self.log:
            for k in ("text", "tooltip", "icon"):
                if r.get(k) is not None:
                    parts.append(str(r[k]))
        return "\n".join(parts)

    def buttons(self) -> List[Dict[str, Any]]:
        return [r for r in self.log if r["kind"] == "button"]

    def icons(self) -> List[str]:
        out = [r["text"] for r in self.log if r["kind"] == "icon"]
        out += [r["icon"] for r in self.log if r["kind"] == "button" and r.get("icon")]
        return out


def live(oid, *, confirmed=True, detail=None, method="api_readback",
         stamped_at="2026-10-01T10:00:00Z", shadow=False) -> Dict[str, Any]:
    return {"objective_id": oid, "confirmed": confirmed, "method": method,
            "detail": detail if detail is not None else f"row {oid} created",
            "stamped_at": stamped_at, "shadow": shadow}


#: case id -> (run status, receipts) where receipts is a dict of receipt dicts,
#: ``None`` (no file), or raw bytes (an unreadable file).
CASES: Dict[str, tuple] = {
    "verified": ("success", {"1": live(1)}),
    "claimed": ("success", {"1": live(1, confirmed=False)}),
    "no_evidence": ("success", None),
    "measured": ("success", {"1": live(1, shadow=True)}),
    "failed": ("failed", {"1": live(1)}),
    "handoff": ("pending_decision", {"1": live(1), "2": live(2, confirmed=False)}),
    "unreadable": ("success", b"{not json"),
}

#: The answer each case must mint -- an independent statement of DEC-13, so a
#: mutated mint is caught here, not only by the mint's own tests.
EXPECTED_RUN_STATE = {
    "verified": "verified", "claimed": "claimed", "no_evidence": "claimed",
    "measured": "measured", "failed": "failed", "handoff": "handoff",
    "unreadable": "not_recorded",
}


def plant(root, eid: str, receipts: Any) -> None:
    """Write a case's receipts under ``<root>/audit/exec_<eid>/receipts.json``."""
    if receipts is None:
        return
    target = root / "audit" / f"exec_{eid}" / "receipts.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(receipts, bytes):
        target.write_bytes(receipts)
    else:
        target.write_text(json.dumps(receipts), encoding="utf-8")


def point_audit_root(monkeypatch, root) -> None:
    """Make the production default (``data_dir=None``) read under ``root``."""
    from pathlib import Path

    from systemu.runtime import execution_snapshot

    def _root(explicit=None):
        return Path(explicit) if explicit else Path(root)

    monkeypatch.setattr(execution_snapshot, "audit_data_root", _root)


def first(seq, pred) -> Optional[Any]:
    for x in seq:
        if pred(x):
            return x
    return None
