"""R-A13.5 / §10 IMPL-15 — the ASK-side **resolver replay**.

WHAT THIS IS, AND WHAT IT IS NOT
================================
:func:`systemu.runtime.replay_metrics.avoidable_ask_report` is an **aggregator**
over the live ``ask_corpus.jsonl``: it counts asks that recorded no resolution
attempt. Its own docstring calls it a *non-definitive proxy*, and that is
accurate — a runtime corpus row carries no inventory snapshot, so nothing
downstream of it can re-run the resolver. **That module is unchanged by this
one and its proxy remains a proxy.**

This module is the other half §10 asks for: a **deterministic post-hoc replay**.
For each ask in a labelled scenario it *actually re-runs the real resolver* —
:func:`systemu.runtime.requirement_binder.compute_requirements`, the same
function the production call site in ``shadow_runtime`` invokes — over the
scenario's recorded inventory/situation, with the operator's answer known, and
asks: **would a source have bound that answer?**

  * bound, and the bind's value digest equals the answer's  → the ask was
    **AVOIDABLE** (the resolver already held it);
  * the leaf came back ``missing``                          → **NECESSARY**;
  * bound a *different* value                               → **NECESSARY**
    (asking was right — a silent bind would have been wrong);
  * bound with nothing comparable, or no requirement at all → **UNASSESSABLE**
    (kept out of numerator AND denominator, per the forge side's discipline).

That verdict is definitive **over this corpus**. It is not, and does not claim
to be, a measurement of the live ask stream — see "BOUNDARY" below.

HOW THE COMPARISON CAN BE DEFINITIVE AT ALL
===========================================
The binder never stores a bind's resolved value; it stores a keyed, non-
reversible digest (``bound_value_digest`` / ``bound_value_canon_digest``, keyed
per vault — see ``requirement_binder._value_digest``). So the replay does not
read a value out of the binder. It digests the **fixture's** answer with the
**same vault key** via :func:`replay_metrics.value_ref` /
:func:`replay_metrics.canonical_value_ref` and compares digests. Equal digest ⇒
the resolver bound exactly that answer.

This is why a **credential leaf is structurally unassessable**: the binder
refuses to digest a secret-classified leaf at all, so there is nothing to
compare and the verdict is ``unassessable``, never ``necessary``. The fixture
loader enforces the matching rule on the input side — a fixture may not carry a
plaintext answer for a secret-classified ``schema_path`` (see
:func:`_validate_ask`).

BOUNDARY — what this does NOT measure
=====================================
* It measures the corpus in ``fixtures/field/``, **not** production traffic.
  Live ``ask_corpus.jsonl`` rows carry no situation snapshot, so they cannot be
  replayed by this or any other harness without a recorder change. Wiring a
  snapshot recorder into the live ask rail is NOT part of this module.
* A ``rate`` of ``None`` means *no rate is available* — either nothing was
  assessable, or the run was not DEFINITIVE. It is deliberately not ``0.0``:
  ``0.0`` is the healthy "no avoidable asks" reading, and no failure path may
  emit it. ``rate`` is a *derived property* of :class:`ReplayReport`, recomputed
  through ``definitive`` on every read — there is no stored number for a broken
  run to leave behind.
* ``definitive`` is DERIVED FROM INPUTS, never stored and never inferred:
  ``declared > 0 and error_count == 0 and not missing and not extra``. It is a
  ``@property`` precisely so that no report can carry a ``definitive: True`` its
  own inputs contradict, and so a caller cannot fabricate one by handing the
  renderer a bare dict — :func:`format_resolver_replay` takes ONLY a
  :class:`ReplayReport` and raises ``TypeError`` on anything else (``None`` /
  ``{}`` included). ``declared == 0`` — an empty or absent roster — is NEVER
  definitive: with no committed denominator, no rate over the corpus is trustable.
* THE DENOMINATOR COMES FROM A COMMITTED ROSTER, NEVER THE OBSERVED GLOB.
  ``fixtures/field/roster.json`` names the exact set of fixture files the corpus
  must contain. ``missing = declared − observed`` (a fixture the roster names but
  the directory does not hold — deleted, renamed to ``*.json.bak``, or moved into
  a subdirectory the non-recursive glob cannot see) and ``extra = observed −
  declared`` (an unrostered ``*.json`` on disk) are BOTH hard failures that
  suppress ``definitive`` and the rate. This is the whole point: a dropped
  scenario takes its asks out of the denominator, so a completeness gate whose
  denominator was ITSELF the observed set could never see its own shrinkage. The
  earlier version gated on ``not errors and not skipped``, and a fixture renamed
  to ``_x`` slipped through a bare ``continue`` — a true 6/9 = 67% rendered as a
  healthy ``0/3 = 0%`` with ``definitive: True``. Against an independent roster
  that same rename is now BOTH a ``missing`` (``x.json`` is gone) and an
  ``extra`` (``_x.json`` is unrostered); either one alone fails the run.
* Tolerating an ``extra`` is refused on purpose: an unrostered fixture is a
  registration defect, and accepting it would reopen "de-roster a scenario to
  drop it from the denominator" — the same silent-shrinkage hole by another door.
  On a mismatch the harness cannot know which side is authoritative (the roster
  owns the denominator, the fixture set owns content), so it does not pretend to:
  it fails closed and says which files diverged.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Fixture format version this harness understands.
FIXTURE_SCHEMA_VERSION = 1

#: The committed roster (DEC-27) — the corpus's INDEPENDENT denominator. It lives
#: in the corpus directory next to the scenarios, so it matches ``*.json`` and is
#: excluded from the scenario set by this exact name. It is never a scenario and
#: never counted as one.
ROSTER_FILENAME = "roster.json"

#: Verdicts. ``UNASSESSABLE`` is kept out of BOTH the numerator and the
#: denominator so an unmeasurable scenario cannot deflate (or inflate) the rate.
AVOIDABLE = "avoidable"
NECESSARY = "necessary"
UNASSESSABLE = "unassessable"

#: Fine-grained reason codes, one per distinguishable path through the replay.
R_BOUND_THE_ANSWER = "resolver_bound_the_answer"
R_BOUND_NOTHING = "resolver_bound_nothing"
R_BOUND_OTHER_VALUE = "resolver_bound_a_different_value"
R_NO_REQUIREMENT = "no_requirement_emitted_for_path"
R_NO_COMPARABLE = "bind_carries_no_comparable_value"
#: The fixture's answer is a declared stand-in, so it must never be scored against a
#: bind. Split in two so the reason stays TRUE: a stand-in on a secret-classified path
#: is doubly unassessable (the binder also refuses to digest it), while a stand-in on
#: an ordinary path is unassessable only because the fixture said so. Reporting the
#: secret code for an ordinary path would be a false statement in a module whose whole
#: value is that its statements are exact.
R_SECRET = "secret_path_is_never_digested"
R_STANDIN = "answer_is_a_declared_standin"

#: Labels a fixture may declare for an ask. The replay compares its own verdict
#: against this, which is what makes the corpus a regression tripwire rather
#: than a pile of inputs.
VALID_LABELS = frozenset({AVOIDABLE, NECESSARY, UNASSESSABLE})


class FixtureError(ValueError):
    """A fixture is malformed, or violates the never-record-a-credential rule.

    Raised LOUDLY at load time. A bad fixture is never silently skipped: silently
    dropping one would shrink the denominator and move the rate, which is the
    failure mode this metric exists to be trusted against.
    """


# ── the fixture model ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ask:
    """One labelled operator ask inside a scenario."""

    schema_path: str
    ask_class: str
    operator_answer: Optional[str]
    label: str
    why: str = ""
    #: True when ``operator_answer`` is a declared stand-in for a value that must
    #: never be written down (see the secret rule). Such an ask is unassessable.
    answer_is_synthetic: bool = False


@dataclass(frozen=True)
class Scenario:
    """A replayable, labelled field scenario."""

    scenario_id: str
    title: str
    source_path: Path
    objective: Dict[str, Any]
    capability: Dict[str, Any]
    situation: Dict[str, Any]
    asks: Tuple[Ask, ...]
    files: Tuple[Dict[str, Any], ...] = ()
    granted_roots: Tuple[str, ...] = ()
    files_produced: Tuple[str, ...] = ()
    provided_params: Optional[Dict[str, Any]] = None
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AskVerdict:
    """The replay's finding for ONE ask."""

    scenario_id: str
    schema_path: str
    ask_class: str
    verdict: str
    reason: str
    label: str
    #: ``True``/``False`` when the fixture declared a label; the corpus is a
    #: regression tripwire only because this is checked.
    agrees_with_label: bool
    #: Observed binder state, for the operator-facing report.
    bound_state: str = ""
    bound_source: str = ""
    #: ``"exact"`` | ``"canonical"`` | ``""`` — which comparison produced an
    #: ``avoidable`` verdict. A canonical-only match is weaker evidence (the fold
    #: casefolds and folds separators), so the two are never merged.
    match_basis: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    verdicts: Tuple[AskVerdict, ...] = ()
    error: str = ""


