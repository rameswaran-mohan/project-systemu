"""G0 — the shared EffectTag vocabulary (spec UNIFIED-v2 §5.7).

This is the effect language every later gate/verifier consumes. Two design
properties are load-bearing:

  * **Open vocabulary (Callout 2).** A tier or the planner may PROPOSE an effect
    class the type system has never seen. ``coerce`` maps any unrecognized value
    to :data:`EffectTag.UNKNOWN` (which the action gate treats as
    dangerous-until-proven ⇒ REQUIRE_APPROVAL), and ``register_effect_tag`` lets
    a new modality add its own tag at runtime. Determinism *classifies and gates*
    a proposal; it never *refuses* the plan.

  * **Advisory, escalate-only classifier.** ``classify_source`` is a deterministic
    AST scan of a tool's source for effectful sinks. It is a SIGNAL, not a proof:
    the absence of a tag is NEVER "no effect" (unparseable source ⇒ UNKNOWN), and
    the real network boundary is the OS-kernel egress jail (S2), not this scan.

Nothing here imports :mod:`systemu.core.models` — the vocabulary is foundational
and must stay import-cycle-free (``core.models`` stores ``effect_tags`` as plain
strings and never imports this module).

**Cycle note (R-A13b-2ii-a).** :func:`classify_source` consults the curated
:mod:`systemu.runtime.effect_signals` map to emit the SEMANTIC classes
(``money_move``/``send_message``) the structural scan cannot reach. That module
imports ``EffectTag`` from HERE at its top, so this module must import it **LAZILY
inside** :func:`classify_source` (never at module top) — whichever module loads
first fully initializes ``effect_tags`` (no top-level ``effect_signals`` import)
before ``effect_signals`` pulls ``EffectTag`` back, so there is no import cycle.
"""
from __future__ import annotations

import ast
from enum import Enum
from typing import Set


class EffectTag(str, Enum):
    """The canonical effect classes (str-valued so they serialize to their value
    in ``model_dump(mode="json")`` and round-trip through plain-string storage)."""

    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    LOCAL_DELETE = "local_delete"
    SHELL_EXEC = "shell_exec"
    NET_READ = "net_read"
    NET_MUTATE = "net_mutate"
    SEND_MESSAGE = "send_message"
    MONEY_MOVE = "money_move"
    OAUTH_CALL = "oauth_call"
    # sentinel: an effect the classifier could not resolve — gated, never refused
    UNKNOWN = "unknown"


# The §5.7 two-band DENY floor keys on these: an UNKNOWN effect that ALSO carries
# a high-severity signal fails closed to DENY (not a rubber-stampable card).
HIGH_SEVERITY: "frozenset[EffectTag]" = frozenset({
    EffectTag.LOCAL_DELETE,
    EffectTag.NET_MUTATE,
    EffectTag.SEND_MESSAGE,
    EffectTag.MONEY_MOVE,
})

_CANONICAL = {t.value: t for t in EffectTag}
# Runtime-registered extension tags: value -> is_high_severity
_EXTENSIONS: "dict[str, bool]" = {}


def register_effect_tag(value: str, *, high_severity: bool = False) -> str:
    """Register a new effect class proposed by a tier/modality at runtime.

    Idempotent; returns the normalized value. Registering a canonical value is a
    no-op. This is the open-vocabulary hook — a novel actuation can declare its
    own effect class, and the gate then treats it by its declared severity."""
    v = str(value or "").strip().lower()
    if not v:
        raise ValueError("effect tag value must be a non-empty string")
    if v not in _CANONICAL:
        _EXTENSIONS[v] = bool(high_severity)
    return v


def coerce(value) -> str:
    """Normalize any value to a known/registered tag value, else ``UNKNOWN``.

    Open-world rule: an unrecognized effect is UNKNOWN (gated), never an error."""
    if isinstance(value, EffectTag):
        return value.value
    v = str(value or "").strip().lower()
    if v in _CANONICAL or v in _EXTENSIONS:
        return v
    return EffectTag.UNKNOWN.value


_HIGH_SEVERITY_VALUES = {t.value for t in HIGH_SEVERITY}


def is_high_severity(value) -> bool:
    """True iff the (known) tag is a high-severity effect. UNKNOWN is NOT
    high-severity on its own — the DENY floor is UNKNOWN *plus* a separately
    detected high-severity signal (see the action gate, S1)."""
    v = coerce(value)
    if v == EffectTag.UNKNOWN.value:
        return False
    if v in _HIGH_SEVERITY_VALUES:
        return True
    return _EXTENSIONS.get(v, False)


def all_known() -> "list[str]":
    return sorted(set(_CANONICAL) | set(_EXTENSIONS))


# --------------------------------------------------------------------------- #
# deterministic AST source classifier (advisory signal, escalate-only)
# --------------------------------------------------------------------------- #

_NET_CLIENTS = {"requests", "httpx", "aiohttp", "session", "client", "http", "urllib3"}
_NET_READ_METHODS = {"get", "head", "options"}
_NET_WRITE_METHODS = {"post", "put", "patch", "delete", "request", "send"}
# attr-only network mutators (receiver is not a bare module name, e.g. self.session.post)
_ATTR_ONLY_NET_MUTATE = {"post", "put", "patch"}

_SHELL_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"),
                ("os", "execve"), ("os", "execvp"), ("os", "execvpe")}
_SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}

