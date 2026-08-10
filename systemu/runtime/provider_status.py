"""THE SOLE MINT of the "provider X is configured" fact for the Settings surface.

WHY THIS MODULE EXISTS (F8)
---------------------------
The Settings → *Provider credentials* table used to decide every row with

    val = (getattr(config, attr, "") or "").strip()
    mark = "OK Set" if val else "Not set"

For the four **key-based** providers that is defensible — the string *is* the
credential. For **Ollama it is not**: Ollama is the one keyless provider, its
config attribute is a *base URL*, and ``Config`` invents
``http://localhost:11434`` for it unconditionally with no operator input at
all. So the Ollama row was green on every machine ever, including one with no
Ollama installed. A sandbox ``.env`` holding only ``OPENROUTER_API_KEY`` still
rendered "Ollama URL: OK Set".

That is DEC-40 (a disclosure must ride a LIVENESS WITNESS, never a
declaration) and DEC-43 form (ii) (derivation from a PROXY instead of the
authority is wrong from birth) — the same shape as the pidfile-instead-of-
socket bug fixed in ``systemu.scheduler.daemon.probe_readiness``, whose
pattern this module follows.

THE PROPERTY
------------
    No provider is disclosed as configured unless the thing that makes it
    usable has actually been OBSERVED — a credential for key-based providers,
    REACHABILITY for a keyless one. Satisfaction is per-provider and declared
    in exactly ONE place.

    …and ONE POLICY, not just one recipe (DEC-43, closed later than the rest):
    a keyless provider that ANSWERS is usable on every surface — boot gate,
    wizard gate, checklist, doctor — whatever a tier currently SELECTS.
    Satisfaction and selection are different facts; ``unusable_selected`` is
    the surface for the second one. When ``any_provider_usable`` made the
    keyless witness conditional on selection, ``daemon start`` booted a machine
    the /welcome wizard then refused. See that function for the full account.

    ...and the SELECTION itself is derived in one place too
    (``routed_tier_providers``). It was not, and the two surfaces that red-flag
    an unrunnable tier disagreed about which tiers those were - see that
    function.

HOW THE SHAPE ENFORCES IT
-------------------------
* ``PROVIDER_SPECS`` is the ONE table. It carries, per provider: display name,
  config attribute, env var, and its SATISFACTION RULE. Adding a provider or
  renaming an env var is a one-line edit that every consumer inherits — the
  duplicate ``_CRED_ROWS`` / ``_prov_to_attr`` pair that used to sit 15 lines
  apart in ``settings.py`` (DEC-43 form (i)) is gone.
* ``ProviderStatus.satisfied`` is a **derived** property over a whitelist of
  states, not a stored field. Nothing — not a caller, not a probe — can mint a
  green verdict out of band, and ``state`` can never drift out of step with
  ``satisfied``.
* An unrecognised rule, a probe that raises, a probe that returns an
  unrecognised verdict, and a timeout all FAIL CLOSED to ``unknown``. Per
  DEC-34 the mint never trusts a verdict token supplied by the thing being
  verified: the probe's return is filtered through
  ``_PROBE_STATES`` before it can influence ``satisfied``.
* Nothing here imports NiceGUI, click or a vault, so the later project-wide
  provider registry can absorb it as-is.

CARE: ``probe_ollama`` is bounded and never raises. A timeout is reported as
``unknown`` — "we do not know" is a different, honest claim from "it is down",
and neither is "configured".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# ── satisfaction rules ──────────────────────────────────────────────────────
RULE_CREDENTIAL = "credential"      # a key-based provider: the string is it
RULE_REACHABILITY = "reachability"  # a keyless provider: it must answer

# ── the states a row can be in ──────────────────────────────────────────────
STATE_SET = "set"                   # credential present
STATE_MISSING = "missing"           # credential absent
STATE_REACHABLE = "reachable"       # the endpoint answered, as itself
STATE_UNREACHABLE = "unreachable"   # we asked and did not get that answer
STATE_UNKNOWN = "unknown"           # we could not ask at all — the third state

# The unreachable/unknown split is "we asked and got no usable answer" vs "we
# could not even ask". A TIMEOUT is UNREACHABLE: on Windows a closed loopback
# port is silently dropped rather than refused (measured: 1.0 s for
# 127.0.0.1, 2.0 s for the dual-stack `localhost` the shipped default uses),
# so filing timeouts under `unknown` would put every machine with no Ollama
# into the muted third state instead of telling the operator it is down.
# UNKNOWN is for a missing/unparseable URL or a provider with no witness at
# all. Neither is ever "configured".

#: The ONLY states that mean "configured". Everything else, including every
#: failure mode, is un-satisfied. This is the whitelist ``satisfied`` reads.
SATISFIED_STATES = frozenset({STATE_SET, STATE_REACHABLE})

#: The only verdicts a reachability probe is allowed to contribute. A probe
#: cannot hand back ``STATE_SET`` (or anything else) and be believed.
_PROBE_STATES = frozenset({STATE_REACHABLE, STATE_UNREACHABLE, STATE_UNKNOWN})

#: The subset of probe verdicts that are an ACTUAL OBSERVATION of the endpoint,
#: and therefore the only ones the short-TTL memo may replay. ``unknown`` is
#: "we did not ask", which is not an observation and is never remembered.
_OBSERVED_STATES = frozenset({STATE_REACHABLE, STATE_UNREACHABLE})

#: Per-ADDRESS, not per-call: a dual-stack hostname is tried on ``::1`` and
#: then ``127.0.0.1``, so the wall-clock bound is roughly twice this. Measured
#: on the shipped default ``http://localhost:11434`` with Ollama genuinely
#: RUNNING: 1.688 s (``::1`` dropped, then IPv4 answers) versus 0.096 s for
#: ``http://127.0.0.1:11434``. That is why callers must keep this off a render
#: path — see ``settings.provider_credentials_card``.
DEFAULT_PROBE_TIMEOUT = 1.0


@dataclass(frozen=True)
class ProviderSpec:
    """One row of THE table: who the provider is and what makes it usable."""

    provider: str   # canonical id, also the tier-dropdown value
    display: str    # operator-facing name
    attr: str       # attribute on sharing_on.config.Config
    env: str        # the .env variable that populates ``attr``
    rule: str       # RULE_CREDENTIAL | RULE_REACHABILITY


#: THE TABLE. One row per provider; the single declaration of the recipe.
PROVIDER_SPECS: Tuple[ProviderSpec, ...] = (
    ProviderSpec("openrouter", "OpenRouter",
                 "openrouter_api_key", "OPENROUTER_API_KEY", RULE_CREDENTIAL),
    ProviderSpec("google", "Google",
                 "google_api_key", "GOOGLE_API_KEY", RULE_CREDENTIAL),
    ProviderSpec("anthropic", "Anthropic",
                 "anthropic_api_key", "ANTHROPIC_API_KEY", RULE_CREDENTIAL),
    ProviderSpec("openai", "OpenAI",
                 "openai_api_key", "OPENAI_API_KEY", RULE_CREDENTIAL),
    # Keyless. A URL is not a credential — this row is decided by a probe.
    ProviderSpec("ollama", "Ollama",
                 "ollama_url", "OLLAMA_URL", RULE_REACHABILITY),
)

SPEC_BY_PROVIDER: Dict[str, ProviderSpec] = {s.provider: s for s in PROVIDER_SPECS}
_SPEC_BY_ATTR: Dict[str, ProviderSpec] = {s.attr: s for s in PROVIDER_SPECS}


@dataclass(frozen=True)
class ProviderStatus:
    """The minted verdict for one provider.

    ``satisfied`` is deliberately NOT a field: it is derived from ``state``, so
    a caller constructing this cannot assert a capability the state does not
    support, and the two can never disagree.
    """

    provider: str
    display: str
    env: str
    rule: str
    state: str
    detail: str

    @property
    def satisfied(self) -> bool:
        """Has the thing that makes this provider usable been OBSERVED?"""
        return self.state in SATISFIED_STATES


# ── the witness for the keyless provider ────────────────────────────────────

def _silent_detail(base: str, timeout: float) -> str:
    return (f"nothing answered at {base} within {timeout:g}s - is "
            f"`ollama serve` running?")


def probe_ollama(url: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Tuple[str, str]:
    """Ask the configured endpoint to identify itself as an Ollama server.

    Returns ``(state, detail)``; NEVER raises and never blocks longer than
    ``timeout``. Mirrors the shipped, proven probe in
    ``systemu.runtime.model_validation._validate_ollama`` (``GET /api/tags``),
    hardened for use on a render path:

    * proxies are bypassed — a loopback probe routed through a corporate proxy
      would answer about the proxy, not about Ollama;
    * an HTTP answer that is not an Ollama model list is ``unreachable``, not
      ``reachable``. Something merely listening on the port is a different
      fact, exactly as ``daemon.probe_readiness`` refuses to call a squatter on
      port 8765 "the systemu daemon";
    * a timeout is ``unreachable`` (see the state comments above), while a URL
      that cannot be probed at all is ``unknown``.
    """
    import socket
    import urllib.error
    import urllib.parse
    import urllib.request

    base = url.strip().rstrip("/") if type(url) is str else ""
    if not base:
        return (STATE_UNKNOWN, "no base URL configured - set OLLAMA_URL in .env")
    try:
        parsed = urllib.parse.urlparse(base)
        scheme, netloc = parsed.scheme, parsed.netloc
    except Exception:
        return (STATE_UNKNOWN, f"{base} could not be parsed as a URL")
    if scheme not in ("http", "https") or not netloc:
        return (STATE_UNKNOWN,
                f"{base} is not an http(s) URL - it cannot be probed")

    target = f"{base}/api/tags"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(target, timeout=float(timeout)) as resp:
            code = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0)
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        return (STATE_UNREACHABLE,
                f"{base} answered HTTP {exc.code} on /api/tags - that is not "
                f"an Ollama server")
    except (socket.timeout, TimeoutError):
        return (STATE_UNREACHABLE, _silent_detail(base, timeout))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return (STATE_UNREACHABLE, _silent_detail(base, timeout))
        return (STATE_UNREACHABLE, f"nothing is serving Ollama at {base} "
                                   f"({exc.reason})")
    except Exception as exc:
        return (STATE_UNKNOWN, f"could not probe {base} ({type(exc).__name__})")

    if code and code != 200:
        return (STATE_UNREACHABLE, f"{base} answered HTTP {code} on /api/tags")
    try:
        models = json.loads(body.decode("utf-8", "replace")).get("models")
    except Exception:
        models = None
    if type(models) is not list:
        return (STATE_UNREACHABLE,
                f"{base} answered on /api/tags but not with an Ollama model "
                f"list - it is some other server")
    return (STATE_REACHABLE,
            f"answered at {base} with {len(models)} model(s) installed")


#: provider -> its reachability witness. Only keyless providers appear here.
_PROBES: Dict[str, Callable[[str, float], Tuple[str, str]]] = {
    "ollama": probe_ollama,
}


# ── the mint ────────────────────────────────────────────────────────────────

def provider_status(spec: ProviderSpec, config, *,
                    timeout: float = DEFAULT_PROBE_TIMEOUT,
                    probe: Optional[Callable[[str, float], Tuple[str, str]]] = None
                    ) -> ProviderStatus:
    """Mint the verdict for ONE provider. Never raises.

    ``probe`` is an injection point for tests; production leaves it None and
    the spec's own witness from ``_PROBES`` is used.
    """
    try:
        raw = getattr(config, spec.attr, "")
    except Exception:
        raw = ""
    value = raw.strip() if type(raw) is str else ""

    def _mk(state: str, detail: str) -> ProviderStatus:
        return ProviderStatus(spec.provider, spec.display, spec.env, spec.rule,
                              state, detail)

    if spec.rule == RULE_CREDENTIAL:
        if value:
            return _mk(STATE_SET, f"{spec.env} is set in .env")
        return _mk(STATE_MISSING, f"not set - add {spec.env} to .env")

    if spec.rule == RULE_REACHABILITY:
        fn = probe if probe is not None else _PROBES.get(spec.provider)
        if fn is None:
            # Declared keyless but nothing can witness it: fail closed rather
            # than fall back to "a string is present", which is the bug.
            return _mk(STATE_UNKNOWN,
                       f"{spec.display} has no reachability witness - it "
                       f"cannot be confirmed from {spec.env} alone")
        try:
            verdict = fn(value, timeout)
            state, detail = verdict[0], verdict[1]
        except Exception as exc:
            return _mk(STATE_UNKNOWN,
                       f"the {spec.display} probe failed ({type(exc).__name__})")
        # DEC-34: filter the probe's own verdict token. It may not hand back
        # STATE_SET, or any unrecognised value, and be believed.
        if type(state) is not str or state not in _PROBE_STATES:
            return _mk(STATE_UNKNOWN,
                       f"the {spec.display} probe returned an unusable verdict")
        return _mk(state, detail if type(detail) is str else "")

    # An unrecognised rule is never "configured".
    return _mk(STATE_UNKNOWN,
               f"no satisfaction rule is declared for {spec.provider}")


#: F19: a probe that OBSERVES NOTHING, for a caller that must do zero I/O.
#: It cannot make a keyless provider green — ``STATE_UNKNOWN`` is not in
#: ``SATISFIED_STATES`` — so declining to pay for the witness can only ever
#: withhold a claim, never manufacture one. "We did not ask" is a third, honest
#: answer, distinct from both "it is down" and "it is configured".
def unprobed(_url: str, _timeout: float = DEFAULT_PROBE_TIMEOUT) -> Tuple[str, str]:
    """The null witness: no network, no claim."""
    return (STATE_UNKNOWN,
            "not probed on this path - run `systemu doctor` to check "
            "whether it answers")


# ── the short-TTL memo for the keyless witness ──────────────────────────────
#
# The probe costs real wall-clock (measured on this machine with Ollama RUNNING:
# 0.60 s for `http://127.0.0.1:11434`, 1.08 s for the dual-stack `localhost`
# default; ~2 s when nothing is listening). A dashboard render path or a
# per-objective gate cannot pay that per call, and the settings page's answer
# was to move it off the render path entirely — which only works when you have a
# render path to move it off.
#
# So: memoise the OBSERVED verdict for a short TTL, keyed by (provider, URL).
# Only reachability results are cached; a credential is never a cache key and no
# credential value is ever stored here. A cache HIT replays a verdict that was
# genuinely observed within the TTL — it never invents one, and an expired entry
# re-probes rather than decaying to green.
PROBE_CACHE_TTL_S = 20.0
_PROBE_CACHE: Dict[Tuple[str, str], Tuple[float, str, str]] = {}


def clear_probe_cache() -> None:
    """Drop every memoised reachability verdict (tests; also after a config edit)."""
    _PROBE_CACHE.clear()


def _memoised(provider: str, fn, ttl_s: float, now):
    """Wrap a reachability probe in a per-(provider, URL) TTL memo."""
    import time as _time
    clock = now if callable(now) else _time.monotonic

    def _wrapped(url: str, timeout: float) -> Tuple[str, str]:
        key = (provider, url if type(url) is str else "")
        hit = _PROBE_CACHE.get(key)
        ts = clock()
        if hit is not None and (ts - hit[0]) < ttl_s:
            return (hit[1], hit[2])
        verdict = fn(url, timeout)
        try:
            state, detail = verdict[0], verdict[1]
        except Exception:
            return verdict
        # A MEMO MAY ONLY REPLAY AN OBSERVATION. `unknown` is the mint's "we did
        # not ask" — including everything `unprobed` returns — and pinning it
        # for the TTL would let one caller that declined to look suppress the
        # next caller that was willing to. Measured as a real defect while
        # building this: `_has_llm_provider` warmed the cache with `unprobed`
        # and the very next call, with a live probe, read back "unknown".
        # A malformed verdict is fail-closed by the mint and is never stored.
        if type(state) is str and state in _OBSERVED_STATES:
            _PROBE_CACHE[key] = (ts, state, detail if type(detail) is str else "")
        return verdict

    return _wrapped


def _effective_probe(spec: ProviderSpec, probe, cache_ttl_s, _now=None):
    """The witness ``spec`` will actually be scored with.

    Shared by ``all_provider_statuses`` and ``any_provider_usable`` so the two
    cannot memoise — or decline to memoise — differently. A second copy of this
    resolution would be a DEC-43 form (i) recipe split in miniature, and one of
    the two would inevitably grow its own policy: that is exactly what happened
    to the keyless witness itself (see ``any_provider_usable``).
    """
    if spec.rule != RULE_REACHABILITY or not cache_ttl_s or cache_ttl_s <= 0:
        return probe
    base = probe if probe is not None else _PROBES.get(spec.provider)
    # THE NULL WITNESS NEVER READS THE MEMO EITHER. `unprobed` means "this
    # caller may not spend an observation on this path"; letting it cash
    # someone else's would make the answer depend on whether an unrelated
    # surface happened to probe in the last 20 s, which is precisely the
    # hidden coupling that makes two surfaces disagree. Caught by
    # test_s4_episodic_guard: `_has_llm_provider(Config())` returned True
    # only when a settings-page probe had run first in the same process.
    # Identity on the function object (DEC-36: `is`, no dispatch).
    if base is None or base is unprobed:
        return probe
    return _memoised(spec.provider, base, float(cache_ttl_s), _now)


def all_provider_statuses(config, *, timeout: float = DEFAULT_PROBE_TIMEOUT,
                          probe=None, cache_ttl_s: float = 0.0,
                          _now=None) -> Dict[str, ProviderStatus]:
    """Mint every provider's verdict, keyed by provider id. Never raises.

    ``cache_ttl_s`` > 0 memoises the reachability witness for that many seconds
    per URL — for callers on a render path or a per-objective gate, which cannot
    each pay a loopback round trip. ``probe=unprobed`` skips the witness
    entirely and reports the honest ``unknown``.
    """
    out: Dict[str, ProviderStatus] = {}
    for s in PROVIDER_SPECS:
        out[s.provider] = provider_status(
            s, config, timeout=timeout,
            probe=_effective_probe(s, probe, cache_ttl_s, _now))
    return out


# ── the derived answers every surface consumes ──────────────────────────────

def satisfied_providers(statuses: Dict[str, ProviderStatus]) -> list:
    """Every provider whose usability was OBSERVED, in table order."""
    return [statuses[s.provider] for s in PROVIDER_SPECS
            if s.provider in statuses and statuses[s.provider].satisfied]


def any_satisfied(statuses: Dict[str, ProviderStatus]) -> bool:
    """Is ANY provider usable? The admission question, asked of the mint."""
    return bool(satisfied_providers(statuses))


def any_provider_usable(config, *, probe=None, timeout: float = DEFAULT_PROBE_TIMEOUT,
                        cache_ttl_s: float = PROBE_CACHE_TTL_S,
                        _now=None) -> bool:
    """"Can this install reach a model at all?" — for hot, non-render callers.

    EXACTLY ``any_satisfied(all_provider_statuses(...))``, evaluated lazily.
    ``tests/test_provider_verdict_unification`` pins that equality over every
    machine shape, because the difference between those two expressions is the
    defect this function used to be.

    WHAT IT USED TO DO, AND WHY THAT WAS WRONG (DEC-43)
    ---------------------------------------------------
    It spent the keyless witness only when a helper called ``selects_keyless``
    reported that a tier already NAMED the keyless provider. That made
    SATISFACTION conditional on SELECTION — two facts this module elsewhere
    keeps apart on purpose (``unusable_selected`` is the surface for the second
    one). The result, on one machine in one minute: ``daemon start`` BOOTED on a
    keyless install whose Ollama was answering (its gate,
    ``setup_flow.provider_available``, always spent the witness), ``doctor``
    printed "Ollama  OK  Reachable", and the /welcome wizard — whose own step-1
    remedy text offers "use Ollama (no key)" — refused to finish, because its
    gate came through here. F19 removed the six copies of the RECIPE; this was a
    split in the POLICY, one layer further in, and it produced the same thing:
    two answers to one operator-facing question.

    So the witness is now spent unconditionally, and the answer is the same one
    every other surface gets.

    THE COST, WHICH IS REAL AND IS PAID ELSEWHERE
    ---------------------------------------------
    The keyless probe costs ~1-2 s of loopback and this runs at the end of every
    capture (``episodic_memory``) and per objective (``open_world_planner``).
    Two things keep that bounded WITHOUT reintroducing a second verdict:

    * the operands are ordered, not filtered. ``any_satisfied`` is an OR, so
      once a keyed provider is satisfied the keyless operand cannot change the
      result and is never evaluated. A machine with any key pays nothing. The
      machine that pays is the one with no key — where the probe is the only
      thing that can answer the question at all;
    * the surviving probe rides the module's 20 s memo by default, the same one
      the health banner spends on every route render.

    Never raises.
    """
    try:
        keyed = [s for s in PROVIDER_SPECS if s.rule != RULE_REACHABILITY]
        keyless = [s for s in PROVIDER_SPECS if s.rule == RULE_REACHABILITY]
        for s in keyed + keyless:
            st = provider_status(
                s, config, timeout=timeout,
                probe=_effective_probe(s, probe, cache_ttl_s, _now))
            if st.satisfied:
                return True
    except Exception:
        return False
    return False


class _EnvOverlay:
    """A config VIEW whose provider attributes fall back to the environment.

    NOT a second copy of the recipe: the attribute-to-env-var pairing is read
    off ``PROVIDER_SPECS``, and the satisfaction rule is still the mint's. It
    exists because a long-lived ``Config`` snapshot can be OLDER than the .env
    the operator just edited — the onboarding checklist has to answer "is a
    provider usable NOW?", and it was the old code's ``or os.environ.get(...)``
    fallback that made that true for OpenRouter only.

    Every other attribute (tier selections, vault dir, …) delegates untouched.
    """

    __slots__ = ("_cfg", "_env")

    def __init__(self, cfg, env):
        object.__setattr__(self, "_cfg", cfg)
        object.__setattr__(self, "_env", env)

    def __getattr__(self, name):
        cfg = object.__getattribute__(self, "_cfg")
        spec = _SPEC_BY_ATTR.get(name)
        if spec is None:
            return getattr(cfg, name)
        try:
            raw = getattr(cfg, name, "")
        except Exception:
            raw = ""
        value = raw.strip() if type(raw) is str else ""
        if value:
            return value
        env = object.__getattribute__(self, "_env")
        try:
            fallback = env.get(spec.env, "")
        except Exception:
            fallback = ""
        return fallback if type(fallback) is str else ""


def env_overlay(config, environ=None):
    """``config``, with blank provider attributes filled from the environment."""
    import os as _os
    return _EnvOverlay(config, _os.environ if environ is None else environ)


def configure_hint(statuses: Dict[str, ProviderStatus]) -> str:
    """One ASCII line naming EVERY provider the operator could configure.

    Generated from ``PROVIDER_SPECS``, so a sixth provider appears here with
    nobody editing a sentence — and so no surface can go on offering only
    OpenRouter, which is exactly what ``daemon start``, ``doctor``, the health
    banner and ``Config.validate`` each did in their own words.

    ASCII only (DEC-32c): this crosses a Windows console, a log file and the
    dashboard. It names ENV VARS and reachability, never a credential value.
    """
    keyed, keyless = [], []
    for spec in PROVIDER_SPECS:
        if spec.rule == RULE_REACHABILITY:
            keyless.append(f"{spec.display} (no key - run it locally, or point "
                           f"{spec.env} at it)")
        else:
            keyed.append(f"{spec.env} ({spec.display})")
    parts = []
    if keyed:
        parts.append("set one of " + ", ".join(keyed) + " in .env")
    if keyless:
        parts.append("or use " + ", ".join(keyless))
    text = "; ".join(parts) + "."
    return text.encode("ascii", "backslashreplace").decode("ascii")


def status_line(st: ProviderStatus) -> str:
    """One ASCII operator-facing line for a provider. Never prints a value."""
    mark = {STATE_SET: "OK   set",
            STATE_MISSING: "--   not set",
            STATE_REACHABLE: "OK   reachable",
            STATE_UNREACHABLE: "--   not reachable",
            STATE_UNKNOWN: "?    unknown"}.get(st.state, "?    unknown")
    line = f"{st.display:<12} {mark:<18} {st.detail}"
    return line.encode("ascii", "backslashreplace").decode("ascii")


# ── the SELECTION half: what the tiers will really be called on ─────────────
#
# The pair this module keeps apart is SATISFACTION ("is this provider usable")
# and SELECTION ("is what we are set to use among them"). Everything above is
# the first. These three are the second, and they live here for the same reason
# the first does: there were two derivations of "selected" in the tree and the
# two surfaces that consume `unusable_selected` disagreed because of it (see
# `routed_tier_providers`).


def routed_provider(model, override, config) -> str:
    """Which provider will the ROUTER actually call for this tier? "" if unknown.

    THE ROUTER IS ASKED, NOT IMITATED. ``llm_router.resolve_provider_keyaware``
    is where "which provider serves this model on this machine" is decided, and
    the decision is KEY-AWARE: a ``google/*`` or ``anthropic/*`` tier on a box
    that holds only an OpenRouter key is genuinely served BY OpenRouter. A
    model-prefix guess written here would be a second copy of that rule (DEC-43
    form (i)) and would cry wolf on exactly that machine -- warning about a
    provider the call never reaches.

    The import is deferred because this module is loaded by the CLI, the boot
    gate and every dashboard render, while ``llm_router`` pulls in the OpenAI
    SDK and loads very early itself. The name it reaches for is PUBLIC, so this
    is a seam the router maintains rather than a private coupling.

    An id the mint cannot score returns "", so an unrecognised provider can only
    ever WITHHOLD a warning, never manufacture one. Never raises.
    """
    try:
        from systemu.core.llm_router import resolve_provider_keyaware
        cls = resolve_provider_keyaware(
            model if type(model) is str else "",
            (override if type(override) is str else "").strip(), config)
        name = getattr(cls, "__name__", "")
    except Exception:
        return ""
    if type(name) is not str:
        return ""
    # The provider registry's own id convention (``providers._by_name``).
    suffix = "Provider"
    pid = (name[:-len(suffix)] if name.endswith(suffix) else name).lower()
    return pid if pid in SPEC_BY_PROVIDER else ""


def routed_tier_providers(config) -> List[str]:
    """The provider each of tiers 1-3 will really be called on.

    THE SELECTION ``unusable_selected`` IS MEANT TO SCORE, and the last DEC-43
    split. Its two consumers used to derive that word differently: /welcome fed
    it this, while the Settings banner fed it the explicit ``tier{N}_provider``
    OVERRIDE alone -- which is EMPTY on a fresh install, so Settings stayed
    silent on the very machine the wizard warned about (no keys, a keyless
    endpoint answering, the shipped cloud tier models). One fact, one mint, two
    derivations of its input, and therefore two answers on one screen apiece.

    The override is not discarded, it is SUBSUMED: the router obeys it
    literally, so a tier pinned to a dead provider still scores as that
    provider. What it adds is the tier whose provider was never named out loud
    and is implied by the MODEL id -- which is every tier on a default install.

    Never raises; a tier it cannot score contributes "" and is ignored
    downstream.
    """
    out: List[str] = []
    for i in (1, 2, 3):
        try:
            model = getattr(config, f"tier{i}_model", "")
            override = getattr(config, f"tier{i}_provider", "")
        except Exception:
            model, override = "", ""
        out.append(routed_provider(model, override, config))
    return out


def unusable_selected(statuses: Dict[str, ProviderStatus], selected) -> list:
    """The selected providers whose satisfaction was NOT observed.

    ``selected`` is any iterable of provider ids ("", "auto" and unknown ids
    are ignored) -- in practice ``routed_tier_providers(config)``, which is the
    one derivation of that word both consuming surfaces now share. Consumed by
    the Settings banner and by /welcome step 1, each of which red-flags a tier
    that will fail at call time.
    """
    out = []
    for p in sorted({str(x).strip().lower() for x in (selected or ())}
                    - {"", "auto"}):
        st = statuses.get(p)
        if st is not None and not st.satisfied:
            out.append(st)
    return out