# ── loading ──────────────────────────────────────────────────────────────────
def default_corpus_dir() -> Path:
    """``<repo>/fixtures/field`` — the DEC-11 field corpus.

    Overridable with ``SYSTEMU_FIELD_CORPUS`` (used by tests to replay a
    purpose-built corpus without touching the real one).
    """
    env = os.environ.get("SYSTEMU_FIELD_CORPUS", "").strip()
    if env:
        return Path(env)
    # systemu/runtime/resolver_replay.py → <repo>/fixtures/field
    return Path(__file__).resolve().parents[2] / "fixtures" / "field"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise FixtureError(msg)


def _contained(raw_path: str, field_name: str, where: str) -> None:
    """Refuse any scenario path that could resolve outside the replay workdir.

    The one containment rule for every path a fixture declares. A scenario file
    is DATA — it may be authored anywhere and is not trusted to name absolute
    locations, so the harness that materialises it must not be able to touch
    anything outside the throwaway workdir it owns.

    ``{root}`` is the sanctioned way to spell "inside my workdir". It is NOT
    contained "by construction", and an earlier version of this docstring said so
    wrongly: only ``files_produced`` was ever expanded, so ``{root}/out`` reached
    ``files[].path`` and ``granted_roots[]`` verbatim and was joined as a LITERAL
    directory named ``{root}``. That stayed inside the workdir purely because a
    relative join happens to, which is containment by accident, not by rule — and
    the accident does not survive the next fixture author taking the docstring at
    its word. Every consuming site now expands the token through
    :func:`_expanded_within`, which also re-checks the RESOLVED path. So the rule
    is: the raw string is vetted here, the resolved path is vetted there, and one
    idiom means one thing in all three fields.

    Note the check here is on the RAW string, deliberately: expanding first and
    then testing would compare against a path that already had the workdir glued
    on, which is exactly how an absolute entry slips through (``Path(wd) / "C:/x"``
    discards ``wd`` and yields ``C:/x``).
    """
    s = str(raw_path).strip()
    _require(bool(s), f"{where}: {field_name} must not be empty")
    if s.startswith("{root}"):
        rest = s[len("{root}"):].lstrip("/\\")
        _require(".." not in Path(rest).parts if rest else True,
                 f"{where}: {field_name} must not escape the scenario root "
                 f"(got {raw_path!r})")
        return
    _require(not _escapes(s),
             f"{where}: {field_name} must be relative (or {{root}}-prefixed) and "
             f"must not escape the scenario root (got {raw_path!r})")


