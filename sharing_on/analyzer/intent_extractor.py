"""Intent extractor (v0.6.0-a, Stage 1).

Pre-pass that runs BEFORE the narrative generator (`generator.py`).  Reads
the raw events + detected steps and emits a structured ``intent.json``
artifact at the session root.

The point: today the downstream pipeline (Scroll → Activity → Tools →
Shadow) infers the user's intent from a click-by-click narrative.  When
the user's captured workflow uses one app to achieve an outcome that
could be achieved better another way (e.g., Snipping Tool + Word to
"document weather" — actually just wants weather data documented), the
downstream LLMs faithfully reproduce the means and miss the end.

This extractor decouples intent from means.  Output schema::

    {
      "intent":           "<outcome, one line, no app/GUI names>",
      "expected_outcome": "<concrete success description>",
      "success_signal":   "<observable proof of completion>",
      "abstracted_steps": ["<outcome-described step>", ...],
      "confidence":       "high" | "medium" | "low"
    }

Best-effort throughout — when the extractor fails or returns
``confidence == "low"``, callers fall back to today's narrative-only
behaviour (no operator card; this stage is read-only background work).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from sharing_on.analyzer.step_detector import Step
from sharing_on.events.models import EventAction, EventCategory
from sharing_on.redactor import redact

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IntentExtraction:
    """Structured intent inferred from a capture session."""

    intent:           str = ""
    expected_outcome: str = ""
    success_signal:   str = ""
    abstracted_steps: List[str] = field(default_factory=list)
    confidence:       str = "low"   # high | medium | low
    error:            Optional[str] = None
    # v0.9.35 (P2): record-time generalization mode + lifted parameters.
    # None/"standard" == today's behaviour (no params, intent unchanged).
    generalization:   str = "standard"   # broad | standard | narrow
    parameters:       List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when downstream pipeline should prefer this over narrative."""
        return self.confidence in ("high", "medium") and bool(self.intent.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def extract_intent(
    *,
    steps: List[Step],
    events: List[Any],
    session_name: str,
    platform_info: str,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    model: Optional[str] = None,
    generalization: str = "standard",
) -> IntentExtraction:
    """Run the intent-extraction LLM pass.

    Returns a structured ``IntentExtraction``.  Never raises — failures
    are returned as ``confidence="low"`` with ``error`` populated so the
    caller can fall back gracefully.
    """
    # Route an unspecified model through the tier/preset system. Production
    # callers pass config.tier2_model; this default keeps standalone/test calls
    # on the shipped tier-2 model instead of a stale hardcoded id.
    if model is None:
        import os as _os
        from sharing_on.model_presets import resolve_preset
        model = resolve_preset(_os.environ)["tier2"]
    mode = _coerce_generalization(generalization)
    if not steps and not events:
        return IntentExtraction(
            confidence="low",
            error="no steps or events to analyse",
            generalization=mode,
        )

    summary = _summarise_events(events, steps)
    abstracted = _abstract_step_titles(steps)

    payload = {
        "session_name": session_name,
        "platform":     platform_info,
        "event_summary": summary,
        "abstracted_step_descriptions": abstracted,
    }

    try:
        prompt = _load_prompt()
    except Exception as exc:
        logger.warning("[IntentExtractor] could not load prompt: %s", exc)
        return IntentExtraction(confidence="low", error=str(exc), generalization=mode)

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt + _mode_directive(mode)},
                {"role": "user",   "content": json.dumps(payload, default=str)},
            ],
            temperature=0.1,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
    except Exception as exc:
        logger.warning("[IntentExtractor] LLM call failed: %s", exc)
        return IntentExtraction(confidence="low", error=str(exc), generalization=mode)

    return _parse_response(raw, generalization=mode)


def write_intent_json(intent: IntentExtraction, session_dir: Path) -> Path:
    """Persist the extraction to ``<session_dir>/intent.json``."""
    target = Path(session_dir) / "intent.json"
    try:
        data = asdict(intent)
        # Drop the transient error field on usable extractions — operators
        # looking at the file want signal, not noise.  Failed/low-confidence
        # extractions keep the error so the failure mode is visible.
        if intent.is_usable:
            data.pop("error", None)
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(
            "[IntentExtractor] wrote %s (confidence=%s)",
            target, intent.confidence,
        )
    except Exception:
        logger.exception("[IntentExtractor] failed to write intent.json")
    return target


