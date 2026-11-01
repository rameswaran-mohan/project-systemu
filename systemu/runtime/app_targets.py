"""P26 -- what a ``launch_application`` / ``close_application`` call acts on: ONE mint.

THE DEFECT. Both tools spawn a process chosen by their parameters, but only the
per-tool gate governed them, and its signature ignores parameters: ONE standing
approval of ``launch_application`` covered EVERY program the model later chose
(and one of ``close_application`` covered stopping any process). The choice of
program -- which alias parameter is used, the friendly-name lookup -- happened
INSIDE the tool body, after every gate, and the card showed the raw parameters cut
to 8 keys, so a 9th key the registry reconciled onto ``path`` ran but was never
shown.

THE PROPERTY. The target is resolved HERE, by a pure function of the call's
final keyword arguments (the value ``ToolSandbox.execute_tool`` seals and then
executes verbatim). The gate scores, signs and shows the resolved target; the
tool body calls the same resolver on the same kwargs and acts on exactly what it
returns. Nothing else in the tree knows the friendly-name maps or the alias
order (DEC-43: one mint per fact).

A call whose target cannot be resolved deterministically -- no target, a blank
or non-string value, two parameters naming different applications, a NUL,
control or invisible formatting character, unbalanced quotes, shell syntax that
would run more than one program or expand text when it runs, a parameter the
tool does not take -- is a ``TargetRefusal``: refused, never carded, never
signed (DEC-27).

What runs, exactly:

  * ``launch_application`` -- ``value`` is the command line handed to the
    system shell (``subprocess.Popen(value, shell=True)``), or, on Windows, a URI
    opened with its file association when it ends in ``:``.
  * ``close_application`` -- ``value`` is the image name passed to ``taskkill
    /IM`` (Windows) or the pattern passed to ``pkill -f`` (elsewhere), or a
    process ID. A process ID is reused by other programs once its process exits,
    so a PID target is never ``rememberable``: no standing approval covers it.

Refusal reasons never echo a parameter VALUE (only short identifier-shaped
parameter NAMES), so a reason can be logged and shown without redaction.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Union

#: The tools gated per resolved target (``ToolSandbox.execute_tool``).
TARGET_GATED_TOOLS = frozenset({"launch_application", "close_application"})

#: ``error_type`` of a call whose target could not be resolved.
TARGET_UNDETERMINABLE = "target_undeterminable"

#: Friendly application name -> what ``launch_application`` runs.
LAUNCH_APP_MAP: Dict[str, str] = {
    "snipping tool":    "SnippingTool.exe",
    "snippingtool":     "SnippingTool.exe",
    "snip & sketch":    "ms-screenclip:",
    "microsoft word":   "WINWORD.EXE",
    "word":             "WINWORD.EXE",
    "microsoft excel":  "EXCEL.EXE",
    "excel":            "EXCEL.EXE",
    "microsoft powerpoint": "POWERPNT.EXE",
    "powerpoint":       "POWERPNT.EXE",
    "notepad":          "notepad.exe",
    "notepad++":        "notepad++.exe",
    "google chrome":    "chrome.exe",
    "chrome":           "chrome.exe",
    "microsoft edge":   "msedge.exe",
    "edge":             "msedge.exe",
    "firefox":          "firefox.exe",
    "explorer":         "explorer.exe",
    "file explorer":    "explorer.exe",
    "cmd":              "cmd.exe",
    "powershell":       "powershell.exe",
    "paint":            "mspaint.exe",
    "calculator":       "calc.exe",
    "task manager":     "taskmgr.exe",
}

#: Friendly application name -> the image name ``close_application`` stops.
CLOSE_APP_MAP: Dict[str, str] = {
    "snipping tool":        "SnippingTool.exe",
    "snippingtool":         "SnippingTool.exe",
    "microsoft word":       "WINWORD.EXE",
    "word":                 "WINWORD.EXE",
    "microsoft excel":      "EXCEL.EXE",
    "excel":                "EXCEL.EXE",
    "microsoft powerpoint": "POWERPNT.EXE",
    "powerpoint":           "POWERPNT.EXE",
    "notepad":              "notepad.exe",
    "google chrome":        "chrome.exe",
    "chrome":               "chrome.exe",
    "microsoft edge":       "msedge.exe",
    "edge":                 "msedge.exe",
    "paint":                "mspaint.exe",
    "calculator":           "calc.exe",
}

#: ``launch_application``'s parameters, in the order the body has always read them.
LAUNCH_PARAMS: Tuple[str, ...] = ("path", "application_name", "executable_name", "name")
#: ``close_application``'s name parameters, in the order the body has always read them.
CLOSE_NAME_PARAMS: Tuple[str, ...] = ("application_name", "name", "executable")
CLOSE_PARAMS: Tuple[str, ...] = CLOSE_NAME_PARAMS + ("pid", "force")

#: Characters that make a launch command line more than ONE program run with its
#: arguments, or make what runs depend on text expanded when it runs: chaining and
#: redirection (``& | ; < >``), substitution (backtick, ``$``), cmd.exe variable
#: expansion and escaping (``% ^``), and line breaks.
_LAUNCH_SHELL_CHARS = frozenset("&|;<>`$%^\r\n")
#: Longest target accepted, so the approval card can always show it whole.
MAX_TARGET_CHARS = 1024

_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")


@dataclass(frozen=True)
class AppTarget:
    """One resolved target. ``value`` is exactly what the tool body acts on."""

    tool: str        # "launch_application" | "close_application"
    action: str      # "launch" | "close"
    kind: str        # launch: "command" | "uri";  close: "image" | "pid"
    value: str
    requested: str   # what the call named, before the friendly-name lookup
    via: str         # the parameter it came from
    force: bool = False

    @property
    def rememberable(self) -> bool:
        """False for a process ID: PIDs are reused, so "always allow PID n" would
        later cover whatever program holds that number."""
        return self.kind != "pid"

    def signed(self) -> str:
        """The canonical form a standing approval binds (ASCII JSON, one-to-one).
        ``requested``/``via`` are deliberately absent: two spellings that resolve
        to the same program are the same target."""
        return json.dumps([self.tool, self.action, self.kind, self.value, self.force],
                          ensure_ascii=True, separators=(",", ":"))

    def describe(self, *, show: Callable[[str], str] = lambda s: s,
                 platform: Optional[str] = None) -> str:
        """One line saying what the call will do. ``show`` renders model-supplied
        text (the gate passes a redact-then-cap function, DEC-31)."""
        plat = sys.platform if platform is None else platform
        v = show(self.value)
        mapped = self.requested.lower() != self.value.lower()
        named = " (the name {} resolves to it)".format(show(self.requested)) if mapped else ""
        if self.action == "launch":
            if self.kind == "uri":
                return "opens: {}{}".format(v, named)
            return "launches: {}{}".format(v, named)
        if self.kind == "pid":
            return "{} process ID {}".format("kills" if self.force else "terminates", v)
        if plat == "win32":
            how = ("taskkill /F /IM {}".format(v) if self.force else
                   "taskkill /IM {}, then /F if it does not exit".format(v))
            return "closes every running {}{} ({})".format(v, named, how)
        return ("kills every process whose command line matches {}{} (pkill -f)"
                .format(v, named))


@dataclass(frozen=True)
class TargetRefusal:
    """Why a call's target could not be resolved. ``reason`` echoes no value."""

    reason: str


