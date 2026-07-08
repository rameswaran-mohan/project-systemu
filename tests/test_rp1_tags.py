from systemu.messaging import decision_bridge as db


def test_tag_deterministic_6_base32_lower():
    t = db.decision_tag("dec_abc123")
    assert t == db.decision_tag("dec_abc123")
    assert len(t) == 6 and t == t.lower()
    assert all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in t)   # rfc4648 base32 lower


def test_different_ids_usually_differ():
    assert db.decision_tag("dec_a") != db.decision_tag("dec_b")


def test_disambiguate_extends_to_8_on_collision():
    # if the 6-char tag is already taken (passed in the open set), extend to 8
    t6 = db.decision_tag("dec_x")
    out = db.disambiguate_tag("dec_x", open_tags={t6})
    assert len(out) == 8 and out.startswith(t6)
    # no collision -> plain 6
    assert db.disambiguate_tag("dec_x", open_tags=set()) == t6


def test_callback_token_roundtrip_and_len():
    tok = db.callback_token("k3f7qa", "a1")
    assert tok == "d|k3f7qa|a1"
    assert len(tok.encode("utf-8")) <= 64
    assert db.parse_callback(tok) == ("k3f7qa", "a1")


def test_parse_callback_rejects_garbage_never_raises():
    for bad in ["", "x|y", "d|", "nope", "d|tag|a1|extra", "e|tag|a1", None]:
        assert db.parse_callback(bad) is None


def test_callback_data_stays_under_64_for_8char_tag():
    assert len(db.callback_token("k3f7qaZZ", "a4").encode()) <= 64
