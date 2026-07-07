"""v0.9.51 — schema-driven test-parameter synthesizer for the forge dry-run.

ONE recursive engine that emits a structurally-complete, constraint-valid instance
from an arbitrary JSON Schema and materializes a REAL fixture file at every
path-shaped string leaf — at ANY nesting depth (object / array / array-of-objects /
nested). It replaces the flat, scalar-only ``_schema_default_params`` +
``_sandbox_paths`` patchwork: "a path inside a list inside an object" is the same
code as "a top-level path", which is what dissolves the per-shape whack-a-mole.

Design (see the session's holistic-param-synthesis design):
  * SCHEMA-FIRST, deterministic — the dry-run only needs *valid inputs that run*,
    which a schema can produce without a flaky LLM. An optional LLM overlay (added
    later) is validated per-leaf before use and never owns a path value.
  * ONE path oracle applied at every string leaf (key-name / format / description).
  * Fixture materialization at any leaf; a list-of-paths → N distinct real files.
  * Recursive in STRUCTURE (object/array/$ref/anyOf/allOf), deliberately SHALLOW in
    constraint-solving (no regex-SMT / remote $ref) — the hard cases route to the
    caller's operator_verify, never a false failure.

Contract: ``synthesize_params(schema, ...) -> SynthResult(params, created_paths,
unresolved)``. ``params`` is always structurally complete; every path leaf points
at a real fixture under a per-run sandbox temp dir.
"""
from __future__ import annotations

import logging
import re
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_DEPTH = 6  # recursion floor (also bounds pathological/cyclic schemas)

# Private sentinel for the resolved-schema-value seam: a const/enum/default leaf
# routes its resolved value through leaf_fn as ``schema_value=<value>`` so a custom
# leaf_fn (the requirement binder) can observe it. A private sentinel (not None) is
# required so a leaf whose resolved value is legitimately None/False/0 still works.
class _Sentinel:
    __slots__ = ()
    def __repr__(self):  # pragma: no cover - debug aid only
        return "<fixture_synth._SENTINEL>"


_SENTINEL = _Sentinel()

# ── format-valid fixture bytes (lifted from tool_dry_run so a forged file/format
#    tool can actually OPEN its input during dry-run instead of choking on text) ──
_FIXTURE_EXTS = ("docx", "xlsx", "pptx", "pdf", "png", "jpg", "jpeg",
                 "zip", "csv", "json", "txt", "xls", "doc")
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da6360000002000154a24f5d0000000049454e44ae426082"
)
_MIN_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\ntrailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def write_fixture_file(p: Path, ext: str) -> None:
    """Write a FORMAT-VALID dry-run fixture at ``p`` for ``ext`` (real docx/xlsx/
    pdf/png/zip/json bytes; text fallback otherwise). Best-effort; never raises."""
    e = (ext or "").lower().lstrip(".")
    try:
        if e == "docx":
            import docx
            docx.Document().save(str(p)); return
        if e == "xlsx":
            import openpyxl
            openpyxl.Workbook().save(str(p)); return
        if e == "pdf":
            p.write_bytes(_MIN_PDF); return
        if e in ("png", "jpg", "jpeg"):
            p.write_bytes(_PNG_1x1); return
        if e == "zip":
            import zipfile
            with zipfile.ZipFile(str(p), "w"):
                pass
            return
        if e == "json":
            p.write_bytes(b"{}"); return
    except Exception:
        logger.debug("[FixtureSynth] format fixture for .%s failed; text fallback", e, exc_info=True)
    p.write_bytes(b"dry-run test payload\n")


def infer_fixture_ext(name: str, spec: Dict[str, Any], tool_name: str = "") -> str:
    """Best-effort extension for a path leaf, inferred from the param name, the
    tool name, the description, or a contentMediaType. Returns ``.<ext>`` or ``""``."""
    spec = spec if isinstance(spec, dict) else {}
    cmt = str(spec.get("contentMediaType") or "")
    desc = str(spec.get("description") or "")
    hay = " ".join(str(x).lower() for x in (name, tool_name or "", desc, cmt))
    for e in _FIXTURE_EXTS:
        if e in hay:
            return "." + e
    return ""