Resolution = Union[AppTarget, TargetRefusal]


def _name(key: Any) -> str:
    """A parameter NAME fit to quote in a reason (never a value)."""
    if type(key) is str and _PARAM_NAME.fullmatch(key):
        return "'" + key + "'"
    return "a parameter with an unusual name"


def _call_items(call: Any) -> Optional[Dict[Any, Any]]:
    """The call's kwargs, read through the BASE dict accessors (DEC-36)."""
    if not isinstance(call, dict):
        return None
    return dict(dict.items(call))


def _unknown_keys(items: Dict[Any, Any], accepted: Tuple[str, ...]) -> Optional[TargetRefusal]:
    unknown = [k for k in items if not (type(k) is str and k in accepted)]
    if not unknown:
        return None
    names = sorted({_name(k) for k in unknown})
    return TargetRefusal("it passes parameters the tool does not take: " + ", ".join(names))


def _bad_char(value: str) -> Optional[str]:
    """What an approval card could not show faithfully (NUL, control or invisible
    formatting characters), else None. A tab is ordinary whitespace."""
    for ch in value:
        if ch == "\x00":
            return "a NUL byte"
        if ch == "\t":
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc":
            return "a control character"
        if cat == "Cf":
            return "an invisible formatting character"
    return None


def _named_values(items: Dict[Any, Any],
                  params: Tuple[str, ...]) -> Union[list, TargetRefusal]:
    """``[(param, value)]`` for every name parameter that carries a value.
    Absent, ``None`` and ``""`` mean "not given" (the body's defaults); anything
    else must be a non-blank exact ``str`` (DEC-36: ``type(x) is str``)."""
    given = []
    for key in params:
        v = items.get(key)
        if v is None or (type(v) is str and v == ""):
            continue
        if type(v) is not str:
            return TargetRefusal("{} is not a string".format(_name(key)))
        bad = _bad_char(v)
        if bad is not None:
            return TargetRefusal("{} contains {}".format(_name(key), bad))
        if not v.strip():
            return TargetRefusal("{} is blank".format(_name(key)))
        given.append((key, v))
    return given


