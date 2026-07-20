"""DEC-12 / DEC-20a MODEL-MATRIX — the ``{pipeline_stage -> model_tier}`` map.

The narrative artifact is ``docs/MODEL-MATRIX.md``; THIS module is its
executable half. Before this module existed the three ``Config`` knobs
(``planner_tier`` / ``binder_tier`` / ``parser_tier``) were strings that
``llm_router`` had no way to consult — ``binder_tier`` and ``parser_tier`` had
literally zero consumers, so setting them did nothing at all. The matrix gives
the router a *stage* concept so a call site can say **what kind of work it is**
instead of hard-coding a tier number.

Two vocabularies live here and they are NOT the same thing:

* **tier class** (``planner`` / ``binder`` / ``parser`` / ``verifier``) — which
  ``Config`` knob carries this stage's tier. This is what actually routes.
* **locality** (``cloud_default`` / ``local_capable`` / ``n/a``) — DEC-20a's
  declaration of which stages a local backend *could* serve first. It is a
  DECLARATION, not a routing input: nothing in this module or in ``llm_router``
  reads ``locality`` to pick a model. Making it route would be Privacy-Complete
  Mode, which MASTER-SPEC §15.4 marks "FLAGGED, NOT COMMITTED" behind its own
  spec pass. See ``locality_of_stage`` and the doc for the full reasoning.

Note the deliberate asymmetry with ``model_presets.locality_of``: that function
classifies a MODEL ID; ``locality_of_stage`` here classifies a STAGE. They share
a vocabulary and nothing else.

Lives in ``sharing_on`` (not ``systemu``) for the same reason
``model_presets`` does — the import direction is systemu -> sharing_on, never
the reverse, and ``llm_router`` (systemu/core) must be able to import it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# The four tier classes and the ``Config`` field that carries each one's tier.
# ``verifier`` is the odd one out: ``Config.verifier_tier`` is an INT (=3) that
# predates the matrix, while the three R-A10 knobs are STRINGS ("tier1"). The
# resolver below handles both shapes rather than forcing a config migration.
_CONFIG_FIELD_BY_CLASS: Dict[str, str] = {
    "planner":  "planner_tier",
    "binder":   "binder_tier",
    "parser":   "parser_tier",
    "verifier": "verifier_tier",
}

# The fallback tier when a knob is absent or blank. These mirror the shipped
# ``Config`` defaults exactly — a missing attribute must not silently change
# which model a stage gets.
_DEFAULT_TIER_BY_CLASS: Dict[str, int] = {
    "planner":  1,   # deepest reasoning — open-world planning
    "binder":   1,   # advisory bind-judgment
    "parser":   3,   # mechanical schema-shaped transforms
    "verifier": 3,   # deterministic verdict; LLM is advisory-only
}


@dataclass(frozen=True)
class StageRow:
    """One row of the matrix.

    ``wired`` is the honesty field: True means a real call site in this build
    tags this stage, so changing its knob changes which model runs. False means
    the stage is registered and resolvable but NO call site tags it yet —
    setting its knob moves nothing until a call site is wired. ``call_site``
    carries the provenance so the claim is checkable, not assertable.
    """
    stage: str
    tier_class: str
    locality: str
    wired: bool
    call_site: str
    note: str = ""


#: The matrix. Stage names are the stable public vocabulary — a call site
#: passes one of these to ``llm_router``'s ``stage=`` parameter.
#:
#: Locality per DEC-20a: parse-class stages are ``local_capable`` (the stages a
#: local backend could serve first); planner/binder-class are ``cloud_default``
#: and may only be re-marked after a local model passes that stage's full
#: fixture set; verification is deterministic so locality is ``n/a`` by
#: construction.
MATRIX: Dict[str, StageRow] = {
    # ---- planner class (strongest configured) -------------------------------
    "planner": StageRow(
        stage="planner", tier_class="planner", locality="cloud_default",
        wired=True, call_site="systemu/runtime/open_world_planner.py",
        note="Open-world replanning over a SituationReport.",
    ),
    "refiner": StageRow(
        stage="refiner", tier_class="planner", locality="cloud_default",
        wired=False, call_site="",
        note="Scroll refinement. systemu/pipelines/scroll_refiner.py hard-codes "
             "tier=1 at 5 call sites; none tags this stage yet.",
    ),
    # ---- binder class (advisory bind-judgment) ------------------------------
    "binder_assist": StageRow(
        stage="binder_assist", tier_class="binder", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/fact_extractor.py",
        note="Extracts candidate user facts that the profile binder later "
             "consumes. NOTE: runtime/requirement_binder.py itself makes no "
             "LLM call at all — it is deterministic — so this is the only "
             "genuine binder-class LLM stage in the build.",
    ),
    # ---- parser class (mechanical, schema-shaped) ---------------------------
    "consult_parse": StageRow(
        stage="consult_parse", tier_class="parser", locality="local_capable",
        wired=True, call_site="systemu/runtime/table_consult.py",
        note="Free-text consult answer -> named items. Falls back to a "
             "deterministic split when the call fails.",
    ),
    "desk_extraction": StageRow(
        stage="desk_extraction", tier_class="parser", locality="local_capable",
        wired=True, call_site="systemu/runtime/extractor.py",
        note="Schema-validated record extraction from UNTRUSTED page text.",
    ),
    "brief_phrasing": StageRow(
        stage="brief_phrasing", tier_class="parser", locality="local_capable",
        wired=False, call_site="",
        note="Named by DEC-20a; no call site tags it in this build.",
    ),
    "router_suggestion": StageRow(
        stage="router_suggestion", tier_class="parser", locality="local_capable",
        wired=False, call_site="",
        note="Named by DEC-20a; no call site tags it in this build.",
    ),
    "slot_canonicalization": StageRow(
        stage="slot_canonicalization", tier_class="parser", locality="local_capable",
        wired=False, call_site="",
        note="Named by DEC-20a; no call site tags it in this build.",
    ),
    # ---- verifier class (deterministic; LLM advisory-only) ------------------
    "verification": StageRow(
        stage="verification", tier_class="verifier", locality="n/a",
        wired=False, call_site="",
        note="goal_verifier / objective_verifier / harness_judge / coach already "
             "read config.verifier_tier directly (predates the matrix). "
             "Registered so the class is nameable; those call sites are NOT "
             "re-routed through stage= in this slice.",
    ),
}


def registered_stages() -> Tuple[str, ...]:
    """Every stage name the router will accept, in matrix order."""
    return tuple(MATRIX.keys())


def wired_stages() -> Tuple[str, ...]:
    """The stages an actual call site tags in this build.

    The complement (``registered - wired``) resolves correctly but moves
    nothing, because no call site asks for it yet.
    """
    return tuple(name for name, row in MATRIX.items() if row.wired)


def require_stage(stage: str) -> StageRow:
    """Return the row for *stage*, or raise ``ValueError``.

    Deliberately raises rather than falling back to a default tier: a typo'd or
    unregistered stage name must be a loud failure, never a silent route to
    some other model. Silently accepting a stage and routing it somewhere else
    is precisely the class of bug this module exists to remove.
    """
    row = MATRIX.get(stage)
    if row is None:
        raise ValueError(
            f"unknown MODEL-MATRIX stage {stage!r}; registered stages are: "
            f"{', '.join(registered_stages())}"
        )
    return row


def config_field_for(stage: str) -> str:
    """The ``Config`` attribute name whose value routes *stage*."""
    return _CONFIG_FIELD_BY_CLASS[require_stage(stage).tier_class]


def default_tier_for(stage: str) -> int:
    """The tier *stage* resolves to when its knob is absent or blank."""
    return _DEFAULT_TIER_BY_CLASS[require_stage(stage).tier_class]


def resolve_stage_tier(stage: str, config) -> int:
    """Resolve *stage* to the numeric tier (1/2/3) ``llm_router`` expects.

    Reads the stage's class knob off *config* and maps its label through the
    one shipped string->int idiom (``execution_mind.py``'s
    ``1 if "1" in label else (3 if "3" in label else 2)``), which also handles
    the int-valued ``verifier_tier`` via ``str()``.

    An absent or blank knob yields the class default — NOT tier 2, which is
    what the bare idiom would produce for an empty string.

    Raises ``ValueError`` for an unregistered stage.
    """
    row = require_stage(stage)
    raw = getattr(config, _CONFIG_FIELD_BY_CLASS[row.tier_class], None)
    label = "" if raw is None else str(raw).strip()
    if not label:
        return _DEFAULT_TIER_BY_CLASS[row.tier_class]
    return 1 if "1" in label else (3 if "3" in label else 2)


def locality_of_stage(stage: str) -> str:
    """The DEC-20a locality DECLARATION for *stage*.

    ``local_capable`` | ``cloud_default`` | ``n/a``.

    This value participates in NO routing decision anywhere in the build, by
    design. It exists so a future Privacy-Complete Mode can be built without
    re-auditing every call site, and so the privacy page can render the current
    reality. If you are reaching for this to pick a model, stop — that is PCM,
    and MASTER-SPEC §15.4 gates it behind its own spec pass plus per-stage
    fixture evidence.
    """
    return require_stage(stage).locality


def stages_by_locality(locality: str) -> Tuple[str, ...]:
    """Every registered stage carrying *locality* — the PCM scoping query."""
    return tuple(n for n, r in MATRIX.items() if r.locality == locality)
