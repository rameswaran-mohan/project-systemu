"""P21 -- the agent never runs a systemu OPERATOR command.

THE PROPERTY. No command the agent runs through a process-spawning tool can
invoke a systemu CLI command that ``sharing_on.cli_access`` classifies
operator-only (consent, grants, posture, credentials, approvals, decisions, or
anything that acts with the operator's authority). Such a call is refused
BEFORE anything executes: it is never offered as an approval card, a standing
approval on record for it is ignored (read, never consumed), and the refusal
names the exact command for the operator to run in their own terminal.

Why it is needed: every operator-consent surface is a CLI verb with a typed y/N
and a ``--yes`` for scripts, and the shell gate binds a standing approval to the
exact command string. One "Always allow" of ``systemu census grant X --yes``
would let any later model run re-grant a category the operator had withdrawn.

WHERE IT RUNS. ``ToolSandbox.execute_tool`` -- the one chokepoint every
registry-tool execution crosses (the shadow runtime, the chat lane, the forge
dry-run, and ``ToolSandbox.execute``'s v1 fall-through), ahead of the command
gate and of both execution paths (the in-process ``ToolRegistry`` fast path and
the subprocess backend) -- and the v2 code-registered branch of
``ToolSandbox.execute``. The verdict is a VALUE (a refusal the caller returns as
the call's result), never a raise a frame in between could swallow (DEC-32).

WHAT IT INSPECTS. Every string parameter (and every list-of-strings argv) of a
call to a tool that spawns a process from its parameters: ``run_command``,
``run_cli_command`` and ``launch_application`` by name, plus any tool whose
declared or source-derived effect tags include ``shell_exec``. All of a call's
string parameters, not just ``command``: ``ToolRegistry._reconcile_params`` can
map an unrecognised parameter onto ``run_cli_command``'s ``command``.

HOW IT RECOGNISES AN INVOCATION. Each string is tokenized four ways -- cmd.exe
rules, POSIX-sh rules, ``shlex.split`` (exactly what ``run_cli_command`` runs)
and ``str.split`` (what the command gate's read-only check uses) -- and every
segment of every chain (``&&``, ``||``, ``;``, ``|``, ``&``, newlines,
parentheses) is examined. An invocation is any of:

  * the ``systemu`` / ``sharing_on`` console script (or any other console script
    pointing at ``sharing_on.cli``), by bare name or path, with any of the
    launcher suffixes (``.exe``, ``-script.py`` ...), anywhere in a segment --
    so ``start``, ``env``, ``nohup``, ``xargs``, ``pipx run`` ... are covered;
  * a file that IS an installed console script -- same file (link) or same
    bytes (copy) -- at a program position or as a path token;
  * ``-m systemu`` / ``-m sharing_on`` / ``-m systemu.<anything>`` for any
    interpreter (``python``, ``py``, a venv launcher stub, a renamed copy);
  * an interpreter running a script from the systemu / sharing_on package;
  * the payload of a shell wrapper -- ``cmd /c``, ``bash -c``, ``sh -c``,
    ``powershell -Command`` (and ``-EncodedCommand``, decoded), ``pwsh`` --
    and any quoted string that names systemu (``schtasks /tr "..."``), each
    analysed recursively as a command line.

The verb is then resolved against the LIVE click tree (``sharing_on.cli.cli``)
and its registry classification; this module keeps no list of verbs.

FAIL-CLOSED (DEC-27). A call that names systemu or sharing_on and cannot be
parsed confidently -- a tokenizer error, run-time expansion (``$``, backticks,
``%VAR%``), inline interpreter code, an expression around the name, an
unclassified or unknown verb, a systemu module other than the CLI, a command
table that would not load -- is refused with the reason "could not establish
this does not run a systemu operator command". False positives are accepted on
purpose: e.g. ``python -c "import systemu"`` and an ``echo`` of a sentence that
contains "systemu <word>" are refused.

RESIDUALS -- what this does NOT see (DEC-34: stated, not implied):

  1. A spawn performed INSIDE a tool body. A forged/agent-written tool whose
     code calls ``subprocess.run(["systemu", "census", "grant", ...])`` (or
     builds the argv from anything but its parameters) is invisible here: the
     fence reads parameters, not bodies. The only control on such a tool is the
     per-tool gate, and a standing approval of that tool (params-independent
     signature) covers every later call. In-process Python is one trust domain
     (DEC-36); a body-scan "second layer" would check something cheaper than
     execution and is deliberately not added.
  2. A command line that constructs the program name at run time without
     naming it literally: variable expansion or command substitution of a value
     set elsewhere (``%P% census grant``), globs (``sy?temu``), brace
     expansion, base64 piped into a shell, string-building inside interpreter
     code. When the name appears literally the call is refused fail-closed;
     when it does not, nothing here can tell.
  3. A script written to disk first (``file_write`` then ``run_command
     grant.bat``), and ``powershell -File`` / ``bash script.sh``.
  4. A renamed copy of the launcher that was MODIFIED (different bytes), or a
     launcher for another interpreter pointed at ``sharing_on.cli``.
  5. Non-subprocess actuation: ``type_text`` / ``keyboard_shortcut`` into an
     operator terminal window, browser automation, and operator-connected MCP
     servers that expose their own shell tools (``systemu/runtime/mcp/``).
  6. Version skew at a NON-program position: ``env systemu <verb>`` where
     ``<verb>`` exists only in a newer installed CLI than the loaded table is
     treated as an argument to another program. At the program position an
     unknown verb is refused.

Loading the command table imports ``sharing_on.cli`` (and through it
``systemu.interface.cli_commands``) into the calling process on the FIRST call
that names systemu; that import loads ``.env`` from the working directory with
``override=False`` and switches stdout to UTF-8, as the CLI itself does.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shlex
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The refusal sentence. Its wording is a contract with the operator surface.
REFUSAL_LEAD = "systemu operator commands can only be run by you, in your own terminal: "
#: The fail-closed reason (DEC-27).
UNCERTAIN_REASON = "could not establish this does not run a systemu operator command"
#: Appended when a standing approval for the exact command was on record.
STANDING_APPROVAL_IGNORED = ("A standing approval on record for this exact command was "
                             "ignored: no approval covers a systemu operator command.")

#: Tools that spawn a process from a parameter. Others qualify by their
#: ``shell_exec`` effect tag (see :func:`refuse_operator_command`).
SPAWNING_TOOL_NAMES = frozenset({"run_command", "run_cli_command", "launch_application"})

OPERATOR_HIT = "operator"
UNCERTAIN_HIT = "uncertain"

#: Floor for the program names; the installed entry points are added to it.
_CLI_NAME_FLOOR = ("systemu", "sharing_on", "sharing-on")
_ENTRY_TARGET_PREFIX = "sharing_on.cli"
_MODULE_ROOTS = ("systemu", "sharing_on")
#: Modules whose ``-m`` run IS the CLI (the rest of argv is the CLI's argv).
_CLI_ENTRY_MODULES = frozenset({"systemu", "systemu.__main__", "sharing_on",
                                "sharing_on.__main__", "sharing_on.cli"})
#: Programs whose arguments are themselves a command line.
_SHELL_PROGRAMS = frozenset({"cmd", "bash", "sh", "zsh", "dash", "ksh", "fish",
                             "powershell", "pwsh", "busybox", "wsl"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_PYTHON_RE = re.compile(r"^(python|pythonw|py|pyw|pypy)(\d+(\.\d+)*)?w?$")
_PY_NOARG_FLAGS = "bBdEhiIOPqsSuvR"
_PY_MODULE_CLUSTER = re.compile(r"^-[" + _PY_NOARG_FLAGS + r"]*m(.*)$")
_PY_CODE_CLUSTER = re.compile(r"^-[" + _PY_NOARG_FLAGS + r"]*c$")
_PY_VALUE_FLAGS = frozenset({"-W", "-X", "--check-hash-based-pycs"})
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-E", "-p", "-r", "--eval", "--exec",
                                "--command", "--print", "-command"})
_SUFFIXES = ("-script.pyw", "-script.py", ".exe", ".cmd", ".bat", ".ps1", ".pyw",
             ".py", ".com", ".sh")
_QUOTEISH = "\"'`^"
#: Characters that make a token an EXPRESSION rather than a word. A name inside
#: one is a name this fence cannot read around.
_CODEISH = frozenset("[]{}$`%*?,!;")
_SHELL_PUNCT = frozenset(";&|()<>\n")
_SECRET_OPTION = re.compile(
    r"^--?[\w-]*(pass(word|phrase)?|secret|token|key)[\w-]*$", re.IGNORECASE)
_SECRET_OPTION_IN_TEXT = re.compile(
    r"(--?[\w-]*(?:pass(?:word|phrase)?|secret|token|key)[\w-]*)(=|\s+)(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE)
_MAX_DEPTH = 6
_MAX_VALUE_DEPTH = 4
_DISPLAY_CAP = 500
_UNDECODABLE = object()


# ─────────────────────────────────────────────────────────────────────────────
# The verdict
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OperatorCommandRefusal:
    """The fence's verdict on a call it refuses. ``message`` is what the agent and
    the operator both read; it is ASCII (DEC-32c) and redacted (DEC-31)."""

    message: str
    command: str
    reason: str
    detail: str
    standing_approval_ignored: bool
    tool_name: str

    def as_parsed(self) -> Dict[str, Any]:
        return {"success": False, "error": self.message,
                "error_type": "operator_command_refused",
                "operator_command": self.command,
                "refusal_reason": self.reason,
                "standing_approval_ignored": self.standing_approval_ignored}


@dataclass(frozen=True)
class _Hit:
    kind: str
    command: str      # the systemu command to show (operator hits), else ""
    detail: str


@dataclass(frozen=True)
class _Access:
    status: str       # "operator" | "agent" | "unknown" | "unclassified" | "unavailable"
    path: Tuple[str, ...]
    detail: str = ""
    at_option: bool = False


class _Unparseable(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────────────────────

_cli_names_cache: Optional[Tuple[str, ...]] = None


def cli_program_names() -> Tuple[str, ...]:
    """Lower-cased program names that run the systemu CLI: the floor plus every
    installed console script whose entry point is ``sharing_on.cli``."""
    global _cli_names_cache
    if _cli_names_cache is not None:
        return _cli_names_cache
    names = set(_CLI_NAME_FLOOR)
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="console_scripts"):
            value = getattr(ep, "value", "")
            name = getattr(ep, "name", "")
            if (type(value) is str and type(name) is str and name
                    and value.strip().startswith(_ENTRY_TARGET_PREFIX)):
                names.add(name.lower())
    except Exception:
        logger.debug("[OperatorCLIFence] entry points unavailable", exc_info=True)
    _cli_names_cache = tuple(sorted(names))
    return _cli_names_cache


def _needles() -> Tuple[str, ...]:
    return tuple(sorted(set(cli_program_names()) | set(_MODULE_ROOTS)))


_resolve_cache: Dict[Tuple[str, str, bool, str], Optional[Path]] = {}
_scripts_cache: Dict[Tuple[str, str], List[Path]] = {}


def _mentions(text: Any) -> bool:
    """Does ``text`` name systemu / sharing_on, once quoting and escapes that a
    shell would remove (``sys''temu``, ``sys^temu``, ``"sys"temu``) are gone?"""
    if type(text) is not str or not text:
        return False
    t = text.lower()
    for ch in _QUOTEISH + "\\":
        t = t.replace(ch, "")
    return any(n in t for n in _needles())


def _clean(tok: str) -> str:
    """A token as a program would receive it, for matching only."""
    out = tok
    for ch in _QUOTEISH:
        out = out.replace(ch, "")
    return out


def _program_name(tok: str) -> str:
    """The lower-cased basename of a program token, launcher suffixes removed."""
    t = _clean(tok).lstrip("@&(").rstrip(");,")
    t = t.split("=")[-1]
    parts = [p for p in re.split(r"[\\/]", t) if p]
    if not parts:
        return ""
    base = parts[-1].split(":")[-1].lower()
    for suffix in _SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def _is_cli_name(tok: str) -> bool:
    return _program_name(tok) in cli_program_names()


def _has_ws(tok: str) -> bool:
    return any(c.isspace() for c in tok)


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in _SHELL_PUNCT for c in tok)


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizers -- each returns a list of segments (lists of tokens)
# ─────────────────────────────────────────────────────────────────────────────

def _tokenize_cmd(s: str) -> List[List[str]]:
    """cmd.exe rules, approximately: ``"`` quotes, ``^`` escapes, ``& && | ||``,
    newlines and parentheses separate commands, redirections are dropped with
    their target. Never fails (cmd tolerates an unterminated quote)."""
    segments: List[List[str]] = []
    tokens: List[str] = []
    cur: List[str] = []
    started = False
    in_q = False
    i, n = 0, len(s)

    def end_token():
        nonlocal cur, started
        if started:
            tokens.append("".join(cur))
        cur, started = [], False

    def end_segment():
        nonlocal tokens
        end_token()
        if tokens:
            segments.append(tokens)
        tokens = []

    while i < n:
        c = s[i]
        if in_q:
            if c == '"':
                in_q = False
            elif c in "\r\n":
                in_q = False
                end_segment()
            else:
                cur.append(c)
            i += 1
            continue
        if c == '"':
            in_q, started = True, True
            i += 1
            continue
        if c == "^":
            if i + 1 < n and s[i + 1] not in "\r\n":
                cur.append(s[i + 1])
                started = True
            i += 2
            continue
        if c in " \t":
            end_token()
            i += 1
            continue
        if c in "\r\n&|()":
            end_segment()
            i += 1
            continue
        if c in "<>":
            if started and "".join(cur).isdigit():
                cur, started = [], False       # the handle of `2>`
            else:
                end_token()
            i += 1
            while i < n and s[i] == ">":
                i += 1
            if i < n and s[i] == "&":
                i += 1
                while i < n and s[i].isdigit():
                    i += 1
                continue
            while i < n and s[i] in " \t":
                i += 1
            quoted = False
            while i < n and (quoted or s[i] not in " \t\r\n&|()<>"):
                if s[i] == '"':
                    quoted = not quoted
                i += 1
            continue
        cur.append(c)
        started = True
        i += 1
    end_segment()
    return segments


def _tokenize_posix(s: str) -> List[List[str]]:
    """POSIX-sh rules via shlex: quotes, backslash escapes, punctuation runs as
    separators, newlines as separators, redirections dropped with their target."""
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        raw = list(lex)
    except ValueError as exc:
        raise _Unparseable(str(exc)) from None
    segments: List[List[str]] = []
    tokens: List[str] = []
    skip_next = False
    for tok in raw:
        if skip_next:
            skip_next = False
            if not _is_punct(tok):
                continue
        if _is_punct(tok):
            if "<" in tok or ">" in tok:
                skip_next = True
                continue
            if tokens:
                segments.append(tokens)
            tokens = []
            continue
        tokens.append(tok)
    if tokens:
        segments.append(tokens)
    return segments


def _tokenize_shlex(s: str) -> List[List[str]]:
    """Exactly ``run_cli_command``'s tokenizer: one argv, no shell."""
    try:
        toks = shlex.split(s)
    except ValueError as exc:
        raise _Unparseable(str(exc)) from None
    return [toks] if toks else []


