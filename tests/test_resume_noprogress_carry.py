# tests/test_resume_noprogress_carry.py
from systemu.runtime.shadow_runtime import (
    _encode_no_progress_note, _decode_no_progress_note,
)


def test_encode_decode_roundtrip():
    note = _encode_no_progress_note(7)
    assert note.startswith("__NO_PROGRESS_CARRY__::")
    assert _decode_no_progress_note([note]) == 7


def test_decode_missing_returns_zero():
    assert _decode_no_progress_note(["__STUCK_ROUNDS__::{}"]) == 0
    assert _decode_no_progress_note([]) == 0


def test_decode_malformed_returns_zero():
    assert _decode_no_progress_note(["__NO_PROGRESS_CARRY__::notanint"]) == 0
