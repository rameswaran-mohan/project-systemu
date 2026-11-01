"""DEC-12 / DEC-20a MODEL-MATRIX — the ``{pipeline_stage -> model_tier}`` map.

The narrative artifact is ``docs/MODEL-MATRIX.md``; THIS module is its
executable half. Before this module existed the three ``Config`` knobs
(``planner_tier`` / ``binder_tier`` / ``parser_tier``) were strings that
``llm_router`` had no way to consult — ``binder_tier`` and ``parser_tier`` had
literally zero consumers, so setting them did nothing at all. The matrix gives
the router a *stage* concept so a call site can say **what kind of work it is**
instead of hard-coding a tier number.

Two vocabularies live here and they are NOT the same thing:

* **tier class** -- which ``Config`` knob carries this stage's tier. This is
  what actually routes. Several stages may share a class (and so a knob).
* **locality** (``cloud_default`` / ``local_capable`` / ``n/a``) -- DEC-20a's
  declaration of which stages a local backend *could* serve first. It is a
  DECLARATION, not a routing input: nothing in this module or in ``llm_router``
  reads ``locality`` to pick a model. Making it route would be Privacy-Complete
  Mode, which DEC-20b flags as a candidate, not a commitment, behind its own
  spec pass. See ``locality_of_stage`` and the doc for the full reasoning.

Note the deliberate asymmetry with ``model_presets.locality_of``: that function
classifies a MODEL ID; ``locality_of_stage`` here classifies a STAGE. They share
a vocabulary and nothing else.

Lives in ``sharing_on`` (not ``systemu``) for the same reason
``model_presets`` does -- the import direction is systemu -> sharing_on, never
the reverse, and ``llm_router`` (systemu/core) must be able to import it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Each tier class and the ``Config`` field that carries its tier.
# ``verifier`` is the odd one out: ``Config.verifier_tier`` is an INT (=3) that
# predates the matrix, while the other knobs are STRINGS ("tier1"). The
# resolver below handles both shapes rather than forcing a config migration.
_CONFIG_FIELD_BY_CLASS: Dict[str, str] = {
    "planner":   "planner_tier",
    "binder":    "binder_tier",
    "parser":    "parser_tier",
    "verifier":  "verifier_tier",
    "reasoning": "reasoning_tier",
    "judge":     "judge_tier",
    "executor":  "executor_tier",
    "forge":     "forge_tier",
}

# The fallback tier when a knob is absent or blank. These mirror the shipped
# ``Config`` defaults exactly -- a missing attribute must not silently change
# which model a stage gets.
_DEFAULT_TIER_BY_CLASS: Dict[str, int] = {
    "planner":   1,   # strongest: plans or decides the course of a run
    "binder":    1,   # strongest: advisory bind-judgment
    "parser":    3,   # cheap: parsers and summarizers (DEC-12)
    "verifier":  3,   # verdicts are deterministic; the LLM only advises
    "reasoning": 1,   # strongest: analysis/generation that informs, not decides
    "judge":     1,   # LLM judges of a draft (scroll, skill, peer verdicts)
    "executor":  2,   # per-step action choice inside a running loop
    "forge":     2,   # tool/skill spec + code generation
}


@dataclass(frozen=True)
class StageRow:
    """One row of the matrix.

    ``wired`` is the honesty field: True means a real call site in this build
    tags this stage, so changing its knob changes which model runs. False means
    the stage is registered and resolvable but NO call site tags it yet --
    setting its knob moves nothing until a call site is wired. ``call_site``
    (plus ``more_call_sites`` for a stage tagged in several files) carries the
    provenance so the claim is checkable, not assertable:
    ``tests/test_model_matrix_coverage.py`` checks that every file listed here
    really tags the stage and that every file tagging it at a router call is
    listed.
    """
    stage: str
    tier_class: str
    locality: str
    wired: bool
    call_site: str
    note: str = ""
    more_call_sites: Tuple[str, ...] = ()

    @property
    def call_sites(self) -> Tuple[str, ...]:
        """Every file that tags this stage (empty for an unwired row)."""
        first = (self.call_site,) if self.call_site else ()
        return first + tuple(self.more_call_sites)


#: The matrix. Stage names are the stable public vocabulary -- a call site
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
        more_call_sites=(
            "systemu/pipelines/activity_extractor.py",
            "systemu/pipelines/quick_task.py",
            "systemu/pipelines/shadow_decision.py",
        ),
        note="Calls that plan or decide the course of a run: open-world "
             "replanning over a SituationReport; activity extraction (decides "
             "the skills/tools an approved scroll needs, plus its empty-result "
             "retry); the quick lane's next-action choice; the shadow "
             "assignment tiebreak (assign an existing shadow or create one).",
    ),
    "refiner": StageRow(
        stage="refiner", tier_class="planner", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/scroll_refiner.py",
        note="Scroll refinement: a capture session or a free-text prompt -> a "
             "Scroll with objectives. All 5 llm_call_json call sites in that "
             "module tag this stage. THREE are on live production paths "
             "(refine_scroll's _call_refine + its GUI-rewrite retry, and "
             "refine_from_text's elder_intake); the other TWO are inside "
             "_refine_with_gui_guard, which nothing in production calls -- "
             "refine_scroll re-implements that guard inline. Those two are "
             "tagged for consistency, but the wired=True claim rests on the "
             "three live ones.",
    ),
    # ---- binder class (advisory bind-judgment) ------------------------------
    "binder_assist": StageRow(
        stage="binder_assist", tier_class="binder", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/fact_extractor.py",
        note="Extracts candidate user facts that the profile binder later "
             "consumes. NOTE: runtime/requirement_binder.py itself makes no "
             "LLM call at all -- it is deterministic -- so this is the only "
             "genuine binder-class LLM stage in the build.",
    ),
    # ---- reasoning class (strongest; informs rather than decides) ----------
    "reasoning": StageRow(
        stage="reasoning", tier_class="reasoning", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/evolution_engine.py",
        more_call_sites=(
            "systemu/pipelines/refinery.py",
            "systemu/pipelines/shadow_decision.py",
            "systemu/pipelines/tool_inadequacy_diagnosis.py",
            "systemu/runtime/auto_skill_extractor.py",
            "systemu/runtime/supervisor.py",
            "systemu/runtime/tools/delegate.py",
        ),
        note="Analysis and generation that inform a decision without making "
             "it: evolution proposals, persona rewrite and wild-card "
             "reflection; execution appraisal; persona generation for a new "
             "shadow; tool-inadequacy diagnosis; skill / anti-pattern "
             "extraction; supervisor failure analysis; a delegated subagent.",
    ),
    # ---- judge class (LLM judges of a draft) --------------------------------
    "judge": StageRow(
        stage="judge", tier_class="judge", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/scroll_validator.py",
        more_call_sites=(
            "systemu/pipelines/skill_validator.py",
            "systemu/runtime/tools/mixture_of_agents.py",
        ),
        note="The scroll satisfiability check, skill validation, and the "
             "mixture-of-agents peer verdicts.",
    ),
    # ---- executor class (per-step action choice in a running loop) ----------
    "executor": StageRow(
        stage="executor", tier_class="executor", locality="cloud_default",
        wired=True, call_site="systemu/runtime/shadow_runtime.py",
        more_call_sites=("systemu/runtime/web/act_loop.py",),
        note="The shadow run loop's per-iteration decision call and the web "
             "act loop's next-action call.",
    ),
    # ---- forge class (tool / skill generation) ------------------------------
    "forge": StageRow(
        stage="forge", tier_class="forge", locality="cloud_default",
        wired=True, call_site="systemu/pipelines/tool_forge.py",
        more_call_sites=(
            "systemu/pipelines/tool_recalibrator.py",
            "systemu/pipelines/skill_recalibrator.py",
            "systemu/pipelines/workshop_module.py",
        ),
        note="Tool spec + code generation (the main forge path's spec and "
             "code calls, code preview, spec proposals, reforge), skill "
             "recalibration and the workshop scroll rebuild. Whether "
             "forge should default to the strongest tier is an OPEN question "
             "(the flash-tier forge-degradation debt); it stays at tier 2, "
             "today's tier, until that is ruled.",
    ),
    # ---- parser class (cheap: parsers and summarizers) ----------------------
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
    "summarize": StageRow(
        stage="summarize", tier_class="parser", locality="local_capable",
        wired=True, call_site="systemu/pipelines/memory_consolidator.py",
        more_call_sites=(
            "systemu/runtime/memory_consolidator.py",
            "systemu/scheduler/jobs.py",
            "systemu/pipelines/refinery.py",
            "systemu/runtime/episodic_memory.py",
            "systemu/pipelines/quick_task.py",
        ),
        note="Summarizers (DEC-12: cheap tier): shadow / global / per-run "
             "memory consolidation, memory-candidate extraction, the episodic "
             "session summary, and the quick lane's grounding-only synthesis. "
             "All but the synthesis ran on tier 1 before this row existed.",
    ),
    "parse": StageRow(
        stage="parse", tier_class="parser", locality="local_capable",
        wired=True, call_site="systemu/pipelines/tool_dry_run.py",
        more_call_sites=("systemu/runtime/context_builder.py",),
        note="Mechanical transforms: dry-run test parameters and the "
             "context-history compaction snapshot.",
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
        wired=True, call_site="systemu/runtime/goal_verifier.py",
        more_call_sites=(
            "systemu/runtime/objective_verifier.py",
            "systemu/runtime/harness_judge.py",
            "systemu/runtime/coach.py",
        ),
        note="The advisory LLM verdicts: goal and objective verification, the "
             "harness-request judge and the coach's steer. The knob is the "
             "pre-existing config.verifier_tier, so whatever an operator set "
             "there is unchanged.",
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
    and DEC-20b gates it behind its own spec pass plus per-stage fixture
    evidence.
    """
    return require_stage(stage).locality


def stages_by_locality(locality: str) -> Tuple[str, ...]:
    """Every registered stage carrying *locality* — the PCM scoping query."""
    return tuple(n for n, r in MATRIX.items() if r.locality == locality)