def _tokenize_split(s: str) -> List[List[str]]:
    """The command gate's own tokenizer (``is_readonly_shell_command``)."""
    toks = s.split()
    return [toks] if toks else []


_TOKENIZERS = (("cmd", _tokenize_cmd), ("posix", _tokenize_posix),
               ("shlex", _tokenize_shlex), ("split", _tokenize_split))


def _has_dynamic(flavor: str, s: str) -> bool:
    if flavor in ("posix", "shlex"):
        return "$" in s or "`" in s
    if flavor == "cmd":
        return bool(re.search(r"%[^%\s]*%", s) or re.search(r"![^!\s]+!", s))
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Console-script files (copies and links of the launcher)
# ─────────────────────────────────────────────────────────────────────────────

_hash_cache: Dict[Tuple[str, int, int], str] = {}


def _console_script_files() -> List[Path]:
    """Existing files that ARE the installed systemu console scripts (cached per
    PATH; a reinstall changes the bytes, which the hash cache keys on)."""
    key = (os.environ.get("PATH", ""), sys.executable)
    cached = _scripts_cache.get(key)
    if cached is not None:
        return cached
    _scripts_cache.clear()
    _scripts_cache[key] = found = _discover_console_script_files()
    return found


def _discover_console_script_files() -> List[Path]:
    dirs: List[Path] = []
    for scheme_dir in _scripts_dirs():
        if scheme_dir not in dirs:
            dirs.append(scheme_dir)
    found: List[Path] = []
    names = cli_program_names()
    for name in names:
        hit = shutil.which(name)
        if hit:
            found.append(Path(hit))
        for d in dirs:
            for suffix in ("", ".exe", "-script.py", "-script.pyw", ".cmd", ".bat"):
                p = d / (name + suffix)
                if p.is_file():
                    found.append(p)
    out: List[Path] = []
    seen = set()
    for p in found:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _scripts_dirs() -> List[Path]:
    dirs: List[Path] = []
    try:
        dirs.append(Path(sysconfig.get_path("scripts")))
    except Exception:
        pass
    try:
        dirs.append(Path(sysconfig.get_path("scripts", f"{os.name}_user")))
    except Exception:
        pass
    exe_dir = Path(sys.executable).parent
    dirs.extend([exe_dir, exe_dir / "Scripts", exe_dir / "bin"])
    return dirs