# ── path oracle — the ONE place "is this string a filesystem path?" is decided ──
_PATH_SUFFIXES = ("_path", "_file", "_dir", "_filepath")
_PATH_EXACT = {
    "output_path", "file_path", "dest", "destination", "output_dir", "path",
    "filepath", "out", "outfile", "input_path", "input_file", "infile",
    "in_path", "src", "source", "source_path", "data_path", "file", "dir",
}
# A token (key split on non-alphanumerics) matching any of these implies a path —
# so `files_to_add`, `sheet_xlsx`, `attachments`, `logo_png` are all detected the
# same way a top-level `input_path` is. Includes the fixture extensions so a key
# like `*_xlsx` / `*_png` (almost always a file path) is recognized.
_PATH_WORDS = {
    "file", "files", "filepath", "filename", "path", "paths", "dir", "dirs",
    "folder", "folders", "document", "documents", "attachment", "attachments",
    "src", "source", "sources", "dest", "destination", "input", "inputs",
    "output", "outputs", "infile", "outfile", "archive", "archives", "image",
    "images", "photo", "logo", "workbook", "spreadsheet",
}
_FIXTURE_EXT_SET = set(_FIXTURE_EXTS)
_DESC_PATH_RE = re.compile(r"\.(docx|xlsx|pptx|pdf|png|jpe?g|zip|csv|json|txt|xls|doc)\b")
_DESC_PATH_WORDS = ("path to", "file path", "filepath", "folder", "directory",
                    "filename", "to save", "save to", "the file", "input file",
                    "output file", "workbook", "document path")


def looks_like_path(key: str, spec: Dict[str, Any], *, tool_name: str = "") -> Tuple[bool, str, str]:
    """Return ``(is_path, kind, ext)`` for a string leaf, where ``kind`` is
    ``"file"`` or ``"dir"``. Unions every signal — JSON-Schema ``format``,
    ``contentMediaType``, key-name patterns, and the description — and is applied
    at EVERY string leaf regardless of nesting. ``ext`` is the inferred fixture
    extension (``""`` when unknown → a generic ``.dat`` fixture)."""
    spec = spec if isinstance(spec, dict) else {}
    kl = (key or "").lower()
    fmt = str(spec.get("format") or "").lower()
    desc = str(spec.get("description") or "").lower()

    # NB: this oracle deliberately leans toward classifying a leaf as a path. For
    # the dry-run the asymmetry favors it — a MISSED path false-FAILS (the tool
    # opens a value that isn't a real file → crash), whereas a path-for-text
    # false-positive usually still RUNS (a path string is a valid string). The
    # residual risk is a text leaf that expects *structured* content; that is
    # covered by the execution-time format-parse → operator_verify router.
    tokens = set(re.split(r"[^a-z0-9]+", kl)) - {""}
    is_dir = (kl.endswith("_dir") or kl in {"dir", "output_dir"}
              or fmt in {"directory", "directory-path"}
              or ("folder" in desc or "directory" in desc))
    is_file_key = (kl in _PATH_EXACT or kl.endswith(_PATH_SUFFIXES)
                   or bool(tokens & _PATH_WORDS) or bool(tokens & _FIXTURE_EXT_SET))
    is_format_path = (fmt in {"path", "file-path", "filepath", "uri", "iri", "binary"}
                      or bool(spec.get("contentMediaType")) or spec.get("contentEncoding") == "base64")
    is_desc_path = bool(_DESC_PATH_RE.search(desc)) or any(w in desc for w in _DESC_PATH_WORDS)

    if not (is_dir or is_file_key or is_format_path or is_desc_path):
        return (False, "", "")
    kind = "dir" if is_dir else "file"
    return (True, kind, infer_fixture_ext(key, spec, tool_name))


# ── leaf value generators (non-path) ──────────────────────────────────────────
_FORMAT_VALUES = {
    "email": "dryrun@example.com", "idn-email": "dryrun@example.com",
    "date": "2020-01-01", "date-time": "2020-01-01T00:00:00Z", "time": "00:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "uri": "https://example.com", "url": "https://example.com",
    "hostname": "example.com", "idn-hostname": "example.com",
    "ipv4": "127.0.0.1", "ipv6": "::1",
}


def _gen_string(spec: Dict[str, Any], required: bool) -> str:
    fmt = str(spec.get("format") or "").lower()
    if fmt in _FORMAT_VALUES:
        return _FORMAT_VALUES[fmt]
    ml = spec.get("minLength")
    if isinstance(ml, int) and ml > len("dryrun"):
        return "x" * ml
    return "dryrun" if required else ""