def _escapes(s: str) -> bool:
    """True if joining ``s`` onto a workdir could land outside it.

    ``Path.is_absolute()`` alone is NOT enough, and the gap is platform-skewed:
    on Windows ``Path("/etc/x").is_absolute()`` is **False** (no drive letter),
    yet ``WindowsPath("D:/wd") / "/etc/x"`` == ``WindowsPath("D:/etc/x")`` — the
    join resets to the drive root and escapes anyway. So a ROOTED-but-driveless
    path has to be refused explicitly, and both path flavours have to be asked,
    because a corpus authored on one OS is replayed on the other.
    """
    if not s:
        return False
    if s[0] in "/\\":                       # '/etc/x', '\\foo', UNC '\\\\host\\share'
        return True
    if PureWindowsPath(s).is_absolute() or PurePosixPath(s).is_absolute():
        return True
    if PureWindowsPath(s).drive:            # drive-relative 'C:foo' resolves off-CWD
        return True
    return ".." in PureWindowsPath(s).parts or ".." in PurePosixPath(s).parts


def _validate_ask(raw: Any, where: str) -> Ask:
    """Build one :class:`Ask`, enforcing the never-record-a-credential rule.

    The rule is enforced with the codebase's CANONICAL secret marker
    (``replay_metrics._is_secret_path`` → ``elicitation.is_secret_field``), not a
    bespoke pattern here — one place decides what "secret" means. A fixture that
    puts a plaintext answer on a secret-classified path is REFUSED; declaring
    ``answer_is_synthetic`` acknowledges a stand-in and makes the ask
    unassessable (which is also what the binder does: it never digests a secret,
    so there is nothing a replay could compare).
    """
    from systemu.runtime.replay_metrics import _is_secret_path

    _require(isinstance(raw, dict), f"{where}: each ask must be an object")
    schema_path = str(raw.get("schema_path", "") or "").strip()
    _require(bool(schema_path), f"{where}: ask needs a non-empty schema_path")
    ask_class = str(raw.get("class", "") or "").strip().lower()
    _require(bool(ask_class), f"{where}: ask {schema_path!r} needs a class")
    label = str(raw.get("label", "") or "").strip().lower()
    _require(label in VALID_LABELS,
             f"{where}: ask {schema_path!r} has label {label!r}; "
             f"expected one of {sorted(VALID_LABELS)}")

    synthetic = raw.get("answer_is_synthetic", False) is True
    answer = raw.get("operator_answer", None)
    if answer is not None and not isinstance(answer, (str, int, float, bool)):
        raise FixtureError(f"{where}: ask {schema_path!r} operator_answer must be a scalar")

    if _is_secret_path(schema_path, ask_class) and answer is not None and not synthetic:
        raise FixtureError(
            f"{where}: ask {schema_path!r} (class={ask_class!r}) is secret-classified "
            "but carries a plaintext operator_answer. A fixture must never record a "
            "credential value: drop the answer, or set answer_is_synthetic=true to "
            "declare a stand-in (the ask then replays as unassessable, which is what "
            "the binder does with a secret leaf anyway)."
        )

    return Ask(schema_path=schema_path, ask_class=ask_class,
               operator_answer=(None if answer is None else answer),
               label=label, why=str(raw.get("why", "") or ""),
               answer_is_synthetic=synthetic)