def _sha256(path: Path) -> str:
    st = path.stat()
    key = (os.path.normcase(str(path)), st.st_size, st.st_mtime_ns)
    hit = _hash_cache.get(key)
    if hit is None:
        hit = hashlib.sha256(path.read_bytes()).hexdigest()
        _hash_cache[key] = hit
    return hit


def _resolve_program_path(tok: str, cwd: str, bare_lookup: bool) -> Optional[Path]:
    key = (tok, cwd, bare_lookup, os.environ.get("PATH", ""))
    if key in _resolve_cache:
        hit = _resolve_cache[key]
        if hit is None or hit.is_file():
            return hit
    if len(_resolve_cache) > 512:
        _resolve_cache.clear()
    _resolve_cache[key] = found = _resolve_program_path_uncached(tok, cwd, bare_lookup)
    return found


def _resolve_program_path_uncached(tok: str, cwd: str, bare_lookup: bool) -> Optional[Path]:
    c = _clean(tok)
    if not c or len(c) > 1024 or any(ch in c for ch in "*?\n\r\0"):
        return None
    base_dir = Path(cwd) if cwd else Path(os.getcwd())
    exts = [""]
    if os.name == "nt":
        exts += [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    try:
        if "/" in c or "\\" in c:
            p = Path(c)
            if not p.is_absolute():
                p = base_dir / p
            for ext in exts:
                q = Path(str(p) + ext)
                if q.is_file():
                    return q
            return None
        if not bare_lookup:
            return None
        for ext in exts:
            q = base_dir / (c + ext)
            if q.is_file():
                return q
        hit = shutil.which(c)
        return Path(hit) if hit else None
    except (OSError, ValueError):
        return None


def _is_console_script_file(tok: str, cwd: str, *, bare_lookup: bool) -> bool:
    path = _resolve_program_path(tok, cwd, bare_lookup)
    if path is None:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    for script in _console_script_files():
        try:
            if os.path.samefile(path, script):
                return True
            if script.stat().st_size == size and _sha256(script) == _sha256(path):
                return True
        except OSError:
            continue
    return False


def _path_like(tok: str) -> bool:
    c = _clean(tok)
    return "/" in c or "\\" in c


# ─────────────────────────────────────────────────────────────────────────────
# The verb: resolved against the live click tree and its registry
# ─────────────────────────────────────────────────────────────────────────────

def _cli_root():
    """The live root click group. Imported lazily: only a call that names
    systemu ever pays for it (see the module docstring)."""
    from sharing_on.cli import cli
    return cli


def _option_kind(command, tok: str) -> Optional[str]:
    """``"flag"`` / ``"value"`` / ``"attached"`` for an option ``command`` accepts,
    or None when click would reject it."""
    import click
    name = tok.split("=", 1)[0]
    help_names = list((getattr(command, "context_settings", None) or {})
                      .get("help_option_names", ["--help"]))
    if name in help_names:
        return "flag"
    for param in getattr(command, "params", ()):
        if not isinstance(param, click.Option):
            continue
        opts = list(param.opts) + list(param.secondary_opts)
        takes_value = not (param.is_flag or param.count)
        if name in opts:
            if not takes_value:
                return "flag"
            return "attached" if "=" in tok else "value"
        if (len(tok) > 2 and tok[0] == "-" and tok[1] != "-" and tok[:2] in opts
                and takes_value):
            return "attached"
    if re.fullmatch(r"-[A-Za-z]{2,}", tok):
        kinds = [_option_kind(command, "-" + ch) for ch in tok[1:]]
        if kinds and all(k == "flag" for k in kinds):
            return "flag"
    return None


def _escalates(command, rest: Sequence[str]) -> bool:
    """Does any of the leaf's declared operator options appear in ``rest``?"""
    import click
    from sharing_on.cli_access import operator_options_of
    wanted = operator_options_of(command)
    if not wanted:
        return False
    params = {p.name: p for p in getattr(command, "params", ())
              if isinstance(p, click.Option)}
    opts: List[str] = []
    for name in wanted:
        param = params.get(name)
        if param is None:
            return True               # a declared escalation that cannot be checked
        opts.extend(list(param.opts) + list(param.secondary_opts))
    for raw in rest:
        tok = _clean(raw)
        for opt in opts:
            if tok == opt or tok.startswith(opt + "="):
                return True
            if (len(opt) == 2 and opt[0] == "-" and re.fullmatch(r"-[A-Za-z]+.*", tok)
                    and not tok.startswith("--") and opt[1] in tok[1:]):
                return True
    return False


def resolve_access(argv: Sequence[str]) -> _Access:
    """Walk the live click tree along ``argv`` and read the registry.

    Mirrors click's own resolution (exact subcommand names, group options) and
    stops at shell punctuation. Anything it cannot follow is reported, never
    guessed: the caller decides what an unknown verb means.
    """
    import click
    try:
        from sharing_on.cli_access import AGENT, OPERATOR, access_of
        root = _cli_root()
    except Exception as exc:  # noqa: BLE001 -- an unloadable table is a verdict
        return _Access("unavailable", (), "the systemu command table could not be "
                       "loaded ({})".format(type(exc).__name__))
    args: List[str] = []
    for raw in argv:
        if _is_punct(raw):
            break
        args.append(_clean(raw))
    node = root
    path: List[str] = []
    i = 0
    while True:
        access = access_of(node)
        label = " ".join(["systemu"] + path)
        if access is None:
            return _Access("unclassified", tuple(path),
                           "`{}` carries no access classification".format(label))
        if not isinstance(node, click.Group):
            if access == AGENT and _escalates(node, args[i:]):
                return _Access(OPERATOR, tuple(path),
                               "`{}` with that option is operator-only".format(label))
            return _Access(access, tuple(path))
        if (type(node).get_command is not click.Group.get_command
                or type(node).resolve_command is not click.Group.resolve_command):
            return _Access("unclassified", tuple(path),
                           "`{}` resolves its commands dynamically".format(label))
        sub = None
        while i < len(args):
            tok = args[i]
            if tok == "--":
                i += 1
                if i < len(args):
                    sub = args[i]
                    i += 1
                break
            if tok.startswith("-") and tok != "-":
                kind = _option_kind(node, tok)
                if kind is None:
                    return _Access("unknown", tuple(path),
                                   "`{}` has no option {}".format(label, tok),
                                   at_option=True)
                i += 2 if kind == "value" else 1
                continue
            sub = tok
            i += 1
            break
        if sub is None:
            return _Access(access, tuple(path))
        child = node.commands.get(sub)
        if child is None:
            return _Access("unknown", tuple(path),
                           "`{}` has no command {}".format(label, sub))
        path.append(sub)
        node = child


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def _quote(tok: str) -> str:
    if tok and not any(c.isspace() or c in "\"'&|<>^;" for c in tok):
        return tok
    return '"' + tok.replace('"', '\\"') + '"'


def _display(argv: Sequence[str]) -> str:
    """``systemu <argv>`` as the operator would type it, secret option values
    masked structurally (the text redactor runs over it afterwards)."""
    shown: List[str] = []
    mask_next = False
    for raw in argv:
        if _is_punct(raw):
            break
        tok = _clean(raw)
        if mask_next and not tok.startswith("-"):
            shown.append("<redacted>")
            mask_next = False
            continue
        mask_next = False
        name, eq, _value = tok.partition("=")
        if _SECRET_OPTION.match(name):
            if eq:
                shown.append(name + "=<redacted>")
                continue
            mask_next = True
        shown.append(_quote(tok))
    return " ".join(["systemu"] + shown)


def _classify(argv: Sequence[str], *, real: bool) -> Optional[_Hit]:
    """``real`` -- ``argv`` is what the CLI will actually receive (the token was
    the program). Otherwise the name sat inside another program's arguments."""
    res = resolve_access(argv)
    if res.status == "agent":
        return None
    if res.status == "operator":
        return _Hit(OPERATOR_HIT, _display(argv), res.detail or
                    "`{}` is an operator-only command".format(
                        " ".join(("systemu",) + res.path)))
    if res.status == "unknown" and not real and not res.at_option:
        return None       # e.g. `git add systemu sharing_on`: an argument, not a verb
    return _Hit(UNCERTAIN_HIT, "", res.detail)


def _module_at(tokens: Sequence[str], k: int) -> Optional[Tuple[str, int]]:
    """``(module, argv_start)`` when ``tokens[k]`` is ``-m`` (or a ``-Im``-style
    cluster) naming a module, else None."""
    tok = _clean(tokens[k])
    m = _PY_MODULE_CLUSTER.match(tok)
    if not m:
        return None
    if m.group(1):
        return m.group(1), k + 1
    if k + 1 < len(tokens):
        return _clean(tokens[k + 1]), k + 2
    return None


def _script_index(tokens: Sequence[str], p: int) -> Optional[int]:
    j = p + 1
    while j < len(tokens):
        t = _clean(tokens[j])
        if _PY_MODULE_CLUSTER.match(t) or _PY_CODE_CLUSTER.match(t) or t == "-":
            return None
        if t in _PY_VALUE_FLAGS:
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        return j
    return None


def _decode_powershell(token: str):
    s = _clean(token).strip()
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=False).decode("utf-16-le")
    except Exception:  # noqa: BLE001 -- undecodable is a verdict, not an error
        return _UNDECODABLE


