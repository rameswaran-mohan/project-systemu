"""Persona-adaptive onboarding content (Charter v2 req 5 - the promised consumer).

`welcome.personas()` has stored a persona fact since W9.1; this registry is the
"starter kits consume it later" the docstring promised. PURE DATA + one reader:
no page logic here, no feature gating anywhere - skins change emphasis, never
capability. DEFAULT_SKIN reproduces pre-registry behavior exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PersonaSkin:
    starters: List[str]
    dare_line: str                 # the forge dare - one per skin, tier-caveated at render
    empty_work: str
    empty_shadows: str
    tour_order: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    preset_hint: str = ""          # one line under the preset select


#: The dare prompt is deliberately a capability the stock toolbox lacks, so it
#: exercises WAITING_ON_TOOLS -> forge offer. Safe, local, reversible.
DARE_PROMPT = ("Create a QR code image named table_link.png that encodes the "
               "path to my output folder")

_NEWS = ("Search the web for today's top 3 news headlines about AI assistants "
         "and summarize them")

DEFAULT_SKIN = PersonaSkin(
    starters=[
        "List the files in my deliverables folder and write a short markdown index of them",
        "Create a CSV named expenses_template.csv with columns Date, Vendor, Amount, Category",
        _NEWS,
    ],
    dare_line="Ask me for something I can't do yet - I'll offer to build the tool.",
    # NOTE: these two mirror today's literals VERBATIM (work.py / army.py) so the
    # no-persona path renders byte-identically to pre-registry behavior. The
    # em-dash is deliberate copy fidelity, not a stray character.
    empty_work="No workflows yet —",
    empty_shadows="No shadows yet — process a scroll to create your first shadow.",
)

PERSONA_CONTENT: Dict[str, PersonaSkin] = {
    "Personal": PersonaSkin(
        starters=[
            "Organize the files in my output folder into a tidy, dated markdown index",
            "Create a packing checklist for a weekend trip as packing_list.md",
            _NEWS,
        ],
        dare_line="Try to stump me: ask for something I have no tool for - I'll build it in front of you.",
        empty_work="Nothing here yet - your first task is one sentence away in Chat.",
        empty_shadows="No shadows yet. Record yourself doing any chore once - it becomes a repeat button.",
        tour_order=[0, 1, 4, 2, 3, 5],   # Build (forge) early: the explorer's aha
    ),
    "Freelance": PersonaSkin(
        starters=[
            "Create a CSV named invoice_template.csv with columns Date, Client, Description, Amount, Status",
            "List the files in my deliverables folder and write a short markdown index of them",
            "Draft a polite payment-reminder email template as reminder.md",
        ],
        dare_line="Missing a tool for your workflow? Ask anyway - I can build what I lack.",
        empty_work="No workflows yet - your first client chore is one sentence away in Chat.",
        empty_shadows="No shadows yet - most freelancers start by recording their invoicing once.",
        tour_order=[0, 1, 2, 3, 4, 5],   # Record/Work early (default order already is)
    ),
    "Solo business": PersonaSkin(
        starters=[
            "Draft a weekly operations checklist as ops_checklist.md",
            "Create a CSV named expenses_template.csv with columns Date, Vendor, Amount, Category",
            "Summarize the files in my output folder into one status brief named brief.md",
        ],
        dare_line="Need a capability I don't have? Ask - building missing tools is the job.",
        empty_work="This becomes your ops board - every task you hand over shows up here with live status.",
        empty_shadows="No shadows yet - record a routine once and it becomes a workflow you re-run.",
        tour_order=[0, 2, 1, 3, 4, 5],   # Work board early: the ops-board aha
    ),
    "Small business team": PersonaSkin(
        starters=[
            "Draft a weekly operations checklist as ops_checklist.md",
            "Create a CSV named meeting_actions.csv with columns Date, Owner, Action, Due",
            "Summarize the files in my output folder into one status brief named brief.md",
        ],
        dare_line="Need a capability I don't have? Ask - building missing tools is the job.",
        empty_work="Every task you hand over shows up here with live status - your working board.",
        empty_shadows="No shadows yet - record a routine once and it becomes a workflow you re-run.",
        tour_order=[0, 2, 1, 3, 4, 5],
    ),
    "Enterprise professional": PersonaSkin(
        starters=[
            "Summarize the documents in my output folder into a one-page brief named brief.md",
            "Create a CSV named meeting_actions.csv with columns Date, Owner, Action, Due",
            _NEWS,
        ],
        dare_line="Ask for a capability I lack - I build tools only with your explicit approval.",
        empty_work="Running and finished work appears here - every action gated, every step visible.",
        empty_shadows="Shadows are workflows learned from your screen. Recording stays off until you start it.",
        tour_order=[0, 3, 1, 2, 4, 5],   # Inbox (control) early: the trust aha
    ),
}


def skin_for(persona: Optional[str]) -> PersonaSkin:
    return PERSONA_CONTENT.get((persona or "").strip(), DEFAULT_SKIN)


def current_persona(vault) -> Optional[str]:
    """Newest 'Usage persona: X' fact, or None. Never raises."""
    try:
        from systemu.runtime.user_profile import get_facts
        facts = get_facts(vault, tags=["persona"])
        for f in reversed(facts):                     # newest-last per get_facts
            text = (f.fact or "").strip()
            if text.startswith("Usage persona:"):
                return text.split(":", 1)[1].strip() or None
    except Exception:
        pass
    return None