def read_intent_json(session_dir: Path) -> Optional[IntentExtraction]:
    """Load a previously-written intent.json; returns None if absent or
    unparseable."""
    target = Path(session_dir) / "intent.json"
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        mode = _coerce_generalization(data.get("generalization"))
        return IntentExtraction(
            intent           = str(data.get("intent", ""))[:500],
            expected_outcome = str(data.get("expected_outcome", ""))[:500],
            success_signal   = str(data.get("success_signal", ""))[:500],
            abstracted_steps = [str(s)[:200] for s in (data.get("abstracted_steps") or [])][:12],
            confidence       = str(data.get("confidence", "low")),
            error            = data.get("error"),
            generalization   = mode,
            parameters       = _normalise_parameters(data.get("parameters"))
                               if mode == "broad"
                               else [],
        )
    except Exception:
        logger.debug("[IntentExtractor] read failed", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Internals

_VALID_GENERALIZATION = ("broad", "standard", "narrow")


def _coerce_generalization(value: Optional[str]) -> str:
    """Validate the mode; anything invalid (incl. None) -> 'standard'."""
    v = (value or "").strip().lower()
    return v if v in _VALID_GENERALIZATION else "standard"


def _mode_directive(mode: str) -> str:
    """Extra system-prompt block for non-standard modes.  STANDARD returns
    '' so the prompt — and therefore the whole LLM payload — is byte-identical
    to pre-v0.9.35 behaviour."""
    if mode == "broad":
        return (
            "\n\n## GENERALIZATION MODE: broad\n"
            "Abstract the `intent` to the bare goal (e.g. \"Buy something "
            "online.\"). LIFT ONLY the few clear WHAT/WHERE specifics that "
            "define the task — the thing acted on and where it was done (e.g. a "
            "product, a destination site/app, a target file or recipient) — "
            "into the `parameters` array, each with the captured value as that "
            "parameter's `default`, and do NOT bake those into the intent text.\n"
            "Do NOT lift incidental values: quantities/counts, timestamps or "
            "dates, individual keystrokes, scroll positions, search-box text, "
            "session ids, or anything the operator would not need to re-specify "
            "to repeat the task.\n"
            "Example — recording: searched \"samsung galaxy s24\" on amazon.com, "
            "opened the product, added to cart, checked out. broad output: "
            "intent \"Buy something online.\"; parameters [{\"name\":\"product\", "
            "\"default\":\"Samsung Galaxy S24\"}, {\"name\":\"site\", "
            "\"default\":\"amazon.com\"}] — NOT the quantity, NOT the search text."
        )
    if mode == "narrow":
        return (
            "\n\n## GENERALIZATION MODE: narrow\n"
            "Keep the task's CATEGORY and location BAKED INTO the intent, but "
            "generalize the exact instance to its kind (an exact model -> its "
            "product category). Return `parameters` as an empty array — nothing "
            "is parameterised.\n"
            "Example — same amazon recording -> intent \"Buy a phone online from "
            "Amazon.\": the category \"phone\" and the site \"Amazon\" are baked "
            "in, while the exact model \"Samsung Galaxy S24\" is generalized to "
            "\"phone\" (not kept verbatim)."
        )
    return ""


_VALID_PARAM_TYPES = ("string", "number", "integer", "boolean")


def _normalise_parameters(raw_params: Any) -> List[Dict[str, Any]]:
    """Coerce the model's `parameters` array into descriptor dicts that
    mirror ScrollParameter.  Drops nameless rows; defaults required=True and
    type='string' per the pinned contract.  Redacts each `default` because
    extract_intent does not otherwise run the redactor (the generator does)."""
    out: List[Dict[str, Any]] = []
    for raw in (raw_params or [])[:12]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        ptype = str(raw.get("type", "string")).strip().lower()
        if ptype not in _VALID_PARAM_TYPES:
            ptype = "string"
        default = raw.get("default")
        if isinstance(default, str):
            default = redact(default)
        param: Dict[str, Any] = {
            "name": name[:64],
            "description": str(raw.get("description", ""))[:300],
            "type": ptype,
            "default": default,
            "salient_kind": (str(raw["salient_kind"])[:32]
                             if raw.get("salient_kind") else None),
            "required": bool(raw.get("required", True)),
        }
        if raw.get("enum"):
            param["enum"] = [str(e)[:120] for e in raw["enum"]][:25]
        if raw.get("format"):
            param["format"] = str(raw["format"])[:32]
        out.append(param)
    return out


def _load_prompt() -> str:
    """Read extract_intent.md from the colocated prompts/ folder."""
    here = Path(__file__).resolve().parent
    return (here / "prompts" / "extract_intent.md").read_text(encoding="utf-8")


def _summarise_events(events: List[Any], steps: List[Step]) -> Dict[str, Any]:
    """Compact event statistics for the LLM payload — keeps token cost
    bounded while preserving signal."""
    apps:    Counter = Counter()
    files_created:   List[str] = []
    files_modified:  List[str] = []
    urls:    List[str] = []
    clipboard_count = 0

    for ev in events or []:
        app = getattr(ev, "application", None)
        if app:
            apps[app] += 1

        action = getattr(ev, "action", None)
        if action == EventAction.FILE_CREATED:
            p = getattr(ev, "file_path", None)
            if p:
                files_created.append(str(p))
        elif action == EventAction.FILE_MODIFIED:
            p = getattr(ev, "file_path", None)
            if p:
                files_modified.append(str(p))

        if getattr(ev, "category", None) == EventCategory.CLIPBOARD:
            clipboard_count += 1

        url = getattr(ev, "url", None) or (getattr(ev, "data", {}) or {}).get("url")
        if url:
            urls.append(str(url))

    # Keep only the most-used apps and dedup file/URL lists; cap sizes.
    top_apps = [a for a, _ in apps.most_common(8)]

    return {
        "applications_used": top_apps,
        "files_created":  _dedup_cap(files_created, cap=10),
        "files_modified": _dedup_cap(files_modified, cap=10),
        "urls_visited":   _dedup_cap(urls, cap=10),
        "clipboard_actions": clipboard_count,
        "step_count":  len(steps or []),
        "total_events": len(events or []),
    }


def _abstract_step_titles(steps: List[Step]) -> List[str]:
    """Produce one short line per detected step.  We deliberately keep app
    hints out where possible — the prompt will further abstract them."""
    out: List[str] = []
    for s in (steps or [])[:20]:
        # Prefer user-provided label, else fall back to primary_app + brief
        # event-type breakdown.  Capped per step.
        if getattr(s, "label", None):
            out.append(f"Step {s.step_number}: {str(s.label)[:140]}")
            continue

        counts = getattr(s, "event_summary", None) or {}
        parts: List[str] = []
        if counts.get("file"):
            parts.append(f"{counts['file']} file ops")
        if counts.get("clipboard"):
            parts.append(f"{counts['clipboard']} clipboard ops")
        if counts.get("process"):
            parts.append(f"{counts['process']} process ops")
        if counts.get("window"):
            parts.append(f"{counts['window']} window switches")

        out.append(
            f"Step {s.step_number}: "
            + (", ".join(parts) if parts else "activity observed")
        )

    return out


def _parse_response(raw: str, *, generalization: str = "standard") -> IntentExtraction:
    """Tolerant JSON parser — strips code fences if the model added them."""
    text = (raw or "").strip()
    if text.startswith("```"):
        # Strip code fence
        lines = text.split("\n")
        # Drop opening fence
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("[IntentExtractor] could not parse model output: %s", exc)
        return IntentExtraction(
            confidence="low", error=f"parse_error: {exc}",
            generalization=generalization,
        )

    if not isinstance(data, dict):
        return IntentExtraction(
            confidence="low", error="response was not a JSON object",
            generalization=generalization,
        )

    params = (
        _normalise_parameters(data.get("parameters"))
        if generalization == "broad"
        else []
    )
    return IntentExtraction(
        intent           = str(data.get("intent", ""))[:500],
        expected_outcome = str(data.get("expected_outcome", ""))[:500],
        success_signal   = str(data.get("success_signal", ""))[:500],
        abstracted_steps = [str(s)[:200] for s in (data.get("abstracted_steps") or [])][:12],
        confidence       = str(data.get("confidence", "low")).lower(),
        generalization   = generalization,
        parameters       = params,
    )


def _dedup_cap(items: List[str], *, cap: int) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= cap:
            break
    return out