def _nested(tokens: Sequence[str], p: int, prog: str) -> List[Any]:
    """The command lines nested inside a segment -- THE wrapper unwrapping.

    * a shell wrapper's payload (``cmd /c``, ``bash -c``, ``powershell
      -Command``): the arguments after the program, switches dropped;
    * ``-EncodedCommand``, decoded (or ``_UNDECODABLE``);
    * any quoted string that names systemu, wherever it sits."""
    out: List[Any] = []
    if prog in _SHELL_PROGRAMS:
        tail = [t for t in tokens[p + 1:]
                if not (t.startswith("-") or re.fullmatch(r"/[A-Za-z](:\S*)?", t))]
        if tail:
            out.append(" ".join(tail))
        if prog in _POWERSHELLS:
            for j in range(p + 1, len(tokens) - 1):
                t = tokens[j]
                if t[:1] not in ("-", "/"):
                    continue
                name = t[1:].lower()
                if name.startswith("e") and (name == "ec" or "encodedcommand".startswith(name)):
                    out.append(_decode_powershell(tokens[j + 1]))
    for t in tokens:
        if _has_ws(t) and _mentions(t):
            out.append(t)
    seen: List[Any] = []
    for item in out:
        if item is _UNDECODABLE or item not in seen:
            seen.append(item)
    return seen


