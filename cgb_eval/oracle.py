"""External ground-truth checks.

The runtime's own goal-verifier NEVER grades itself (spec §5.2 — it is known to
over-accept; using it would be circular).  These primitives inspect the durable
workspace artifacts a real, independent grader would.
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from pathlib import Path
from typing import Any, Callable

from cgb_eval.task_spec import OracleResult

Check = Callable[[Path], OracleResult]


def file_exists(rel: str) -> Check:
    def check(ws: Path) -> OracleResult:
        ok = (ws / rel).is_file()
        return OracleResult(ok, f"{rel} {'exists' if ok else 'MISSING'}")
    return check


def file_contains(rel: str, needle: str) -> Check:
    def check(ws: Path) -> OracleResult:
        p = ws / rel
        if not p.is_file():
            return OracleResult(False, f"{rel} MISSING")
        ok = needle.lower() in p.read_text(encoding="utf-8", errors="replace").lower()
        return OracleResult(ok, f"{rel} {'contains' if ok else 'LACKS'} {needle!r}")
    return check


def file_line_count_between(rel: str, lo: int, hi: int) -> Check:
    def check(ws: Path) -> OracleResult:
        p = ws / rel
        if not p.is_file():
            return OracleResult(False, f"{rel} MISSING")
        n = len([ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()])
        return OracleResult(lo <= n <= hi, f"{rel} has {n} lines (want {lo}-{hi})")
    return check


def json_field_equals(rel: str, key: str, expected: Any) -> Check:
    def check(ws: Path) -> OracleResult:
        p = ws / rel
        if not p.is_file():
            return OracleResult(False, f"{rel} MISSING")
        try:
            val = json.loads(p.read_text(encoding="utf-8")).get(key)
        except (json.JSONDecodeError, AttributeError) as e:
            return OracleResult(False, f"{rel} unparseable: {e}")
        return OracleResult(val == expected, f"{rel}[{key!r}]={val!r} (want {expected!r})")
    return check


def hash_hex_in_file(input_rel: str, output_rel: str, algo: str = "sha256") -> Check:
    """REAL-gap oracle: ``output_rel`` must contain the ``algo`` hex digest of
    ``input_rel``'s bytes.

    An LLM cannot compute a cryptographic hash by hand and no built-in tool
    provides one, so passing requires the agent to have forged (pull-provisioned)
    a hashing tool. The check is lenient about surrounding prose and case — only
    the digest substring must be present.
    """
    def check(ws: Path) -> OracleResult:
        out = ws / output_rel
        src = ws / input_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        if not src.is_file():
            return OracleResult(False, f"{input_rel} MISSING (cannot compute expected)")
        # Hash the TEXT the agent can observe through a text read tool (universal
        # newlines normalize CRLF->LF), not the raw on-disk bytes -- otherwise a
        # CRLF file (Windows text-mode writes) makes a correct LF answer fail.
        observed = src.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        expected = hashlib.new(algo, observed).hexdigest()
        got = out.read_text(encoding="utf-8", errors="replace")
        ok = expected.lower() in got.lower()
        return OracleResult(
            ok, f"{algo}({input_rel}) {'in' if ok else 'NOT in'} {output_rel} "
                f"(expected {expected[:12]}...)")
    return check


def crc32_json_field(input_rel: str, output_rel: str, key: str = "crc32") -> Check:
    """REAL-gap oracle: ``output_rel`` JSON ``key`` must equal the CRC-32 of
    ``input_rel``'s bytes (unsigned). Like hashing, this is not hand-computable,
    so it forces a forged checksum tool."""
    def check(ws: Path) -> OracleResult:
        out = ws / output_rel
        src = ws / input_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        if not src.is_file():
            return OracleResult(False, f"{input_rel} MISSING (cannot compute expected)")
        # Normalize newlines to the LF text the agent reads (see hash_hex_in_file).
        observed = src.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        expected = zlib.crc32(observed) & 0xFFFFFFFF
        try:
            val = json.loads(out.read_text(encoding="utf-8")).get(key)
        except (json.JSONDecodeError, AttributeError) as e:
            return OracleResult(False, f"{output_rel} unparseable: {e}")
        return OracleResult(val == expected, f"{output_rel}[{key!r}]={val!r} (want {expected!r})")
    return check


def zlib_roundtrips_to_input(input_rel: str, output_rel: str) -> Check:
    """REAL-gap oracle (synthesis, NON-hash): ``output_rel`` must contain a hex string
    that ZLIB-DECOMPRESSES back to ``input_rel``'s exact bytes.

    This verifies the agent produced REAL zlib compression of the input -- a fabricated
    blob will not decompress to it -- while accepting ANY zlib level, so a correct forged
    compressor cannot spuriously fail on a level mismatch (the analogue of the CRLF fix
    for the hash tasks). Compression output is high-entropy and not producible by
    reasoning, but unlike a cryptographic hash models rarely (over)confidently fabricate
    a compressed blob, so this probes a different point in the
    'attemptable-vs-recognized' space. Lenient about surrounding prose: any hex run that
    round-trips counts. The input is written LF-on-disk so byte- and text-reads agree."""
    def check(ws: Path) -> OracleResult:
        out = ws / output_rel
        src = ws / input_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        if not src.is_file():
            return OracleResult(False, f"{input_rel} MISSING (cannot verify)")
        expect = src.read_bytes()
        text = out.read_text(encoding="utf-8", errors="replace")
        for run in re.findall(r"[0-9a-fA-F]{8,}", text):
            if len(run) % 2:
                run = run[:-1]
            try:
                if zlib.decompress(bytes.fromhex(run)) == expect:
                    return OracleResult(True, f"{output_rel} zlib-decompresses to {input_rel}")
            except Exception:
                continue
        return OracleResult(
            False, f"{output_rel} has no hex that zlib-decompresses to {input_rel}")
    return check


def ordered_inputs_reproduced(output_rel: str, n: int, input_prefix: str = "part_") -> Check:
    """REAL-gap COMPUTE oracle: ``output_rel`` must have exactly ``n`` non-empty
    lines, where line K (0-based) contains the contents of input
    ``{input_prefix}KK.txt``, in order.

    The inputs hold UNGUESSABLE tokens (not inferable from the goal), so the agent
    cannot fabricate the output or batch it in one write -- it MUST read each input
    (one read per loop iteration). A run short on iteration budget therefore can't
    reproduce them all and FAILS. The oracle reads the actual inputs, so the answer
    cannot be guessed, and there is no request-escape: only the finished, correctly
    ordered artifact passes."""
    def check(ws: Path) -> OracleResult:
        out = ws / output_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        lines = [ln.strip() for ln in
                 out.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
        if len(lines) != n:
            return OracleResult(False, f"{output_rel} has {len(lines)} lines (want {n})")
        for k in range(n):
            part = ws / f"{input_prefix}{k:02d}.txt"
            if not part.is_file():
                return OracleResult(False, f"input {part.name} missing")
            tok = part.read_text(encoding="utf-8", errors="replace").strip()
            if tok.lower() not in lines[k].lower():
                return OracleResult(
                    False, f"{output_rel} line {k} = {lines[k]!r} (missing input {tok!r})")
        return OracleResult(True, f"{output_rel} reproduces {n} inputs in order")
    return check


def mcp_codes_reproduced(output_rel: str, keys: tuple) -> Check:
    """REAL-gap MCP oracle: ``output_rel`` must contain, for every key, the
    UNGUESSABLE code that only the hermetic 'lookup' MCP server can produce
    (``_lookup_logic.resolve_code``).

    The agent has NO local tool for this and the codes are not derivable from the
    goal, so passing requires the agent to have ATTACHED the server
    (``REQUEST_HARNESS kind=mcp``) and CALLED ``resolve_code`` for each key. The
    oracle recomputes the expected codes from the same pure logic the server runs
    (imported here, never via the runtime), so the answer cannot be guessed and
    there is no request-escape -- only a correctly attached-and-used run passes.
    Lenient about surrounding format (only the code substring must be present),
    mirroring ``hash_hex_in_file``."""
    from cgb_eval.mcp_servers._lookup_logic import resolve_code

    def check(ws: Path) -> OracleResult:
        out = ws / output_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        text = out.read_text(encoding="utf-8", errors="replace").lower()
        missing = [f"{k}->{resolve_code(k)}" for k in keys
                   if resolve_code(k).lower() not in text]
        ok = not missing
        return OracleResult(
            ok, f"{output_rel} {'has all' if ok else 'is MISSING'} {len(keys)} lookup "
                f"codes" + ("" if ok else f" (missing {missing[:3]})"))
    return check


def all_of(*checks: Check) -> Check:
    def check(ws: Path) -> OracleResult:
        details = []
        passed = True
        for c in checks:
            r = c(ws)
            details.append(r.details)
            passed = passed and r.passed
        return OracleResult(passed, "; ".join(details))
    return check
