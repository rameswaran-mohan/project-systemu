#!/usr/bin/env python3
"""Drive a web page to accomplish an instruction (a11y-tree interaction)."""
from __future__ import annotations

TOOL_META = {"name": "web_act", "tool_type": "browser_action", "dependencies": ["playwright"]}


def run(**kwargs) -> dict:
    url = kwargs.get("url", "")
    instruction = kwargs.get("instruction", "")
    max_steps = int(kwargs.get("max_steps", 8))
    if not url or not instruction:
        return {"success": False, "result": "", "steps": [], "error": "url and instruction required"}
    try:
        from systemu.runtime.web.browser_pool import BrowserPool, is_url_allowed
        from systemu.runtime.web.act_loop import run_act_loop
        if not is_url_allowed(url):
            return {"success": False, "result": "", "steps": [], "error": f"URL blocked by domain policy: {url}"}
        pool = BrowserPool.get()
        pool._ensure_browser()
        ctx = pool._browser.new_context()
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=20000)
            adapter = _PageAdapter(page)
            out = run_act_loop(adapter, instruction, max_steps=max_steps)
            return {**out, "error": None}
        finally:
            ctx.close()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            return {"success": False, "result": "", "steps": [], "error": "browser not ready (chromium installing)",
                    "error_type": "missing_dependency", "missing_packages": ["playwright-chromium"]}
        return {"success": False, "result": "", "steps": [], "error": str(exc)}


class _PageAdapter:
    """Adapt a Playwright page to the act_loop's expected interface.

    The act_loop snapshots the a11y tree each iteration and assigns deterministic
    refs (e1, e2, ...) via ``parse_a11y_snapshot``. This adapter mirrors that
    numbering so a ref handed back by the LLM resolves to the right role+name,
    then locates the element with Playwright's accessible-role locator.
    """
    def __init__(self, page):
        self._page = page
        self._refs = {}   # ref -> {"role", "name"} for the most recent snapshot

    def accessibility_snapshot(self):
        # Playwright >=1.49 removed page.accessibility; aria_snapshot() yields the
        # computed accessible names — the same names get_by_role(role, name=...)
        # matches in click_ref/type_ref. Parse its YAML into the synthetic a11y
        # tree parse_a11y_snapshot already understands, so no other call site
        # changes. (aria_snapshot walks the DOM in order → stable refs.)
        import re
        from systemu.runtime.web.browser_pool import parse_a11y_snapshot
        try:
            yaml = self._page.locator("body").aria_snapshot() or ""
        except Exception:
            yaml = ""
        children = [
            {"role": m.group(1), "name": m.group(2), "children": []}
            for m in re.finditer(r'-\s+([a-z][a-z0-9-]*)\s+"((?:[^"\\]|\\.)*)"', yaml)
        ]
        snap = {"role": "WebArea", "name": "", "children": children}
        # Rebuild the ref→node map using the same flattening the loop uses.
        self._refs = {n["ref"]: n for n in parse_a11y_snapshot(snap)}
        return snap

    def click_ref(self, ref):
        node = self._refs.get(ref)
        if node:
            self._page.get_by_role(node["role"], name=node["name"]).first.click(timeout=5000)

    def type_ref(self, ref, text):
        node = self._refs.get(ref)
        if node:
            self._page.get_by_role(node["role"], name=node["name"]).first.fill(text, timeout=5000)

    def read_text(self):
        return self._page.inner_text("body")
