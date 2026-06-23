"""TOOL-family: the goal needs a tool that is GENUINELY absent; pull forges it.

Design note (REAL gaps, not synthetic). The agent is given working file read +
write tools, exactly as in normal systemu use. The withheld capability is a
*computation systemu has no built-in for and an LLM cannot do by hand* --- a
cryptographic hash / checksum. So the push baseline genuinely cannot satisfy the
oracle (it can read and write, but cannot produce a correct SHA-256), while the
pull condition can ``REQUEST_HARNESS`` a TOOL and have the Governor forge a
hashing tool (trivially, from the standard library). This avoids the trap of
manufacturing a gap by removing a capability systemu actually ships (e.g. file
writing): the gap here is real, so RQ2 efficacy is meaningful rather than
tautological. The oracle COMPUTES the expected digest from the input, so it
cannot be guessed.
"""
from __future__ import annotations

from pathlib import Path

from cgb_eval.oracle import crc32_json_field, hash_hex_in_file, zlib_roundtrips_to_input
from cgb_eval.task_spec import CGBTask


def _write_lf(ws: Path, name: str, text: str) -> None:
    """Write an input file with LF line endings ON DISK (not the platform default).

    Load-bearing for the hashing tasks: on Windows ``Path.write_text`` translates
    ``\\n``->``\\r\\n``, but the oracle hashes the LF text the agent reads. A
    *correctly* forged tool that hashes the file's raw BYTES---the natural
    ``hashlib.new(algo, open(path,'rb').read())``---would then see CRLF and produce
    a different digest, a spurious failure of a correct tool that understates pull
    efficacy. Writing raw LF bytes makes ``bytes == text``, so byte- and text-reading
    tools BOTH agree with the oracle and the hash task is unambiguous about its input.
    """
    (ws / name).write_bytes(text.encode("utf-8"))


def _setup_quotes(ws: Path) -> None:
    _write_lf(ws, "quotes.txt",
              "\n".join(f"Quote {i}: wisdom line number {i}." for i in range(1, 11)))


def _setup_csv(ws: Path) -> None:
    rows = ["name,team", "ana,red", "bo,blue", "ana,red", "cy,red", "bo,blue"]
    _write_lf(ws, "people.csv", "\n".join(rows))


def _setup_logs(ws: Path) -> None:
    lines = [f"2026-06-01 INFO ok {i}" for i in range(20)]
    lines += [f"2026-06-01 ERROR boom {i}" for i in range(3)]
    _write_lf(ws, "app.log", "\n".join(lines))


def _setup_words(ws: Path) -> None:
    _write_lf(ws, "words.txt", "delta alpha charlie bravo echo alpha delta")


def _setup_blob(ws: Path) -> None:
    # A few KB of mixed text -> a non-trivial compressed blob the agent cannot
    # produce by reasoning (high-entropy output), but a DIFFERENT operation from
    # hashing -- models are far less likely to overconfidently fabricate it.
    _write_lf(ws, "data.txt",
              "\n".join(f"record {i}: status=ok value={(i * 7919) % 1000} "
                        f"tag=alpha-{i % 5} note=the quick brown fox {i}"
                        for i in range(80)))


# Every task PROVIDES working file read+write and WITHHOLDS a computation systemu has
# no built-in for and an LLM cannot do by hand (a hash/checksum, or compression).
_PROVIDED = ("file_read", "file_write")
_WITHHELD = "a hashing/checksum tool (e.g. sha256 / md5 / crc32)"

TOOL_TASKS = [
    CGBTask(
        task_id="tool-01-sha256",
        family="TOOL",
        goal=("Read the file {workspace}/quotes.txt, compute the SHA-256 hash of "
              "its contents, and write the lowercase hex digest to "
              "{workspace}/hash.txt."),
        success_criteria="hash.txt contains the correct SHA-256 hex digest of quotes.txt",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=_setup_quotes,
        oracle=hash_hex_in_file("quotes.txt", "hash.txt", "sha256"),
    ),
    CGBTask(
        task_id="tool-02-md5-csv",
        family="TOOL",
        goal=("Read {workspace}/people.csv, compute the MD5 hash of its "
              "contents, and write the lowercase hex digest to {workspace}/md5.txt."),
        success_criteria="md5.txt contains the correct MD5 hex digest of people.csv",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=_setup_csv,
        oracle=hash_hex_in_file("people.csv", "md5.txt", "md5"),
    ),
    CGBTask(
        task_id="tool-03-crc32-log",
        family="TOOL",
        goal=("Read {workspace}/app.log, compute the CRC-32 checksum of its "
              "contents, and write it as JSON to {workspace}/report.json in the form "
              '{{"crc32": <unsigned integer>}}.'),
        success_criteria="report.json crc32 equals the CRC-32 of app.log",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=_setup_logs,
        oracle=crc32_json_field("app.log", "report.json", "crc32"),
    ),
    CGBTask(
        task_id="tool-04-sha1-words",
        family="TOOL",
        goal=("Read {workspace}/words.txt, compute the SHA-1 hash of its "
              "contents, and write the lowercase hex digest to {workspace}/sha1.txt."),
        success_criteria="sha1.txt contains the correct SHA-1 hex digest of words.txt",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=_setup_words,
        oracle=hash_hex_in_file("words.txt", "sha1.txt", "sha1"),
    ),
    CGBTask(
        # A deliberately DIFFERENT synthesis operation from the four hash tasks, so the
        # frontier-model evidence is not all about hashing: compression output is
        # high-entropy and unfabricable, but models rarely (over)confidently fake a
        # compressed blob the way they fake a hash.
        task_id="tool-05-zlib",
        family="TOOL",
        goal=("Read {workspace}/data.txt, compress its bytes with zlib, and write the "
              "lowercase hex of the compressed bytes to {workspace}/compressed.txt."),
        success_criteria="compressed.txt hex zlib-decompresses to data.txt",
        provided_tools=_PROVIDED,
        withheld="a compression tool (zlib)",
        setup=_setup_blob,
        oracle=zlib_roundtrips_to_input("data.txt", "compressed.txt"),
    ),
]