def _gen_number(spec: Dict[str, Any], integer: bool):
    lo, hi = spec.get("minimum"), spec.get("maximum")
    elo, ehi = spec.get("exclusiveMinimum"), spec.get("exclusiveMaximum")
    val: float = 0
    if isinstance(lo, (int, float)):
        val = lo
    elif isinstance(elo, (int, float)):
        val = elo + 1
    if isinstance(hi, (int, float)) and val > hi:
        val = hi
    elif isinstance(ehi, (int, float)) and val >= ehi:
        val = ehi - 1
    mult = spec.get("multipleOf")
    if isinstance(mult, (int, float)) and mult:
        val = (round(val / mult) or 1) * mult
    return int(val) if integer else float(val)


# ── context-grounding: real operator-referenced files → real dry-run inputs ────
_CANDIDATE_PATH_RE = re.compile(
    r"[^\s\"'<>|]*[\w-]+\.(?:docx|xlsx|pptx|pdf|png|jpe?g|zip|csv|json|txt|xls|doc)\b",
    re.IGNORECASE)


def extract_candidate_paths(*texts: str) -> List[str]:
    """Pull candidate file paths/names out of free text (a Scroll's raw_request /
    narrative) so the forge can persist them as a tool's grounding inputs. Returns
    de-duplicated, order-preserving matches; the consumer keeps only those that
    actually EXIST on disk at dry-run time, so a bare filename or stale path is a
    harmless no-op (the synthesizer falls back to a synthetic fixture)."""
    out: List[str] = []
    seen = set()
    for text in texts:
        for m in _CANDIDATE_PATH_RE.finditer(str(text or "")):
            p = m.group(0).strip().strip("'\"")
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
    return out


# ── the recursive walk ────────────────────────────────────────────────────────
@dataclass
class _Ctx:
    tool_name: str
    sandbox: Path
    grounding: List[str] = field(default_factory=list)
    created: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)

    def take_grounding(self, ext: str) -> Optional[str]:
        """Pop an unused real file matching ``ext`` (or, when the leaf gives no ext
        hint, any available real file) — the context-grounding source for a path
        leaf, so a content-dependent tool is dry-run against real content."""
        e = (ext or "").lower().lstrip(".")
        for i, g in enumerate(self.grounding):
            if os.path.isfile(g) and (not e or g.lower().endswith("." + e)):
                return self.grounding.pop(i)
        return None


def _first_type(t):
    if isinstance(t, list):
        for x in t:
            if x != "null":
                return x
        return t[0] if t else None
    return t


def _resolve_ref(node, root, seen):
    ref = node.get("$ref")
    if not ref or not isinstance(ref, str) or not ref.startswith("#"):
        return node, seen
    if ref in seen:
        return None, seen   # cycle → bottom out
    cur = root
    for part in ref.lstrip("#/").split("/"):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return node, seen
    return cur, seen | {ref}


def _materialize(kind: str, ext: str, key: str, ctx: _Ctx) -> str:
    if kind == "dir":
        d = ctx.sandbox / (re.sub(r"\W+", "_", key or "out_dir") or "out_dir")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        ctx.created.append(str(d))
        return str(d)
    safe = re.sub(r"\W+", "_", key or "input") or "input"
    # context-grounding: for an INPUT-ish leaf, prefer a COPY of a real operator-
    # referenced file of the same type so a content-dependent tool sees real
    # content. Output/destination leaves are written, not read → keep synthetic
    # (and don't burn a grounding file on them).
    _is_output = any(w in (key or "").lower() for w in ("output", "dest", "save", "target"))
    real = None if _is_output else ctx.take_grounding(ext)
    if real:
        rext = os.path.splitext(real)[1] or ext or ".dat"
        p = ctx.sandbox / f"{safe}_{len(ctx.created)}{rext}"
        try:
            shutil.copyfile(real, p)
            ctx.created.append(str(p))
            return str(p)
        except Exception:
            logger.debug("[FixtureSynth] grounding copy of %s failed; synthesizing", real, exc_info=True)
    e = ext or ".dat"
    # unique per leaf → a list-of-paths gets N DISTINCT real fixtures
    p = ctx.sandbox / f"{safe}_{len(ctx.created)}{e}"
    try:
        write_fixture_file(p, e)
        ctx.created.append(str(p))
    except Exception:
        logger.debug("[FixtureSynth] could not materialize %s", p, exc_info=True)
    return str(p)