def _program_index(tokens: Sequence[str]) -> int:
    p = 0
    while p < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[p]):
        p += 1
    return p


def _analyze_segment(tokens: Sequence[str], *, cwd: str, depth: int) -> Optional[_Hit]:
    hits: List[_Hit] = []

    def uncertain(detail: str) -> None:
        hits.append(_Hit(UNCERTAIN_HIT, "", detail))

    tokens = [t for t in tokens if type(t) is str]
    if not tokens:
        return None
    p = _program_index(tokens)
    prog = _program_name(tokens[p]) if p < len(tokens) else ""
    python_prog = bool(_PYTHON_RE.match(prog))
    script_at = _script_index(tokens, p) if python_prog else None
    module_tokens = set()

    for k, tok in enumerate(tokens):
        real = (k == p) or (k == script_at)
        if k in module_tokens:
            continue
        # 1. a systemu console script, by name, anywhere in the segment
        if _is_cli_name(tok):
            hit = _classify(tokens[k + 1:], real=real)
            if hit:
                hits.append(hit)
            continue
        # 2. the installed console script by FILE (a link or a copy of it)
        if (real or _path_like(tok)) and _is_console_script_file(
                tok, cwd, bare_lookup=(k == p)):
            hit = _classify(tokens[k + 1:], real=real)
            if hit:
                hits.append(hit)
            continue
        # 3. -m <module> for any interpreter
        mod = _module_at(tokens, k)
        if mod is not None:
            name, start = mod
            module_tokens.update(range(k + 1, start))
            if name.split(".")[0].lower() in _MODULE_ROOTS:
                if name.lower() in _CLI_ENTRY_MODULES:
                    hit = _classify(tokens[start:], real=python_prog)
                    if hit:
                        hits.append(hit)
                else:
                    uncertain("it runs the systemu module {}".format(name))
            continue
        if k == p:
            # 4. a program whose name starts like systemu but is not the CLI
            if prog and not _is_cli_name(tok) and any(
                    prog.startswith(n) for n in cli_program_names()):
                uncertain("it runs {}, a systemu program this fence does not "
                          "recognise".format(prog))
        if k == script_at and _mentions(tok):
            # 5. an interpreter running a file from the systemu package
            parts = [x.lower() for x in re.split(r"[\\/]", _clean(tok)) if x]
            if any(x in _MODULE_ROOTS for x in parts[:-1]):
                if parts[-1] == "__main__.py" and len(parts) > 1 and parts[-2] in _MODULE_ROOTS:
                    hit = _classify(tokens[k + 1:], real=True)
                    if hit:
                        hits.append(hit)
                else:
                    uncertain("it runs a file from the systemu package")
                continue
        # 6. inline interpreter code that names systemu
        if prog not in _SHELL_PROGRAMS and k + 1 < len(tokens) and (
                _clean(tok) in _INLINE_CODE_FLAGS
                or (python_prog and _PY_CODE_CLUSTER.match(_clean(tok)))) \
                and _mentions(tokens[k + 1]):
            uncertain("it hands code that names systemu to an interpreter")
            continue
        if python_prog and k > p and tok.startswith("-") and not tok.startswith("--") \
                and _mentions(tok):
            uncertain("it hands code that names systemu to an interpreter")
            continue
        # 7. the name inside an expression
        if _mentions(tok) and not _has_ws(tok) and any(c in _CODEISH for c in tok):
            uncertain("it names systemu inside an expression")

    for payload in _nested(tokens, p, prog):
        if payload is _UNDECODABLE:
            uncertain("an encoded PowerShell command could not be decoded")
            continue
        hit = _analyze_text(payload, cwd=cwd, depth=depth + 1)
        if hit:
            hits.append(hit)

    for hit in hits:
        if hit.kind == OPERATOR_HIT:
            return hit
    return hits[0] if hits else None


