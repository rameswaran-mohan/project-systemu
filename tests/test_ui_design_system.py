from systemu.interface.design import tokens as T


def test_palette_is_refined_midnight_indigo():
    assert T.TOKENS["color"]["bg"] == "#0e1016"
    assert T.TOKENS["color"]["accent"] == "#7376f2"
    assert T.TOKENS["color"]["danger"] == "#f0676b"


def test_scales_present():
    assert T.TOKENS["space"] == [4, 8, 12, 16, 24, 32]
    assert T.TOKENS["radius"] == [7, 10, 14, 999]
    assert T.TOKENS["type"] == [11, 12.5, 14, 17, 22, 30]


def test_build_global_css_emits_root_vars_and_base_classes():
    css = T.build_global_css()
    assert "--color-bg: #0e1016;" in css
    assert "--color-accent: #7376f2;" in css
    assert "--space-3: 16px;" in css        # 4,8,12,16 -> index 3 = 16
    assert "--radius-pill: 999px;" in css
    for cls in (".s-card", ".s-pill", ".s-btn", ".s-input", ".s-table", ".s-tabs"):
        assert cls in css
    assert "#6366f1" not in css


from systemu.interface.design import primitives as P


def test_pill_classes_pure_and_tokenized():
    assert P._pill_classes("approved") == "s-pill s-pill--success"
    assert P._pill_classes("retired") == "s-pill s-pill--danger"
    assert P._pill_classes("totally-unknown") == "s-pill s-pill--muted"


def test_button_classes_variants():
    assert P._btn_classes("primary") == "s-btn s-btn--primary"
    assert P._btn_classes("ghost") == "s-btn s-btn--ghost"
    assert P._btn_classes("danger") == "s-btn s-btn--danger"


def test_button_classes_rejects_unknown_variant():
    import pytest
    with pytest.raises(ValueError):
        P._btn_classes("rainbow")


def test_status_pill_html_has_no_raw_hex():
    html = P.status_pill_html("approved")
    assert "s-pill" in html and "#" not in html


from systemu.interface.design import icons as I


def test_icon_maps_concepts_to_material_symbols():
    assert I.icon("approve") == "check_circle"
    assert I.icon("reject") == "cancel"
    assert I.icon("shadow") == "smart_toy"


def test_icon_unknown_concept_is_safe_default():
    assert I.icon("nonexistent-concept") == "help"


def test_no_concept_maps_to_emoji():
    for glyph in I.ICONS.values():
        assert glyph.isascii() and glyph.replace("_", "").isalpha()


def test_dashboard_state_reexports_are_token_backed():
    from systemu.interface import dashboard_state as ds
    from systemu.interface.design.tokens import TOKENS
    assert ds.THEME["danger"] == TOKENS["color"]["danger"] == "#f0676b"
    assert ds.THEME["primary"] == TOKENS["color"]["accent"] == "#7376f2"
    assert "--color-bg: #0e1016;" in ds.GLOBAL_CSS
    assert "#" not in ds.status_badge_html("approved")