def _synth_leaf(node, *, key: str, required: bool, kind: str, ext: str,
                ctx: _Ctx, path: tuple = (), schema_value=_SENTINEL,
                schema_value_kind=None) -> Any:
    """Produce the terminal-value for a scalar/path leaf — the DEFAULT ``leaf_fn``.

    ``kind`` is the path-oracle's classification (``"file"``/``"dir"`` for a path
    leaf, ``""`` otherwise) and ``ext`` its inferred fixture extension, both computed
    once by ``_walk`` from ``looks_like_path``. This reproduces the pre-refactor
    inline leaf body byte-for-byte: same ``_materialize`` calls, same
    ``ctx.unresolved`` flag, same ``_gen_string``/``_gen_number`` values. ``path`` is
    accepted (so a custom binder ``leaf_fn`` shares this call site) but unused here.

    ``schema_value``/``schema_value_kind`` carry a RESOLVED const/enum/default value
    (``schema_value_kind`` = "const"|"enum"|"default"). When set, this returns
    ``schema_value`` VERBATIM — byte-identical to the pre-refactor early-return in
    ``_walk`` (``return node["const"]`` / ``enum[0]`` / ``default``). A custom binder
    leaf_fn instead records a Requirement for it."""
    if schema_value is not _SENTINEL:                     # a const/enum/default leaf
        return schema_value                              # verbatim (old early-return)
    if kind:                                              # oracle classified a path leaf
        return _materialize(kind, ext, key, ctx)
    t = _first_type(node.get("type"))
    if t == "string":
        # A `pattern`-bound required string can't be guaranteed without a regex
        # engine — emit a best-effort value but FLAG it so the caller can degrade a
        # downstream dry-run failure to operator_verify instead of a doomed fail.
        if node.get("pattern") and required:
            ctx.unresolved.append(key or "(pattern)")
        return _gen_string(node, required)
    if t in ("integer", "int"):
        return _gen_number(node, True)
    if t in ("number", "float"):
        return _gen_number(node, False)
    if t == "boolean":
        return False
    # unknown/no type — if it has properties treat as object (handled in _walk); else
    # a bare leaf we can't type → None (the caller's required-field check / operator
    # _verify covers it).
    return None


def _walk(node, *, key: str, required: bool, root, ctx: _Ctx, depth: int, seen,
          leaf_fn=_synth_leaf, path: tuple = ()) -> Any:
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return None
    node, seen = _resolve_ref(node, root, seen)
    if node is None:
        return None
    # A const/enum/default leaf resolves to a fixed value. Route it THROUGH leaf_fn
    # (not an early return) so a custom leaf_fn — the requirement binder — observes it
    # with schema_value/schema_value_kind set. The default _synth_leaf returns
    # schema_value verbatim, byte-identical to the old early-return.
    if "const" in node:
        return leaf_fn(node, key=key, required=required, kind="", ext="", ctx=ctx,
                       path=path, schema_value=node["const"], schema_value_kind="const")
    if node.get("enum"):
        return leaf_fn(node, key=key, required=required, kind="", ext="", ctx=ctx,
                       path=path, schema_value=node["enum"][0], schema_value_kind="enum")
    if node.get("default") is not None:
        return leaf_fn(node, key=key, required=required, kind="", ext="", ctx=ctx,
                       path=path, schema_value=node["default"], schema_value_kind="default")
    for comb in ("allOf", "anyOf", "oneOf"):
        branches = node.get(comb)
        if branches:
            if comb == "allOf":
                merged: Dict[str, Any] = {}
                for s in branches:
                    if isinstance(s, dict):
                        merged.update(s)
                merged.update({k: v for k, v in node.items() if k != comb})
                return _walk(merged, key=key, required=required, root=root, ctx=ctx,
                             depth=depth, seen=seen, leaf_fn=leaf_fn, path=path)
            for branch in branches:                       # anyOf/oneOf: first synthesizable
                v = _walk(branch, key=key, required=required, root=root, ctx=ctx,
                          depth=depth + 1, seen=seen, leaf_fn=leaf_fn, path=path)
                if v is not None:
                    return v
            return None

    t = _first_type(node.get("type"))
    if t == "object" or (t is None and "properties" in node):
        out: Dict[str, Any] = {}
        props = node.get("properties") or {}
        # No `required` list AT ALL → treat every declared field as needed: a forged
        # run() takes its declared params, and an empty value trips the tool's own
        # `if not <arg>` guard (e.g. "password is required"). A flat tool schema and
        # a schema that simply omits `required` both land here. An EXPLICIT required
        # list (even empty) is respected — its omissions are genuinely optional.
        req = set(node["required"]) if "required" in node else set(props.keys())
        for name in sorted(props, key=lambda n: n not in req):   # required first
            out[name] = _walk(props[name], key=name, required=(name in req),
                              root=root, ctx=ctx, depth=depth + 1, seen=seen,
                              leaf_fn=leaf_fn, path=path + (name,))
        ap = node.get("additionalProperties")
        if isinstance(ap, dict):
            out["sample_key"] = _walk(ap, key="sample_key", required=False,
                                      root=root, ctx=ctx, depth=depth + 1, seen=seen,
                                      leaf_fn=leaf_fn, path=path + ("sample_key",))
        return out
    if t in ("array", "list"):
        items = node.get("items") or node.get("prefixItems") or {}
        if isinstance(items, list):                              # tuple schema
            return [_walk(s, key=key, required=True, root=root, ctx=ctx,
                          depth=depth + 1, seen=seen, leaf_fn=leaf_fn, path=path + ("[]",))
                    for s in items]
        n = max(int(node.get("minItems") or 1), 1)               # ≥1 so a path-list is exercised
        return [_walk(items, key=key, required=True, root=root, ctx=ctx,
                      depth=depth + 1, seen=seen, leaf_fn=leaf_fn, path=path + ("[]",))
                for _ in range(n)]
    if t in ("string", "integer", "int", "number", "float", "boolean"):
        kind, ext = "", ""
        if t == "string":
            is_path, kind_, ext_ = looks_like_path(key, node, tool_name=ctx.tool_name)
            if is_path:
                kind, ext = kind_, ext_
        return leaf_fn(node, key=key, required=required, kind=kind, ext=ext,
                       ctx=ctx, path=path)
    # unknown/no type — if it has properties treat as object (handled above); else
    # a bare leaf we can't type → None (the caller's required-field check / operator
    # _verify covers it). Route through leaf_fn (default reproduces the None).
    return leaf_fn(node, key=key, required=required, kind="", ext="",
                   ctx=ctx, path=path)