def _analyze_text(text: str, *, cwd: str, depth: int = 0) -> Optional[_Hit]:
    if depth > _MAX_DEPTH:
        return (_Hit(UNCERTAIN_HIT, "", "it nests commands too deeply to follow")
                if _mentions(text) else None)
    uncertain_hit: Optional[_Hit] = None
    for flavor, tokenize in _TOKENIZERS:
        try:
            segments = tokenize(text)
        except _Unparseable:
            if _mentions(text) and uncertain_hit is None:
                uncertain_hit = _Hit(UNCERTAIN_HIT, "",
                                     "its quoting could not be parsed ({} rules)".format(flavor))
            continue
        if uncertain_hit is None and _has_dynamic(flavor, text) and _mentions(text):
            uncertain_hit = _Hit(UNCERTAIN_HIT, "", "it expands variables or substitutes "
                                 "commands when it runs")
        for seg in segments:
            hit = _analyze_segment(seg, cwd=cwd, depth=depth)
            if hit is None:
                continue
            if hit.kind == OPERATOR_HIT:
                return hit
            if uncertain_hit is None:
                uncertain_hit = hit
    return uncertain_hit


def analyze_command(command: Any, *, cwd: str = "") -> Optional[_Hit]:
    """The fence's verdict on ONE command value: a string (a command line) or a
    list of strings (an argv). None = it does not run a systemu operator command
    as far as this fence can establish (see the residuals in the module doc)."""
    if type(command) is str:
        return _analyze_text(command, cwd=cwd if type(cwd) is str else "")
    if type(command) in (list, tuple) and all(type(t) is str for t in command):
        return _analyze_segment(list(command), cwd=cwd if type(cwd) is str else "",
                                depth=0)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The tool-call fence
