"""web_act — LLM-driven accessibility-tree interaction loop (v0.8.10).

Snapshot a11y tree → Tier-2 LLM picks one action (CLICK/TYPE/READ/DONE) →
execute via role/ref locator → observe → repeat. max_steps bounded.
Safety: refuses to type into password-named fields."""
from __future__ import annotations

import logging
from typing import Any, Dict, List
from systemu.runtime.web.browser_pool import parse_a11y_snapshot

logger = logging.getLogger(__name__)

_PASSWORD_HINTS = ("password", "passwd", "pwd")


def _plan_next(instruction: str, nodes: List[Dict[str, str]], history: List[Dict],
               config, bridge=None) -> Dict:
    """Ask the Tier-2 LLM for the next action. Returns a dict with 'action'.

    When ``bridge`` is supplied (the sdk.sampling parent-LLM-bridge — the SAME
    mechanism MCP sampling uses, Task #11), the planning call is routed through
    it so no api key need enter a browser subprocess. The default path keeps the
    legacy in-process ``llm_call_json`` behavior byte-for-byte.
    """
    import json
    system = (
        "You drive a web page via its accessibility tree. Given the instruction, "
        "the interactive elements (role/name/ref), and history, return ONE action as JSON: "
        '{"action":"CLICK","ref":"eN"} or {"action":"TYPE","ref":"eN","text":"..."} '
        'or {"action":"READ"} or {"action":"DONE","result":"..."}.'
    )
    user = json.dumps({"instruction": instruction, "elements": nodes, "history": history})

    if bridge is not None:
        raw = bridge(
            [{"role": "system", "content": {"type": "text", "text": system}},
             {"role": "user", "content": {"type": "text", "text": user}}],
            config=config, tier=2,
        )
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"action": "READ"}  # fail-safe: re-observe rather than crash

    from systemu.core.llm_router import llm_call_json
    return llm_call_json(tier=2, system=system, user=user, config=config,
                         temperature=0.1, max_tokens=512)


def run_act_loop(page, instruction: str, max_steps: int = 8, config=None,
                 bridge=None) -> Dict[str, Any]:
    steps: List[Dict] = []
    for _ in range(max_steps):
        raw = page.accessibility_snapshot()
        nodes = parse_a11y_snapshot(raw)
        decision = _plan_next(instruction, nodes, steps, config, bridge=bridge)
        action = (decision.get("action") or "").upper()
        if action == "DONE":
            return {"success": True, "result": decision.get("result", ""), "steps": steps}
        if action == "CLICK":
            ref = decision.get("ref", "")
            page.click_ref(ref); steps.append({"click": ref})
        elif action == "TYPE":
            ref = decision.get("ref", ""); text = decision.get("text", "")
            # password guard: refuse if the node name looks like a password field
            node = next((n for n in nodes if n["ref"] == ref), None)
            if node and any(h in node["name"].lower() for h in _PASSWORD_HINTS):
                steps.append({"refused_password_type": ref})
            else:
                page.type_ref(ref, text); steps.append({"type": ref})
        elif action == "READ":
            steps.append({"read": page.read_text()[:500]})
        else:
            steps.append({"unknown_action": action})
    return {"success": False, "result": "max_steps reached", "steps": steps}