_DELETE_ATTRS = {("os", "remove"), ("os", "unlink"), ("shutil", "rmtree")}
_WRITE_ATTRS = {("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"),
                ("shutil", "copytree"), ("shutil", "move"), ("os", "rename"),
                ("os", "replace"), ("os", "mkdir"), ("os", "makedirs")}

_WRITE_METHODS = {"write_text", "write_bytes"}
_READ_METHODS = {"read_text", "read_bytes"}
_WRITE_MODE_CHARS = ("w", "a", "x", "+")


class _EffectVisitor(ast.NodeVisitor):
    def __init__(self, sig=None) -> None:
        self.tags: Set[EffectTag] = set()
        # the curated effect_signals module (lazily injected by classify_source);
        # None ⇒ semantic classification degrades to the structural scan (defensive).
        self._sig = sig

    def _add_class(self, cls) -> None:
        """Add the EffectTag for a curated class VALUE (e.g. "money_move"); no-op on
        None / an unknown value. Keeps the never-raises contract."""
        if not cls:
            return
        try:
            self.tags.add(EffectTag(cls))
        except ValueError:
            pass

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        if self._sig is not None:
            for alias in node.names:
                self._add_class(self._sig.class_for_import(alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if self._sig is not None and node.module:
            self._add_class(self._sig.class_for_import(node.module))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        # a URL / host string literal names a curated endpoint (host-suffix match).
        if self._sig is not None and isinstance(node.value, str):
            self._add_class(self._sig.class_for_host(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API)
        func = node.func

        if isinstance(func, ast.Name):
            if func.id == "open":
                self.tags.add(self._open_effect(node))
            elif func.id == "urlopen":
                self.tags.add(self._urlopen_effect(node))

        elif isinstance(func, ast.Attribute):
            attr = func.attr
            # module.attr where module is a bare Name
            if isinstance(func.value, ast.Name):
                mod = func.value.id
                pair = (mod, attr)
                if pair in _DELETE_ATTRS:
                    self.tags.add(EffectTag.LOCAL_DELETE)
                elif pair in _WRITE_ATTRS:
                    self.tags.add(EffectTag.LOCAL_WRITE)
                elif pair in _SHELL_ATTRS:
                    self.tags.add(EffectTag.SHELL_EXEC)
                elif mod == "subprocess" and attr in _SUBPROCESS_FUNCS:
                    self.tags.add(EffectTag.SHELL_EXEC)
                elif mod == "socket" and attr in {"socket", "create_connection"}:
                    self.tags.add(EffectTag.NET_MUTATE)
                elif mod in _NET_CLIENTS:
                    if attr in _NET_READ_METHODS:
                        self.tags.add(EffectTag.NET_READ)
                    elif attr in _NET_WRITE_METHODS:
                        self.tags.add(EffectTag.NET_MUTATE)
            # attr-only signals (any receiver) — conservative, high-value only
            if attr == "urlopen":
                self.tags.add(self._urlopen_effect(node))
            elif attr == "unlink":
                self.tags.add(EffectTag.LOCAL_DELETE)
            elif attr in _WRITE_METHODS:
                self.tags.add(EffectTag.LOCAL_WRITE)
            elif attr in _READ_METHODS:
                self.tags.add(EffectTag.LOCAL_READ)
            elif attr in _ATTR_ONLY_NET_MUTATE:
                self.tags.add(EffectTag.NET_MUTATE)

            # R-A13b-2ii-a — curated SEMANTIC classes (money_move/send_message) by
            # attr/method chain. Test the QUALIFIED 2-component chain
            # ("PaymentIntent.create"/"messages.create") AND the bare attr
            # ("sendmail"/"chat_postMessage"); the table omits generic verbs so a
            # bare ".create"/".get" never over-hits.
            if self._sig is not None:
                self._add_class(self._sig.class_for_attrchain(attr))
                if isinstance(func.value, ast.Attribute):
                    self._add_class(self._sig.class_for_attrchain(
                        f"{func.value.attr}.{attr}"))

        self.generic_visit(node)

    @staticmethod
    def _mode_arg(node: ast.Call):
        # positional mode is the 2nd arg; else a mode= kwarg
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return kw.value.value
        return None

    def _open_effect(self, node: ast.Call) -> EffectTag:
        mode = self._mode_arg(node)
        if isinstance(mode, str) and any(c in mode for c in _WRITE_MODE_CHARS):
            return EffectTag.LOCAL_WRITE
        return EffectTag.LOCAL_READ

    @staticmethod
    def _urlopen_effect(node: ast.Call) -> EffectTag:
        # urlopen(url, data=...) is a POST; a bare urlopen(url) is a GET
        has_data = len(node.args) >= 2 or any(kw.arg == "data" for kw in node.keywords)
        return EffectTag.NET_MUTATE if has_data else EffectTag.NET_READ


def classify_source(code: str) -> Set[EffectTag]:
    """Deterministically classify a tool's source into effect tags.

    A SIGNAL (advisory, escalate-only). Unparseable source ⇒ ``{UNKNOWN}`` — the
    absence of a tag is never treated as "no effect"."""
    if not code or not code.strip():
        return set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {EffectTag.UNKNOWN}
    # LAZY import (see the module docstring's cycle note): effect_signals imports the
    # EffectTag enum from THIS module, so importing it here — after effect_tags is
    # fully loaded — is cycle-free. A failure degrades to the structural scan.
    try:
        from systemu.runtime import effect_signals as _sig
    except Exception:
        _sig = None
    visitor = _EffectVisitor(_sig)
    visitor.visit(tree)
    return visitor.tags