# ─────────────────────────────────────────────────────────────────────────────

def _command_values(params: Any, depth: int = 0) -> Iterator[Any]:
    """Every string (and every all-string argv) a call carries.

    Subclasses are read through the BASE type's C-level accessors (DEC-36): a
    ``str``/``dict``/``list`` subclass whose ``__str__``/``values``/``__iter__``
    lies is still read as the value the tool will actually receive, never
    skipped -- skipping would be the bypass."""
    if depth > _MAX_VALUE_DEPTH:
        return
    if isinstance(params, str):
        yield str.__str__(params)
    elif isinstance(params, dict):
        for value in dict.values(params):
            yield from _command_values(value, depth + 1)
    elif isinstance(params, (list, tuple)):
        items = list(list.__iter__(params) if isinstance(params, list)
                     else tuple.__iter__(params))
        if items and all(isinstance(v, str) for v in items):
            yield [str.__str__(v) for v in items]
        else:
            for value in items:
                yield from _command_values(value, depth + 1)


def _param(parameters: Any, key: str) -> str:
    """``parameters[key]`` as an exact str, or "" (C-level read, type-pinned)."""
    if not isinstance(parameters, dict):
        return ""
    value = dict.get(parameters, key)
    return str.__str__(value) if isinstance(value, str) else ""


def _value_text(value: Any) -> str:
    return value if type(value) is str else " ".join(value)


def _looks_encoded(text: str) -> bool:
    return bool(re.search(r"(^|\s)[-/]e(c|n\w*)?\s", text, re.IGNORECASE))


def _effect_tags(tool, impl_path) -> set:
    tags = set()
    for t in (getattr(tool, "effect_tags", None) or []) if tool is not None else []:
        v = getattr(t, "value", t)
        if type(v) is str:
            tags.add(v)
    if not tags and impl_path is not None:
        try:
            from systemu.runtime.tool_sandbox import _derive_effect_tags_from_source
            tags = set(_derive_effect_tags_from_source(impl_path))
        except Exception:
            logger.debug("[OperatorCLIFence] tag derivation failed", exc_info=True)
    return tags


def _redact_display(text: str) -> str:
    masked = _SECRET_OPTION_IN_TEXT.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", text)
    try:
        from systemu.runtime.outbox import redact
        return redact(masked, max_len=_DISPLAY_CAP)
    except Exception:  # noqa: BLE001 -- DEC-31: no redactor, no value
        logger.debug("[OperatorCLIFence] redaction unavailable", exc_info=True)
        return "[command withheld: redaction unavailable]"