@dataclass
class SynthResult:
    params: Dict[str, Any]
    created_paths: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)


def synthesize_params(schema: Dict[str, Any], *, tool_name: str = "",
                      sandbox_dir: Optional[str] = None,
                      grounding_paths: Optional[List[str]] = None) -> SynthResult:
    """Synthesize a structurally-complete, constraint-valid instance from ``schema``
    and materialize a real fixture at every path leaf. Accepts a wrapped
    ``{type, properties, required}`` schema OR a flat ``{name: spec}`` map (both via
    ``normalize_parameters_schema``); recursion handles all deeper nesting natively.

    ``grounding_paths`` are real operator-referenced files (persisted on the Tool at
    forge time); any that exist on disk are copied into the sandbox for matching
    INPUT path leaves so a content-dependent tool is dry-run against real content.
    Missing/stale paths are silently ignored (synthetic fixture fallback)."""
    schema = schema if isinstance(schema, dict) else {}
    if ("properties" in schema or schema.get("type") == "object" or "$ref" in schema
            or any(k in schema for k in ("anyOf", "allOf", "oneOf"))):
        # Already a JSON-Schema object node — feed it straight to the walk so
        # additionalProperties / anyOf / $defs survive (normalizing would drop them).
        root = schema
    else:
        # A flat {name: spec} map → wrap it (normalize folds a wrapped form's
        # `required` into each spec, so we read it back per-field).
        from systemu.core.schema_utils import normalize_parameters_schema
        norm = normalize_parameters_schema(schema)
        props: Dict[str, Any] = {n: (s if isinstance(s, dict) else {})
                                 for n, s in (norm or {}).items()}
        req: List[str] = [n for n, s in (norm or {}).items()
                          if isinstance(s, dict) and s.get("required")]
        # A flat tool schema declares no `required` list; only set one if some field
        # explicitly carries `required`. When NONE do, omit it so the walk treats
        # every param as needed (non-empty) — a flat schema lists exactly the params
        # the tool's run() takes, all of which it expects.
        root: Dict[str, Any] = {"type": "object", "properties": props}
        if req:
            root["required"] = req

    sb = Path(sandbox_dir) if sandbox_dir else (
        Path(tempfile.gettempdir()) / f"dry_run_{uuid.uuid4().hex[:8]}")
    try:
        sb.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[FixtureSynth] could not create sandbox %s", sb, exc_info=True)
    ctx = _Ctx(tool_name=tool_name, sandbox=sb,
               grounding=[g for g in (grounding_paths or [])
                          if isinstance(g, str) and os.path.isfile(g)])
    params = _walk(root, key="", required=False, root=root, ctx=ctx, depth=0, seen=frozenset())
    return SynthResult(params=params if isinstance(params, dict) else {},
                       created_paths=ctx.created, unresolved=ctx.unresolved)