def load_scenario(path: Path) -> Scenario:
    """Parse + validate ONE fixture. Raises :class:`FixtureError` on any problem."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FixtureError:
        raise
    except Exception as exc:
        raise FixtureError(f"{p.name}: unreadable / invalid JSON — {exc}") from exc

    where = p.name
    _require(isinstance(raw, dict), f"{where}: top level must be an object")
    ver = raw.get("schema_version")
    _require(ver == FIXTURE_SCHEMA_VERSION,
             f"{where}: schema_version {ver!r}, this harness reads "
             f"{FIXTURE_SCHEMA_VERSION}")

    scenario_id = str(raw.get("id", "") or "").strip()
    _require(bool(scenario_id), f"{where}: needs a non-empty id")

    cap = raw.get("capability")
    _require(isinstance(cap, dict) and str(cap.get("name", "")).strip(),
             f"{where}: capability must be an object with a name")
    _require(isinstance(cap.get("parameters_schema"), dict),
             f"{where}: capability.parameters_schema must be an object")

    obj = raw.get("objective")
    _require(isinstance(obj, dict), f"{where}: objective must be an object")

    sit = raw.get("situation")
    _require(isinstance(sit, dict), f"{where}: situation must be an object")

    asks_raw = raw.get("asks")
    _require(isinstance(asks_raw, list) and asks_raw,
             f"{where}: needs a non-empty asks list")
    asks = tuple(_validate_ask(a, where) for a in asks_raw)

    pp = raw.get("provided_params")
    _require(pp is None or isinstance(pp, dict),
             f"{where}: provided_params must be an object or absent")

    files = raw.get("files") or []
    _require(isinstance(files, list), f"{where}: files must be a list")
    for f in files:
        _require(isinstance(f, dict) and str(f.get("path", "")).strip(),
                 f"{where}: each files[] entry needs a path")
        _contained(str(f["path"]), "files[].path", where)

    # EVERY path a scenario declares gets the same containment rule, not just
    # files[]. `granted_roots` is mkdir'd at replay time (:462) — an absolute
    # entry escaped the replay workdir and created a directory anywhere on disk,
    # with ZERO load errors reported. `files_produced` is fed to the binder as
    # "a file this run produced" (source #2), so an unconstrained entry aims the
    # resolver at an arbitrary path. Both were unguarded.
    granted_roots = [str(r) for r in (raw.get("granted_roots") or [])]
    for g in granted_roots:
        _contained(g, "granted_roots[]", where)
    files_produced = [str(r) for r in (raw.get("files_produced") or [])]
    for fp_ in files_produced:
        _contained(fp_, "files_produced[]", where)

    return Scenario(
        scenario_id=scenario_id,
        title=str(raw.get("title", "") or ""),
        source_path=p,
        objective=obj,
        capability=cap,
        situation=sit,
        asks=asks,
        files=tuple(files),
        granted_roots=tuple(granted_roots),
        files_produced=tuple(files_produced),
        provided_params=pp,
        provenance=dict(raw.get("provenance") or {}),
    )


def load_roster(corpus_dir: Path) -> "frozenset[str]":
    """The DEC-27 committed denominator: the exact set of fixture filenames the
    corpus MUST contain, read from ``roster.json``.

    Deliberately INDEPENDENT of what is on disk — that independence IS the whole
    mechanism. A completeness check whose denominator is the observed glob cannot
    detect its own shrinkage (rename a fixture out of the glob and the denominator
    quietly shrinks with it); a check against a committed roster can.
    ``missing``/``extra`` are computed against THIS set, never against the glob.

    Raises :class:`FixtureError` if the roster is absent or malformed — a corpus
    with no committed denominator cannot yield a definitive rate. An EMPTY roster
    (``{"fixtures": []}``) is not an error here: it loads as the empty set, and
    ``declared == 0`` then makes the run non-definitive downstream.
    """
    rp = Path(corpus_dir) / ROSTER_FILENAME
    if not rp.is_file():
        raise FixtureError(
            f"{ROSTER_FILENAME} not found in {corpus_dir}: the corpus has no "
            f"committed denominator, so no rate over it can be definitive")
    try:
        raw = json.loads(rp.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FixtureError(
            f"{ROSTER_FILENAME}: unreadable / invalid JSON — {exc}") from exc
    _require(isinstance(raw, dict),
             f"{ROSTER_FILENAME}: top level must be an object with a 'fixtures' list")
    fixtures = raw.get("fixtures")
    _require(isinstance(fixtures, list),
             f"{ROSTER_FILENAME}: 'fixtures' must be a list of fixture filenames")
    names: List[str] = []
    for n in fixtures:
        s = str(n or "").strip()
        _require(bool(s), f"{ROSTER_FILENAME}: a fixtures[] entry is empty")
        _require(s.endswith(".json") and "/" not in s and "\\" not in s
                 and s != ROSTER_FILENAME,
                 f"{ROSTER_FILENAME}: entry {n!r} must be a bare *.json fixture "
                 f"filename (no path separators, and not {ROSTER_FILENAME} itself)")
        names.append(s)
    dupes = sorted({s for s in names if names.count(s) > 1})
    _require(not dupes, f"{ROSTER_FILENAME}: duplicate roster entries {dupes}")
    return frozenset(names)


def _observed_fixtures(corpus_dir: Path) -> "frozenset[str]":
    """Every ``*.json`` in ``corpus_dir`` that is a scenario CANDIDATE — the whole
    non-recursive glob minus the roster file itself.

    Non-recursive on purpose: a fixture moved into a subdirectory leaves the
    observed set, becomes ``missing`` against the roster, and fails the run. There
    is no ``_``-prefix exemption either: a file the roster does not name is
    ``extra`` whatever it is called, so hiding a fixture behind a leading
    underscore is now CAUGHT, not tolerated as a benign skip.
    """
    return frozenset(
        p.name for p in corpus_dir.glob("*.json") if p.name != ROSTER_FILENAME)


def load_corpus(
    corpus_dir: Optional[Path] = None,
) -> Tuple[List[Scenario], List[str], "frozenset[str]", "frozenset[str]", "frozenset[str]"]:
    """Load every scenario in ``corpus_dir`` and reconcile it against the roster.

    Returns ``(scenarios, errors, declared, missing, extra)``:

    * ``scenarios`` — every observed ``*.json`` (bar the roster) that parsed;
    * ``errors``    — a malformed fixture, an unreadable/absent roster, or a
      duplicate scenario id. A bad fixture is an ERROR STRING, never silently
      skipped and never allowed to abort the whole corpus;
    * ``declared``  — the roster set (the committed denominator);
    * ``missing = declared − observed`` and ``extra = observed − declared`` — the
      two directions of roster/disk drift, BOTH of which fail the run closed.

    ``missing``/``extra`` are kept as typed sets rather than folded into
    ``errors`` so :class:`ReplayReport.definitive` can name them as first-class
    terms and a test can assert each is empty. They are error-CLASS all the same:
    each suppresses ``definitive`` and the rate and drives a non-zero CLI exit.
    Detecting incompleteness never requires knowing WHICH file drifted — one
    non-empty difference is enough — but the names are recorded for the operator.
    """
    d = Path(corpus_dir) if corpus_dir is not None else default_corpus_dir()
    if not d.is_dir():
        return ([], [f"corpus directory not found: {d}"],
                frozenset(), frozenset(), frozenset())

    errors: List[str] = []
    try:
        declared = load_roster(d)
    except FixtureError as exc:
        # No committed denominator: declared collapses to the empty set, which is
        # never definitive, and every observed file falls into `extra` below.
        declared = frozenset()
        errors.append(str(exc))

    observed = _observed_fixtures(d)
    missing = declared - observed
    extra = observed - declared

    scenarios: List[Scenario] = []
    for p in sorted(d.glob("*.json")):
        if p.name == ROSTER_FILENAME:
            continue
        try:
            scenarios.append(load_scenario(p))
        except FixtureError as exc:
            errors.append(str(exc))
        except Exception as exc:          # pragma: no cover - defensive
            errors.append(f"{p.name}: unexpected loader failure — {exc}")
    ids = [s.scenario_id for s in scenarios]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate scenario id: {dup!r}")
    return scenarios, errors, declared, missing, extra


# ── materialisation ──────────────────────────────────────────────────────────
def _expand(node: Any, tokens: Dict[str, str]) -> Any:
    """Recursively expand ``{root}``-style tokens through a loaded fixture.

    Fixtures are checked in, so they must not contain machine-specific absolute
    paths. Every path is written relative to ``{root}`` and expanded here to the
    per-replay temporary directory.
    """
    if isinstance(node, str):
        out = node
        for k, v in tokens.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(node, list):
        return [_expand(x, tokens) for x in node]
    if isinstance(node, dict):
        return {k: _expand(v, tokens) for k, v in node.items()}
    return node


def _expanded_within(workdir: Path, raw: Any, tokens: Dict[str, str],
                     field_name: str, where: str) -> str:
    """Expand ``{root}`` in ``raw`` and REFUSE the result if it escapes ``workdir``.

    The single place a declared path becomes a real one. Before this existed the
    ``{root}`` token meant two different things depending on which field it was
    written in — ``files_produced`` was expanded, ``files[].path`` and
    ``granted_roots[]`` were not — even though one helper (:func:`_contained`)
    validated all three. A ``{root}/out`` in either unexpanded field created a
    LITERAL directory named ``{root}`` and handed ``<workdir>/{root}/out`` to the
    ``GrantedRootsStore`` that resolver source #1 re-gates through, with zero
    load errors and an empty ``ScenarioResult.error``.

    Returns the EXPANDED STRING, not a re-normalised path: callers join it or
    pass it on exactly as they did before, so the only behaviour added here is
    the refusal. Containment is checked on the RESOLVED join because that — not
    the raw string :func:`_contained` already vetted — is what actually gets
    written to.
    """
    expanded = str(_expand(str(raw), tokens))
    base = Path(workdir).resolve()
    try:
        probe = (Path(workdir) / expanded).resolve()
    except (OSError, ValueError) as exc:
        raise FixtureError(
            f"{where}: {field_name} {raw!r} is not a usable path — {exc}") from exc
    if probe != base and base not in probe.parents:
        raise FixtureError(
            f"{where}: {field_name} {raw!r} resolved to {probe}, which is outside "
            f"the replay workdir {base}"
        )
    return expanded


def _materialise(scenario: Scenario, workdir: Path,
                 tokens: Optional[Dict[str, str]] = None) -> None:
    """Create the scenario's declared files under ``workdir``.

    ``tokens`` defaults to the ``{root}`` binding for ``workdir`` so that calling
    this directly cannot accidentally reintroduce the unexpanded behaviour.
    """
    tok = dict(tokens) if tokens is not None else {"root": str(workdir)}
    for spec in scenario.files:
        target = workdir / _expanded_within(
            workdir, spec["path"], tok, "files[].path", scenario.scenario_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if "text" in spec:
            target.write_text(str(spec["text"]), encoding="utf-8")
        else:
            try:
                n = int(spec.get("bytes", 1))
            except (TypeError, ValueError):
                n = 1
            target.write_bytes(b"x" * max(0, n))


# ── the replay itself ────────────────────────────────────────────────────────
def _find_requirement(reqs: List[Any], schema_path: str) -> Optional[Any]:
    """The Requirement for ``schema_path``: exact match first, then a suffix match.

    The suffix fallback exists because a nested leaf's ``schema_path`` is
    dotted/slashed by the walker, and a fixture naming the leaf alone is the
    readable form. Exact wins so an ambiguous suffix can never shadow it.
    """
    want = str(schema_path or "")
    for r in reqs:
        if str(getattr(r, "schema_path", "")) == want:
            return r
    tail = [r for r in reqs
            if str(getattr(r, "schema_path", "")).endswith("." + want)
            or str(getattr(r, "schema_path", "")).endswith("/" + want)]
    return tail[0] if len(tail) == 1 else None


def _classify(req: Any, ask: Ask, vault: Any) -> Tuple[str, str, str]:
    """``(verdict, reason, match_basis)`` for one ask against the REPLAYED requirement.

    ``match_basis`` names WHICH comparison fired — ``"exact"`` or ``"canonical"``.
    That distinction is reported rather than collapsed, for the reason the R-A16 F2
    fix records: the canonical fold is deliberately lossier (it casefolds, and folds
    separators), so a canonical-only confirm is weaker evidence than an exact one. An
    ``exact or canonical`` test whose basis is not surfaced is a single path wearing
    two names — deleting either branch would change nothing observable, and no test
    could hold it in place.
    """
    from systemu.runtime import replay_metrics as rm

    if req is None:
        return UNASSESSABLE, R_NO_REQUIREMENT, ""

    state = str(getattr(req, "state", "") or "")
    if state == "missing":
        # The real resolver, over the real inventory, produced nothing. The ask was
        # necessary. This is the definitive negative, and it needs no value
        # comparison — which is why it is decided BEFORE the secret short-circuit
        # below: a secret leaf the resolver could not bind is still provably a
        # necessary ask.
        return NECESSARY, R_BOUND_NOTHING, ""

    if ask.answer_is_synthetic:
        # A declared stand-in must never be scored against a bind: a stand-in that
        # happened to collide with the binder's candidate would manufacture a FALSE
        # `avoidable` off a value the operator never gave. The reason code names
        # which kind of stand-in it is (see R_SECRET / R_STANDIN).
        from systemu.runtime.replay_metrics import _is_secret_path
        secret = _is_secret_path(ask.schema_path, ask.ask_class)
        return UNASSESSABLE, (R_SECRET if secret else R_STANDIN), ""
    if ask.operator_answer is None:
        return UNASSESSABLE, R_NO_COMPARABLE, ""

    exact = getattr(req, "bound_value_digest", None)
    canon = getattr(req, "bound_value_canon_digest", None)
    if not exact and not canon:
        # A bind that carries NO value (e.g. a profile user_fact names a fact id,
        # not the parameter value; a credential leaf is never digested). There is
        # nothing to compare, so no honest verdict exists.
        return UNASSESSABLE, R_NO_COMPARABLE, ""

    want_exact = rm.value_ref(ask.operator_answer, vault)
    want_canon = rm.canonical_value_ref(ask.operator_answer, vault)
    if exact and want_exact and exact == want_exact:
        return AVOIDABLE, R_BOUND_THE_ANSWER, "exact"
    if canon and want_canon and canon == want_canon:
        return AVOIDABLE, R_BOUND_THE_ANSWER, "canonical"
    return NECESSARY, R_BOUND_OTHER_VALUE, ""


def replay_scenario(scenario: Scenario, workdir: Optional[Path] = None) -> ScenarioResult:
    """Re-run the REAL resolver over ``scenario`` and return a verdict per ask.

    Builds the concrete types production passes: a real
    :class:`systemu.vault.vault.Vault`, a real
    :class:`systemu.runtime.context_builder.ExecutionContext`, a real
    :class:`systemu.core.models.Tool`/``Objective``, and the real
    ``GrantedRootsStore`` that source #1 re-gates through (constructed by the
    binder itself from ``vault.root``, exactly as at the production call site —
    the ctx injection seam is deliberately NOT used).
    """
    from systemu.core.models import Objective, Tool
    from systemu.runtime.context_builder import ExecutionContext
    from systemu.runtime.granted_roots import GrantedRootsStore
    from systemu.runtime.requirement_binder import compute_requirements
    from systemu.vault.vault import Vault

    owned = workdir is None
    wd = Path(workdir) if workdir is not None else Path(
        tempfile.mkdtemp(prefix="systemu_replay_"))
    try:
        tokens = {"root": str(wd)}
        _materialise(scenario, wd, tokens)

        vault = Vault(str(wd / "_vault"))
        granted = GrantedRootsStore(base_dir=vault.root)
        for rel in scenario.granted_roots:
            target = wd / _expanded_within(
                wd, rel, tokens, "granted_roots[]", scenario.scenario_id)
            target.mkdir(parents=True, exist_ok=True)
            granted.grant(str(target))

        situation = _expand(scenario.situation, tokens)
        cap_raw = _expand(scenario.capability, tokens)
        obj_raw = _expand(scenario.objective, tokens)
        pp = _expand(scenario.provided_params, tokens) if scenario.provided_params else None

        capability = Tool(
            id=str(cap_raw.get("id") or ("tool_" + str(cap_raw.get("name")))),
            name=str(cap_raw["name"]),
            description=str(cap_raw.get("description", "") or "replayed capability"),
            tool_type=str(cap_raw.get("tool_type", "python_function")),
            parameters_schema=cap_raw["parameters_schema"],
            effect_tags=list(cap_raw.get("effect_tags") or []),
        )
        objective = Objective(
            id=int(obj_raw.get("id", 1) or 1),
            goal=str(obj_raw.get("goal", "") or ""),
            success_criteria=str(obj_raw.get("success_criteria", "") or ""),
        )

        ctx = ExecutionContext(
            execution_id="replay-" + scenario.scenario_id,
            system_prompt="", scroll_json=[], tool_index=[],
        )
        # Already expanded before this change; now it also gets the containment
        # check the other two fields were missing. The emitted STRING is
        # deliberately unchanged (the expanded form, not a re-normalised one) —
        # the binder digests it and the fixture's `operator_answer` expands the
        # same way, so re-normalising here would move separators on one side of
        # a digest comparison only.
        ctx.files_produced = [
            _expanded_within(wd, f, tokens, "files_produced[]", scenario.scenario_id)
            for f in scenario.files_produced
        ]
        ctx._situation_report = situation

        reqs = compute_requirements(objective, capability, situation, ctx,
                                    provided_params=pp, vault=vault)

        verdicts: List[AskVerdict] = []
        for ask in scenario.asks:
            expanded = Ask(
                schema_path=ask.schema_path, ask_class=ask.ask_class,
                operator_answer=(_expand(ask.operator_answer, tokens)
                                 if isinstance(ask.operator_answer, str)
                                 else ask.operator_answer),
                label=ask.label, why=ask.why,
                answer_is_synthetic=ask.answer_is_synthetic,
            )
            req = _find_requirement(reqs, ask.schema_path)
            verdict, reason, basis = _classify(req, expanded, vault)
            verdicts.append(AskVerdict(
                scenario_id=scenario.scenario_id,
                schema_path=ask.schema_path,
                ask_class=ask.ask_class,
                verdict=verdict,
                reason=reason,
                label=ask.label,
                agrees_with_label=(verdict == ask.label),
                bound_state=str(getattr(req, "state", "") or "") if req is not None else "",
                bound_source=str(getattr(req, "source", "") or "") if req is not None else "",
                match_basis=basis,
            ))
        return ScenarioResult(scenario_id=scenario.scenario_id, verdicts=tuple(verdicts))
    except Exception as exc:
        logger.debug("[resolver-replay] scenario %s failed", scenario.scenario_id,
                     exc_info=True)
        return ScenarioResult(scenario_id=scenario.scenario_id,
                              error=f"{type(exc).__name__}: {exc}")
    finally:
        if owned:
            shutil.rmtree(wd, ignore_errors=True)


# ── the report ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReplayReport:
    """The DEFINITIVE avoidable-ask replay result over one field corpus.

    Everything that decides trust is a DERIVED PROPERTY, never a stored field:
    ``definitive`` and ``rate`` are recomputed from the raw inputs on every read,
    so a report cannot carry a ``definitive: True`` or a ``rate: 0.0`` that its own
    inputs contradict, and no caller can hand-craft one. This is the DEC-27 shape:

        definitive = declared > 0 and error_count == 0 and not missing and not extra

    ``declared`` is the size of the COMMITTED roster (the denominator), NOT the
    number of files observed on disk — that distinction is the whole packet.
    """

    declared: int
    scenarios: int
    avoidable_count: int
    necessary_count: int
    unassessable_count: int
    reasons: Dict[str, int]
    by_class: Dict[str, Dict[str, int]]
    match_bases: Dict[str, int]
    mislabelled: Tuple[Dict[str, str], ...]
    errors: Tuple[str, ...]
    #: ``declared − observed`` — fixtures the roster names but the corpus lacks.
    missing: "frozenset[str]"
    #: ``observed − declared`` — unrostered ``*.json`` on disk (a registration defect).
    extra: "frozenset[str]"
    details: Tuple[Dict[str, Any], ...]

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def assessable(self) -> int:
        # UNASSESSABLE is excluded from BOTH numerator and denominator.
        return self.avoidable_count + self.necessary_count

    @property
    def total_asks(self) -> int:
        return self.assessable + self.unassessable_count

    @property
    def definitive(self) -> bool:
        """DERIVED FROM INPUTS — never stored, never inferred (DEC-27).

        ``declared > 0`` comes first: an empty or absent roster gives no committed
        denominator, so nothing over the corpus is trustable however clean the
        replay looked. ``not missing and not extra`` are named as their own terms
        even though drift also fails the run by other paths, so the property
        re-derives definitiveness straight from the raw sets and cannot be fooled
        by a future edit that forgets to fail somewhere else.
        """
        return (self.declared > 0
                and self.error_count == 0
                and not self.missing
                and not self.extra)

    @property
    def rate(self) -> Optional[float]:
        """``avoidable / assessable`` — or ``None``, NEVER ``0.0``, on any
        non-definitive run or when nothing was assessable.

        ``0.0`` is the HEALTHY "no avoidable asks" reading. Gating the rate behind
        ``definitive`` (itself derived) makes it structurally impossible for a
        failed or incomplete run to reach that value.
        """
        if not self.definitive or not self.assessable:
            return None
        return self.avoidable_count / self.assessable


def resolver_replay_report(corpus_dir: Optional[Path] = None) -> ReplayReport:
    """§10 / IMPL-15 — the **definitive** avoidable-ask rate over the field corpus.

    Returns a :class:`ReplayReport` whose ``definitive`` and ``rate`` are DERIVED
    from its inputs (see the class), so an empty/missing/wholly-broken corpus can
    never read as a healthy ``0.0``.

    ``mislabelled`` lists every ask whose replayed verdict disagrees with the
    fixture's declared label — the corpus's whole value as a regression tripwire,
    surfaced at the top level rather than buried per-scenario.
    """
    scenarios, errors, declared, missing, extra = load_corpus(corpus_dir)
    errors = list(errors)          # own the list; replay-time errors append here
    counts = {AVOIDABLE: 0, NECESSARY: 0, UNASSESSABLE: 0}
    reasons: Dict[str, int] = {}
    by_class: Dict[str, Dict[str, int]] = {}
    match_bases: Dict[str, int] = {}
    mislabelled: List[Dict[str, str]] = []
    details: List[Dict[str, Any]] = []

    for sc in scenarios:
        res = replay_scenario(sc)
        if res.error:
            errors.append(f"{sc.scenario_id}: {res.error}")
            continue
        for v in res.verdicts:
            counts[v.verdict] = counts.get(v.verdict, 0) + 1
            reasons[v.reason] = reasons.get(v.reason, 0) + 1
            b = by_class.setdefault(v.ask_class,
                                    {AVOIDABLE: 0, NECESSARY: 0, UNASSESSABLE: 0})
            b[v.verdict] = b.get(v.verdict, 0) + 1
            if v.match_basis:
                match_bases[v.match_basis] = match_bases.get(v.match_basis, 0) + 1
            if not v.agrees_with_label:
                mislabelled.append({
                    "scenario_id": v.scenario_id, "schema_path": v.schema_path,
                    "declared": v.label, "replayed": v.verdict, "reason": v.reason,
                })
            details.append({
                "scenario_id": v.scenario_id, "schema_path": v.schema_path,
                "class": v.ask_class, "verdict": v.verdict, "reason": v.reason,
                "bound_state": v.bound_state, "bound_source": v.bound_source,
                "match_basis": v.match_basis,
            })

    return ReplayReport(
        # `declared` is the ROSTER size, NOT len(scenarios). The denominator is the
        # committed count, so a corpus that lost a file reads declared > scenarios and
        # is non-definitive — instead of the observed set silently shrinking to match
        # the survivors and calling the smaller ratio complete.
        declared=len(declared),
        scenarios=len(scenarios),
        avoidable_count=counts[AVOIDABLE],
        necessary_count=counts[NECESSARY],
        unassessable_count=counts[UNASSESSABLE],
        reasons=reasons,
        by_class=by_class,
        match_bases=match_bases,
        mislabelled=tuple(mislabelled),
        errors=tuple(errors),
        missing=missing,
        extra=extra,
        details=tuple(details),
    )


def format_resolver_replay(report: "ReplayReport") -> List[str]:
    """Plain report lines for a CLI / debug surface.

    Accepts ONLY a :class:`ReplayReport`. ``None``, ``{}`` or any dict raises
    ``TypeError`` at the boundary — deliberately. The predecessor did
    ``r = report or {}`` and then read ``r.get("complete", ...)``, which let an
    empty/None/legacy dict render AS IF complete: it printed the DEFINITIVE banner
    over nothing. A report's trust lives in its DERIVED ``definitive`` property;
    there is no way to spell that on a bare dict, so a bare dict is refused rather
    than guessed at.
    """
    if not isinstance(report, ReplayReport):
        raise TypeError(
            "format_resolver_replay requires a ReplayReport; got "
            f"{type(report).__name__}. The DEFINITIVE banner may be rendered only "
            "from a report whose derived `definitive` property vouches for it — a "
            "dict (or None) cannot, so it is refused at the boundary.")

    definitive = report.definitive
    rate = report.rate
    lines: List[str] = []
    if not definitive:
        # No percentage at all. A non-definitive run's ratio is not a degraded
        # estimate, it is a different population — rendering it would launder a
        # broken run into a quotable headline number.
        what: List[str] = []
        if report.declared == 0:
            what.append("NO committed roster (roster.json empty or absent) — no "
                        "denominator to be definitive over")
        if report.missing:
            what.append(f"{len(report.missing)} rostered fixture(s) MISSING from "
                        f"the corpus directory")
        if report.extra:
            what.append(f"{len(report.extra)} unrostered fixture(s) present "
                        f"(a registration defect)")
        if report.error_count:
            what.append(f"{report.error_count} scenario/roster error(s)")
        if not what:                      # non-definitive with no attributable cause
            what.append("the corpus did not reconcile against its roster")
        lines.append(
            "Avoidable-ask rate (resolver replay): INCOMPLETE REPLAY — "
            + "; ".join(what)
            + "; NO RATE (this is NOT 0%)"
        )
    elif rate is None:
        lines.append("Avoidable-ask rate (resolver replay): NO ASSESSABLE ASKS "
                     "— no rate (this is NOT 0%)")
    else:
        lines.append(
            f"Avoidable-ask rate (resolver replay): "
            f"{report.avoidable_count}/{report.assessable} = {rate * 100:.0f}%"
        )
    if definitive:
        lines.append(
            "  (DEFINITIVE over the fixtures/field corpus: the real inventory/resolver was"
        )
        lines.append(
            "   re-run per scenario with the operator's answer known — not a proxy. It does"
        )
        lines.append(
            "   NOT measure the live ask stream, whose rows carry no situation snapshot.)"
        )
    else:
        # The word DEFINITIVE is the module's entire claim; it may not appear over a
        # run that failed to reconcile against its committed roster.
        lines.append(
            "  (NOT definitive: the corpus did not match its committed roster, so the"
        )
        lines.append(
            "   surviving asks are a biased subset. Fix the drift/errors below and re-run.)"
        )
    lines.append(
        f"  declared={report.declared} scenarios={report.scenarios} "
        f"necessary={report.necessary_count} "
        f"unassessable={report.unassessable_count} "
        f"(unassessable is excluded from the rate)"
    )
    mb = report.match_bases or {}
    if mb:
        lines.append(
            "  confirm basis: "
            + ", ".join(f"{k}={v}" for k, v in sorted(mb.items()))
            + "  (canonical is the lossier fold — it casefolds and folds separators)"
        )
    mis = report.mislabelled or ()
    if mis:
        lines.append(f"  ⚠ {len(mis)} ask(s) disagree with their fixture label:")
        for m in mis[:10]:
            lines.append(f"      {m['scenario_id']}:{m['schema_path']} "
                         f"declared={m['declared']} replayed={m['replayed']} "
                         f"({m['reason']})")
    # Named, not merely counted — "3 files missing" is not actionable, the names are.
    if report.missing:
        lines.append(
            f"  ⚠ {len(report.missing)} rostered fixture(s) MISSING (named by "
            f"roster.json, absent on disk — their asks are gone from the denominator):"
        )
        for s in sorted(report.missing)[:10]:
            lines.append(f"      {s}")
    if report.extra:
        lines.append(
            f"  ⚠ {len(report.extra)} unrostered fixture(s) present (on disk, not in "
            f"roster.json — de-rostering to shrink the denominator is the very thing "
            f"the roster forbids):"
        )
        for s in sorted(report.extra)[:10]:
            lines.append(f"      {s}")
    errs = report.errors or ()
    if errs:
        lines.append(f"  ⚠ {len(errs)} corpus error(s):")
        for e in errs[:10]:
            lines.append(f"      {e}")
    return lines