def _standing_approval_on_record(parameters: Any, approvals) -> bool:
    """Read-only: is a standing command-gate approval on record for this exact
    call? Mirrors the key ``ToolSandbox._maybe_gate_command`` uses. Never
    consumes anything (``is_approved`` only reads the file)."""
    command = _param(parameters, "command") or _param(parameters, "cmd")
    cwd = _param(parameters, "cwd")
    try:
        from systemu.runtime.command_approvals import command_signature, init_default_store
        store = approvals
        if store is None:
            store = init_default_store(Path("data"))
        return bool(store.is_approved(command_signature(str(command), cwd=str(cwd))))
    except Exception:  # noqa: BLE001 -- the refusal stands either way
        logger.debug("[OperatorCLIFence] could not read approvals", exc_info=True)
        return False


def _emit_event(refusal: OperatorCommandRefusal) -> None:
    """Say it where the operator reads. Best-effort: the notification layer must
    never be what decides whether the call runs (the verdict is already made)."""
    try:
        from systemu.interface.notifications import log_event
        log_event("WARNING", "tool", refusal.message,
                  {"tool_name": refusal.tool_name[:80], "reason": refusal.reason,
                   "standing_approval_ignored": refusal.standing_approval_ignored})
    except Exception:
        logger.debug("[OperatorCLIFence] could not write the refusal event", exc_info=True)


def refuse_operator_command(tool_name: Any, parameters: Any, *, tool=None,
                            impl_path=None, approvals=None,
                            emit: bool = True) -> Optional[OperatorCommandRefusal]:
    """The refusal for a tool call that would run a systemu operator command, or
    None. Called before ANY gate or execution; a non-None return is the call's
    result and nothing else may run it.

    Fail-closed on its own faults (DEC-27): if the analysis itself breaks, a call
    to a process-spawning tool, or one whose parameters name systemu, is refused
    -- a fence that crashed has established nothing."""
    try:
        return _refuse(tool_name, parameters, tool=tool, impl_path=impl_path,
                       approvals=approvals, emit=emit)
    except Exception as exc:  # noqa: BLE001 -- a fault is a verdict, not a pass
        logger.exception("[OperatorCLIFence] analysis failed")
        try:
            name = tool_name if type(tool_name) is str else ""
            texts = [_value_text(v) for v in _command_values(parameters)]
            declared = getattr(tool, "name", None)
            spawning = name in SPAWNING_TOOL_NAMES or (
                type(declared) is str and declared in SPAWNING_TOOL_NAMES)
            if not texts or not (spawning or any(_mentions(t) for t in texts)):
                return None
        except Exception:  # noqa: BLE001
            name = ""
        from systemu.runtime.command_approvals import _ascii
        detail = "the fence could not analyse it ({})".format(type(exc).__name__)
        message = ("Refused: " + UNCERTAIN_REASON + " (" + detail + "). "
                   + REFUSAL_LEAD + "[command withheld]. Nothing was run.")
        refusal = OperatorCommandRefusal(
            message=_ascii(message), command="[command withheld]", reason="uncertain",
            detail=_ascii(detail), standing_approval_ignored=False,
            tool_name=_ascii(name))
        if emit:
            _emit_event(refusal)
        return refusal


def _refuse(tool_name: Any, parameters: Any, *, tool, impl_path, approvals,
            emit: bool) -> Optional[OperatorCommandRefusal]:
    from systemu.runtime.command_approvals import _ascii

    name = tool_name if type(tool_name) is str else ""
    names = {name}
    declared = getattr(tool, "name", None) if tool is not None else None
    if type(declared) is str:
        names.add(declared)
    values = list(_command_values(parameters))
    if not values:
        return None
    if not (names & SPAWNING_TOOL_NAMES):
        texts = [_value_text(v) for v in values]
        if not any(_mentions(t) or _looks_encoded(t) for t in texts):
            return None
        if "shell_exec" not in _effect_tags(tool, impl_path):
            return None
    cwd = _param(parameters, "cwd")

    hit: Optional[_Hit] = None
    source = ""
    for value in values:
        h = analyze_command(value, cwd=cwd)
        if h is None:
            continue
        if h.kind == OPERATOR_HIT:
            hit, source = h, _value_text(value)
            break
        if hit is None:
            hit, source = h, _value_text(value)
    if hit is None:
        return None

    ignored = _standing_approval_on_record(parameters, approvals)
    # DEC-31: every piece of agent-supplied text is redacted (and only then
    # capped) before it is composed into the message -- the detail too, since it
    # can quote an option or a verb the agent wrote.
    detail = _redact_display(hit.detail)
    if hit.kind == OPERATOR_HIT:
        shown = _redact_display(hit.command)
        message = REFUSAL_LEAD + shown + ". Nothing was run. (" + detail + ".)"
        reason = "operator_command"
    else:
        shown = _redact_display(source.strip())
        message = ("Refused: " + UNCERTAIN_REASON + " (" + detail + "). "
                   + REFUSAL_LEAD + shown + ". Nothing was run.")
        reason = "uncertain"
    if ignored:
        message += " " + STANDING_APPROVAL_IGNORED
    refusal = OperatorCommandRefusal(
        message=_ascii(message), command=_ascii(shown), reason=reason,
        detail=_ascii(detail), standing_approval_ignored=ignored,
        tool_name=_ascii(name))
    logger.warning("[OperatorCLIFence] refused %s: %s", name, refusal.detail)
    if emit:
        _emit_event(refusal)
    return refusal