def _one_target(resolved: list) -> Union[Tuple[str, str, str, str], TargetRefusal]:
    """The single target several name parameters agree on, or a refusal."""
    distinct = {(kind, value) for _k, _v, kind, value in resolved}
    if len(distinct) > 1:
        keys = " and ".join(_name(k) for k, _v, _kind, _value in resolved)
        return TargetRefusal("it is ambiguous: {} name different applications".format(keys))
    return resolved[0]


def _launch_value(raw: str) -> Union[Tuple[str, str], TargetRefusal]:
    text = raw.strip()
    value = LAUNCH_APP_MAP.get(text.lower(), text)
    if len(value) > MAX_TARGET_CHARS:
        return TargetRefusal("it is longer than {} characters".format(MAX_TARGET_CHARS))
    shell = sorted({c for c in value if c in _LAUNCH_SHELL_CHARS})
    if shell:
        shown = " ".join(repr(c)[1:-1] for c in shell)
        return TargetRefusal(
            "it uses shell syntax ({}) that would run more than one program or "
            "expand text when it runs; launch one application, or use run_command "
            "for a shell command".format(shown))
    if value.count('"') % 2:
        return TargetRefusal("its quotes are unbalanced")
    return ("uri" if value.endswith(":") else "command"), value


def resolve_launch_target(call: Any) -> Resolution:
    """What a ``launch_application`` call with these kwargs launches."""
    items = _call_items(call)
    if items is None:
        return TargetRefusal("the call carries no parameters")
    unknown = _unknown_keys(items, LAUNCH_PARAMS)
    if unknown is not None:
        return unknown
    given = _named_values(items, LAUNCH_PARAMS)
    if type(given) is TargetRefusal:
        return given
    if not given:
        return TargetRefusal("it names no application (pass one in 'path')")
    resolved = []
    for key, raw in given:
        got = _launch_value(raw)
        if type(got) is TargetRefusal:
            return TargetRefusal("{}: {}".format(_name(key), got.reason))
        resolved.append((key, raw, got[0], got[1]))
    one = _one_target(resolved)
    if type(one) is TargetRefusal:
        return one
    key, raw, kind, value = one
    return AppTarget(tool="launch_application", action="launch", kind=kind,
                     value=value, requested=raw.strip(), via=key)


def resolve_close_target(call: Any) -> Resolution:
    """What a ``close_application`` call with these kwargs stops."""
    items = _call_items(call)
    if items is None:
        return TargetRefusal("the call carries no parameters")
    unknown = _unknown_keys(items, CLOSE_PARAMS)
    if unknown is not None:
        return unknown
    force = items.get("force")
    if force is None:
        force = False
    elif type(force) is not bool:
        return TargetRefusal("'force' is not true or false")
    given = _named_values(items, CLOSE_NAME_PARAMS)
    if type(given) is TargetRefusal:
        return given
    pid = items.get("pid")
    if pid is not None:
        if type(pid) is not int:
            return TargetRefusal("'pid' is not a whole number")
        if pid <= 0:
            return TargetRefusal("'pid' is not a process ID")
        if given:
            return TargetRefusal("it is ambiguous: it names both a process ID and an "
                                 "application")
        return AppTarget(tool="close_application", action="close", kind="pid",
                         value=str(pid), requested=str(pid), via="pid", force=force)
    if not given:
        return TargetRefusal("it names no application (pass one in 'application_name')")
    resolved = []
    for key, raw in given:
        text = raw.strip()
        value = CLOSE_APP_MAP.get(text.lower(), text)
        if len(value) > MAX_TARGET_CHARS:
            return TargetRefusal("{}: it is longer than {} characters".format(
                _name(key), MAX_TARGET_CHARS))
        resolved.append((key, raw, "image", value))
    one = _one_target(resolved)
    if type(one) is TargetRefusal:
        return one
    key, raw, kind, value = one
    return AppTarget(tool="close_application", action="close", kind=kind, value=value,
                     requested=raw.strip(), via=key, force=force)


_RESOLVERS = {
    "launch_application": resolve_launch_target,
    "close_application": resolve_close_target,
}


def resolve_target(tool_name: Any, call: Any) -> Resolution:
    """The one entry point the gate uses. ``tool_name`` is the implementation's
    file stem -- the name ``execute_tool`` keys every gate on."""
    resolver = _RESOLVERS.get(tool_name) if type(tool_name) is str else None
    if resolver is None:
        return TargetRefusal("it is not a target-gated tool")
    return resolver(call)
