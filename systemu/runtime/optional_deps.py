"""F21 -- the ONE place that knows which pip extra ships which capability, and
the ONE place that probes whether it is installed.

WHY THIS MODULE EXISTS.  ``pip install systemu`` used to pull 122 wheels and
126 MB, of which ``playwright`` (38.2 MB) and ``nicegui`` (20.3 MB plus ~15
transitive packages) were the two largest -- both paid for by every pure-CLI
user who never opened a browser or a dashboard.  Moving them to extras is the
obvious fix and also the dangerous one: this codebase's signature defect is the
capability that LOOKS present and silently does nothing, and an optional
dependency manufactures exactly that shape unless its absence is carried to
every surface that lists the capability.

THE RULE.  A capability whose optional dependency is absent is UNAVAILABLE,
never broken and never quietly missing:

  * it is still LISTED (never-subtract), flagged unavailable, with the remedy;
  * invoking it returns an actionable refusal naming the literal install
    command -- never an ImportError traceback, never a shrug;
  * ``doctor`` says which groups are missing.

SINGLE PROBE.  Availability enters the process through :func:`is_installed` and
nowhere else.  That is deliberate and load-bearing: it is the only way a fence
can flip the world for every surface at once and prove they all agree, and the
only way a future surface cannot accidentally invent a second, disagreeing
answer.  Do not add ``try: import nicegui`` anywhere.

MEMOISED, PER DISTRIBUTION, FOR THE PROCESS LIFETIME.  :func:`is_installed`
asks ``importlib.metadata`` the FIRST time a given distribution is probed and
remembers that verdict (present or absent) in a module-level memo keyed on the
PEP 503 canonical name; every later call for the same name answers from the
memo.  The memo is never expired by time -- only :func:`clear_probe_cache`
empties it (a test that fakes the metadata tree, or an installer that has just
finished) -- and it holds at most ``_MEMO_CAP`` = 512 names; past that the
probe still answers correctly, it just stops remembering.

This docstring used to claim the probe was not cached at all, and the reason
it gave is still the cost of the memo: a long-lived process that
answered "available" once keeps answering it after the operator uninstalls the
extra, until it is restarted or the memo is cleared.  The memo was taken on
anyway, on measured evidence (D-3, e2e 0.10.31): the walk is ~5 ms per call on
Windows and the dashboard layout asked it 42 times per page paint.  The
staleness is bounded in the direction that matters: every remedy this module
mints is a ``pip install`` run OUTSIDE this process, and the surfaces that
print one already say to restart afterwards.  See :func:`is_installed` for the
full trade.

WHY DISTRIBUTION METADATA AND NOT ``find_spec``.  Tool manifests declare pip
DISTRIBUTION names (``python-docx``, ``pillow``), whose import names differ
(``docx``, ``PIL``).  Probing by import name needs a hand-maintained mapping,
which is precisely the kind of table that drifts silently.  ``importlib.metadata``
is keyed on the same names the manifests and pyproject use, so there is nothing
to keep in sync.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: The distribution name, i.e. the token in ``pip install <dist>[<extra>]``.
#: Must match ``[project] name`` in pyproject -- fenced by
#: ``tests/test_f21_optional_capability_honesty.py``.
DISTRIBUTION = "systemu"

_NORMALISE = re.compile(r"[-_.]+")


def canonical(name: str) -> str:
    """PEP 503 normalisation, so ``Python_DOCX`` and ``python-docx`` are one key."""
    return _NORMALISE.sub("-", str(name or "").strip()).lower()


class OptionalDependencyMissing(RuntimeError):
    """Raised at a use site when a registered optional group is not installed.

    A TYPE, not a message: callers that must translate the refusal into their
    own payload shape (a tool result dict, an HTTP body) need to distinguish
    "this capability was never installed" from "this capability failed", and
    matching on substrings of an ImportError is how that distinction gets lost.
    ``str(exc)`` is always the full actionable sentence, remedy included.
    """

    def __init__(self, message: str, *, packages: Sequence[str] = (),
                 extra: str = "") -> None:
        super().__init__(message)
        self.packages: Tuple[str, ...] = tuple(packages)
        self.extra = extra


@dataclass(frozen=True)
class OptionalGroup:
    """One pip extra and the capability it switches on."""
    extra: str                       # `pip install systemu[<extra>]`
    packages: Tuple[str, ...]        # distribution names, as pyproject spells them
    label: str                       # operator-facing name of the capability
    covers: str                      # what stops working without it
    post_install: str = ""           # a second step, if the wheel is not enough


#: THE REGISTRY.  Adding a group here is what makes a package safe to move out
#: of ``[project] dependencies`` -- ``test_every_seed_tool_dep_is_core_or_a_
#: registered_group`` refuses any dep that is neither.
GROUPS: Tuple[OptionalGroup, ...] = (
    OptionalGroup(
        extra="browser",
        packages=("playwright",),
        label="Browser automation",
        # PRECISE, because this line is the operator's map of what they lose.
        # `web_read` is NOT unavailable without it — its Jina/raw-GET tiers are
        # pure HTTP and it declares no dependency; only its last-resort
        # JS-render escalation needs Chromium. Listing it flatly here would
        # have told an operator a working tool was broken.
        covers="web_act, web_screenshot (and web_read's JS-render tier)",
        # The wheel alone is not enough: playwright ships a driver, not a
        # browser. Naming only the pip line would be the same class of
        # half-truth this module exists to prevent.
        post_install="python -m playwright install chromium",
    ),
    OptionalGroup(
        extra="dashboard",
        packages=("nicegui",),
        label="Web dashboard",
        covers="`systemu daemon start` and the UI on http://localhost:8765",
    ),
    # e2e31. The `[anthropic]` extra had shipped since v0.7-e with no entry
    # here, so `pip install "systemu[anthropic]"` was a working command the
    # product could never print -- and the provider mint, having no remedy to
    # offer, had no reason to look for the package at all. That is the half of
    # the fence pyproject CLAIMED was enforced and was not.
    OptionalGroup(
        extra="anthropic",
        packages=("anthropic",),
        label="Anthropic models",
        # PRECISE, like the browser row above. Without this package an
        # `anthropic/*` tier is NOT necessarily dead: `llm_router.resolve_
        # provider_keyaware` serves the same catalog ids through OpenRouter on
        # a machine that has an OpenRouter key and no Anthropic one. What
        # breaks is the NATIVE path -- the tier the router really sends to
        # Anthropic, which is exactly the machine with ANTHROPIC_API_KEY set.
        covers="tiers the router sends to Anthropic itself (an `anthropic/*` "
               "or `claude-*` model with ANTHROPIC_API_KEY set)",
    ),
)

_BY_PACKAGE: Dict[str, OptionalGroup] = {
    canonical(p): g for g in GROUPS for p in g.packages
}


def group_for_package(package: str) -> Optional[OptionalGroup]:
    """The group that ships ``package``, or None if it is not one of ours."""
    return _BY_PACKAGE.get(canonical(package))


#: The memo below is a RENDER optimisation, not a registry -- an unbounded
#: corpus must not grow an unbounded dict. Past the cap the probe still answers
#: correctly, it just stops remembering.
_MEMO_CAP = 512

#: canonical distribution name -> the probe's verdict for THIS interpreter.
_INSTALLED_MEMO: Dict[str, bool] = {}


def clear_probe_cache() -> None:
    """Forget every memoised availability verdict.

    The seam for the caller that genuinely needs a re-walk: a test that faked
    the metadata tree, or an installer that has just finished.
    """
    _INSTALLED_MEMO.clear()


def is_installed(package: str) -> bool:
    """Is this pip distribution present in the CURRENT interpreter?

    THE SINGLE PROBE. Every availability answer in the product resolves here.
    Fails CLOSED: anything other than a distribution we can positively find is
    reported absent, because "I could not tell" must never render as ready.

    D-3 (e2e 0.10.31): ANSWERED ONCE PER DISTRIBUTION, NOT ONCE PER CALLER.
    ``importlib.metadata.distribution`` walks every ``sys.path`` entry with
    ``listdir`` + ``stat``; profiled on Windows that is ~5 ms, and the dashboard
    layout asked it FORTY-TWO times per page paint (once per tool row, through
    ``capability_index._apply_availability``) -- 218 ms of the event loop, on
    every route. The per-row listing surfaces paid the same toll again.

    The memo is process-lifetime rather than TTL'd on purpose. A TTL bounds how
    OFTEN the walk is paid, not WHERE it is paid, and WHERE is the whole
    finding. The verdict cannot go stale in a direction that matters either:
    every remedy this module mints is a ``pip install`` run OUTSIDE this
    process, and the surfaces that print it already say to restart afterwards.
    """
    key = canonical(package)
    memo = _INSTALLED_MEMO.get(key)
    # DEC-36: pin the concrete type in this frame. A memo slot holding anything
    # but a bool is not a verdict, and must fall through to the real probe.
    if type(memo) is bool:
        return memo
    from importlib import metadata
    try:
        metadata.distribution(key)
        found = True
    except Exception:
        found = False
    if len(_INSTALLED_MEMO) < _MEMO_CAP:
        _INSTALLED_MEMO[key] = found
    return found


def missing_packages(packages: Iterable[str]) -> Tuple[str, ...]:
    """The subset of ``packages`` that is not installed, order preserved."""
    return tuple(p for p in (packages or ()) if not is_installed(p))


def missing_groups(packages: Iterable[str]) -> Tuple[OptionalGroup, ...]:
    """The registered groups covering the missing members of ``packages``.

    Deduplicated, declaration order. Packages that are missing but belong to no
    group are NOT represented here -- they are somebody else's problem (the
    operator dep-approval flow), and pretending otherwise would put a bogus
    ``pip install systemu[...]`` in front of a third-party package.
    """
    out: List[OptionalGroup] = []
    for p in missing_packages(packages):
        g = group_for_package(p)
        if g is not None and g not in out:
            out.append(g)
    return tuple(out)


def install_command(packages: Iterable[str]) -> str:
    """The single copyable line that installs the groups ``packages`` need.

    One ``pip install`` even when two groups are involved, because two lines is
    two chances to run only the first.
    """
    extras = sorted({g.extra for g in missing_groups(packages)}
                    or {g.extra for p in (packages or ())
                        for g in ((group_for_package(p),) if group_for_package(p) else ())})
    if not extras:
        return ""
    # F22: the extras token is QUOTED. Square brackets are glob metacharacters,
    # and zsh — the macOS default shell — refuses `pip install systemu[dashboard]`
    # outright with "no matches found" rather than passing it through the way
    # bash happens to. An unrunnable remedy is no remedy; this is the same defect
    # as the Rich-markup one this module already guards against, from the other
    # side. Quoting is inert in bash/PowerShell/cmd, so one form works everywhere.
    return f'pip install "{DISTRIBUTION}[{",".join(extras)}]"'


def unavailable_reason_parts(packages: Iterable[str]) -> Tuple[str, str, str]:
    """The operator-facing sentence in THREE pieces: ``(lead, command, tail)``.

    ``lead + command + tail`` IS :func:`unavailable_reason`, byte for byte, and
    that is the point: the sentence is authored once, here, and a surface that
    needs to lay the command out separately gets the same words rather than a
    second wording of them.

    F29: why a surface would need that. The whole refusal used to go through one
    Rich ``console.print``, and Rich wraps a paragraph at the terminal width
    wherever the break lands -- on an 80-column terminal the dashboard remedy came
    out as ``Install it \\n with: pip install "systemu[dashboard]"``. A command
    split across a line break is not a command: it cannot be copied, and the half
    that survives a copy runs and does nothing. Same class as the wrapped absolute
    paths the ``roots`` group is line-oriented to avoid.

    ``("", "", "")`` when every group is present -- so the joined form is ``""``
    and the "nothing is missing" answer stays falsy in both shapes.
    """
    groups = missing_groups(packages)
    if not groups:
        return ("", "", "")
    absent = ", ".join(sorted({p for g in groups for p in g.packages}))
    labels = " + ".join(g.label for g in groups)
    lead = (f"UNAVAILABLE - {labels} is not installed ({absent}). "
            f"Install it with: ")
    steps = [g.post_install for g in groups if g.post_install]
    tail = ("  Then: " + "; ".join(steps)) if steps else ""
    return (lead, install_command(packages), tail)


def unavailable_reason(packages: Iterable[str]) -> str:
    """The full operator-facing sentence, or '' when everything is present.

    This exact string is what every listing surface shows and what every
    refusal carries, so there is one wording to get right and one to fix.
    A surface that must keep the remedy command off the wrap path asks for
    :func:`unavailable_reason_parts` instead -- same words, laid out separately.
    """
    return "".join(unavailable_reason_parts(packages))


def require(packages: Iterable[str], *, what: str = "") -> None:
    """Raise :class:`OptionalDependencyMissing` unless every group is present.

    The guard for a use site that is about to import the package. Call it
    BEFORE the import so the operator gets the remedy instead of a
    ``ModuleNotFoundError`` naming a package they never asked for.
    """
    groups = missing_groups(packages)
    if not groups:
        return
    reason = unavailable_reason(packages)
    raise OptionalDependencyMissing(
        f"{what + ': ' if what else ''}{reason}",
        packages=tuple(p for g in groups for p in g.packages),
        extra=",".join(g.extra for g in groups),
    )


def group_status() -> List[dict]:
    """Every registered group with its live installed/missing verdict.

    The shape ``doctor`` and the dashboard health page render. Plain dicts, so
    the report stays JSON-serialisable for ``/health``.
    """
    rows = []
    for g in GROUPS:
        absent = missing_packages(g.packages)
        rows.append({
            "extra": g.extra,
            "label": g.label,
            "covers": g.covers,
            "packages": list(g.packages),
            "missing": list(absent),
            "installed": not absent,
            # F22 + DEC-43: consume the ONE builder rather than re-composing the
            # recipe here. The duplicate was already drifting — it missed the
            # shell-safety quoting the builder needs, so this surface printed a
            # line zsh rejects while the refusal path printed a working one.
            "remedy": "" if not absent else (
                install_command(g.packages)
                + (f"  then: {g.post_install}" if g.post_install else "")
            ),
        })
    return rows
