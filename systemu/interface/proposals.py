"""Derived, never stored growth proposals - the (key, text, route) engine.

TWO CONSUMERS, ONE SHAPE.  Both derive at render time and neither writes
anything to the OnTheTable store, so its sole-writer invariant (DEC-10) is
untouched by either.

  * HOME (:func:`derive_proposal`) - at most ONE per render, over Home's own
    candidates, and a declined key NEVER returns (the decline is a user fact
    tagged ``proposal_declined``).  That contract is unchanged and unwidened.
  * THE TABLE PAGE (:func:`derive_scan_proposals`) - up to three, derived from
    a P2c folder scan.  A scan is SESSION-TRANSIENT, so its proposals are too:
    there is no key to remember a decline against once the result is gone, and
    dismissing one of these cards is a card dismissal for that render only.
    Nothing on this path writes a fact.

Every proposal names an existing surface - no unwired promises.  For the scan
rules that wall is :func:`wishes.ready_tools`, the wishlist's own: a starter is
offered only when the seed tool that would produce its artifact is DEPLOYED and
enabled on THIS install.  That proves the install can land the starter's
output; it does not promise the whole task will succeed, and the copy says
nothing stronger.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

_DECLINE_TAG = "proposal_declined"

#: The workflow statuses that mean "this run FINISHED".  Deliberately the same
#: set ``workflow_tracker._stage_for_activity_status`` maps to the "done" stage,
#: because these are the values the two lanes actually write: the workflow lane
#: stamps ``ActivityStatus.COMPLETED`` ("completed") via
#: ``activity_completion.mark_activity_completed``, and the quick lane stamps
#: "success" (``pipelines/quick_task.py``).  "failed"/"cancelled"/"partial" are
#: terminal too, and are NOT here - "your first task finished" may only be said
#: about a run that actually produced something.
_COMPLETED_STATUSES = {"completed", "done", "success"}


def _declined(vault) -> set:
    """The set of proposal keys the operator has already turned down.

    An unreadable fact store degrades to "nothing declined": the cost is one
    repeated suggestion, never a broken Home page.
    """
    try:
        from systemu.runtime.user_profile import get_facts
        return {f.fact.split(":", 1)[1].strip()
                for f in get_facts(vault, tags=[_DECLINE_TAG])
                if ":" in (f.fact or "")}
    except Exception:
        return set()


def decline(vault, key: str) -> None:
    """Turn a proposal down for good.

    Deliberately NOT guarded: the caller must be able to tell a persisted
    decline from a failed one, so it never reports a preference it did not
    actually save.
    """
    from systemu.runtime.user_profile import add_fact
    add_fact(vault, f"declined:{key}", source="home", tags=[_DECLINE_TAG])


def derive_proposal(vault) -> Optional[Tuple[str, str, str]]:
    """(key, text, action_route) or None. Pure reads; never raises."""
    try:
        declined = _declined(vault)
        shadows = vault.list_shadows() or []
        tools = vault.list_tools() or []
        if not shadows and "first_shadow" not in declined:
            return ("first_shadow",
                    "You haven't recorded a task yet. Record one chore once - "
                    "it becomes a workflow you can re-run forever.",
                    "/chat")
        if shadows and not tools and "first_forge" not in declined:
            return ("first_forge",
                    "Your workflows so far used only built-in abilities. Ask for "
                    "something I can't do yet and watch me build the tool.",
                    "/chat")
        found = _wish_fulfilled(vault, tools, declined)
        if found is not None:
            return found
        found = _first_run_replay(declined)
        if found is not None:
            return found
    except Exception:
        pass
    return None


def _first_run_replay(declined) -> Optional[Tuple[str, str, str]]:
    """2b: after the FIRST task finishes, point at the replay we already have.

    The operator's first completed run is the one moment the machine can show
    its whole reasoning against something they actually asked for - and Work's
    list (and the detail view behind each row) is that surface already.  So this
    is a PROPOSAL, not a new page: no rendering machinery, no store, no writer.

    THE COUNT IS THE POINT.  Exactly one finished run means the first one just
    landed; two means the operator has been here before and does not need to be
    told what a finished task looks like.  Runs still in flight are not counted
    either way, so a second task already executing does not cancel the nudge for
    the first one that finished.

    PRECEDENCE: last, after both starters AND the wish nudge.  Nothing here is
    time-critical, and the earlier proposals are about getting the operator to
    DO something; this one only explains something already done.

    Reads ``work._load_rows()`` - the very loader ``/work`` renders its list
    from (the same reuse ``console._quest_first_task_done`` makes), so this can
    never disagree with what the operator sees when they follow the link.
    """
    key = "first_run_replay"
    if key in declined:
        return None
    from systemu.interface.pages import work
    finished = 0
    for row in work._load_rows() or []:
        status = (row or {}).get("status")
        if type(status) is str and status.lower() in _COMPLETED_STATUSES:
            finished += 1
    if finished != 1:
        return None
    return (key,
            "Your first task finished. See exactly what it did - every stage, "
            "tool and gate.",
            "/work")


def _wish_fulfilled(vault, tools, declined) -> Optional[Tuple[str, str, str]]:
    """The capability-wishlist nudge: the newest open wish a SHIPPED tool now
    covers.  Derived, like everything else here - a wish is a user fact and this
    only reads it.

    PRECEDENCE: after both starter proposals, before the 2b first-run replay
    (which only explains a run that already happened, and can always wait).  A
    cold install should be told to record something before it is told a wish
    came true, and the first-forge nudge cannot compete anyway - it requires an
    EMPTY toolbox while a fulfilled wish requires a deployed tool in it.

    THE HONESTY WALL is `ready_tools` plus the name check below: this may only
    name a tool that exists, is deployed-or-upgraded, is enabled, and has a name
    to print.  Anything else would be promising a build, and v1 builds nothing
    from a wish.
    """
    from systemu.interface.wishes import (
        match_wish, open_wishes, ready_tools, short_wish,
    )
    ready = ready_tools(tools)
    if not ready:
        return None
    for fact_id, text in open_wishes(vault):
        key = f"wish:{fact_id}"
        if key in declined:
            continue
        hit = match_wish(text, ready)
        if not hit:
            continue
        name = str(hit.get("name") or "").strip()
        if not name:
            continue
        return (key,
                f"You wished: '{short_wish(text)}'. The toolbox can now do "
                f"this - {name} is ready.",
                "/tools")
    return None


# --- P2c: proposals derived from a folder scan --------------------------------

#: how many files of one kind a folder must hold before it earns a proposal.
#: Stated as a constant so the threshold is testable and cannot drift silently.
SCAN_MIN_FILES = 3

#: the ceiling on how many cards one scan may produce
SCAN_MAX_PROPOSALS = 3

#: the extensions rule 3 treats as "an image".  A closed list, not a guess -
#: an unknown extension is simply not an image as far as this rule is concerned.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff")

#: the extensions rule 2 treats as "a document"; they are counted TOGETHER, so
#: a folder holding one of each still earns the summarize offer.
_DOCUMENT_EXTENSIONS = (".docx", ".pdf", ".md")


@dataclass(frozen=True)
class ScanRule:
    """One deterministic extension-count rule.  No model runs on this path.

    ``requires`` is the seed tool that produces the starter's artifact - the
    honesty wall.  ``text`` is formatted with ``n`` (what was counted) and
    ``starter`` with ``folder`` (the scanned path), so the prompt the operator
    lands on always names the folder they actually pointed at.
    """

    key: str
    extensions: Tuple[str, ...]
    requires: str
    text: str
    starter: str


#: The rule table, in RENDER ORDER.  Order is fixed here rather than derived
#: from the counts: a card that moves because a folder gained a file is a card
#: the operator has to re-read every time.
SCAN_RULES: Tuple[ScanRule, ...] = (
    ScanRule(
        key="scan:expenses",
        extensions=(".csv",),
        requires="write_csv_file",
        text=("{n} .csv files in that folder. Turn them into one expenses "
              "summary."),
        starter=("Read the CSV files in {folder} and write one combined "
                 "expenses summary as expenses_summary.csv"),
    ),
    ScanRule(
        key="scan:summarize",
        extensions=_DOCUMENT_EXTENSIONS,
        requires="write_markdown_file",
        text=("{n} documents in that folder (.docx, .pdf, .md). Summarize them "
              "into one brief."),
        starter=("Summarize the documents in {folder} into a one-page brief "
                 "named brief.md"),
    ),
    ScanRule(
        key="scan:file_index",
        extensions=_IMAGE_EXTENSIONS,
        requires="file_list_dir",
        text="{n} images in that folder. Write a markdown index of them.",
        starter=("List the image files in {folder} and write a markdown index "
                 "of them as image_index.md"),
    ),
)


def _files_matching(counts: Any, extensions: Tuple[str, ...]) -> int:
    """How many scanned files carry one of ``extensions``.

    Every value is type-pinned before it is added (DEC-36): a count is an
    ``int`` or it is not a count, and nothing here is going to let a value with
    an opinionated ``__radd__`` decide whether a proposal fires.
    """
    total = 0
    for ext in extensions:
        n = counts.get(ext)
        if type(n) is int and n > 0:
            total += n
    return total


def derive_scan_proposals(result: Any,
                          tools: Any) -> List[Tuple[str, str, str]]:
    """Up to three (key, text, route) proposals for a completed folder scan.

    THE INPUT FENCE.  ``result`` must be an actual ``ScanResult`` that says it
    succeeded.  The check is ``type(x) is T`` in this frame (DEC-36) - the only
    type check nothing can dispatch around - and the refusal it enforces is the
    one ``scan_folder`` already returned as a value (DEC-32).  A refused scan,
    or anything merely shaped like a result, proposes nothing at all.

    THE HONESTY WALL.  ``ready_tools`` is applied to the caller's tool index
    exactly as the wishlist nudge applies it; a rule whose ``requires`` tool is
    not deployed-and-enabled is skipped rather than offered.

    Pure: reads two values, writes nothing, and never raises for the page.
    """
    from systemu.interface.components.command_palette import prefill_target
    from systemu.interface.wishes import ready_tools
    from systemu.interface.world_scan import ScanResult

    if type(result) is not ScanResult or result.ok is not True:
        return []
    counts = result.counts
    folder = result.folder
    if type(counts) is not dict or type(folder) is not str or not folder:
        return []

    ready = {str(t.get("name") or "") for t in ready_tools(tools)}
    out: List[Tuple[str, str, str]] = []
    for rule in SCAN_RULES:
        if len(out) >= SCAN_MAX_PROPOSALS:
            break
        n = _files_matching(counts, rule.extensions)
        if n < SCAN_MIN_FILES or rule.requires not in ready:
            continue
        out.append((rule.key,
                    rule.text.format(n=n),
                    prefill_target(rule.starter.format(folder=folder))))
    return out
