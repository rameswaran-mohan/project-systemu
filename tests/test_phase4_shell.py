"""Phase 4 Task 3/4 — shell integration: right-rail CSS + import safety."""


def test_s_rail_class_in_global_css():
    from systemu.interface.design.tokens import build_global_css
    css = build_global_css()
    assert ".s-rail" in css, "right-rail class missing from GLOBAL_CSS"
    # The rail must collapse on narrow viewports (mirrors the sidebar rule).
    assert "max-width" in css, "no responsive media query in GLOBAL_CSS"


def test_dashboard_exposes_right_rail_helper():
    import systemu.interface.dashboard as d
    assert hasattr(d, "_render_persistent_right_rail")
    assert callable(d._render_persistent_right_rail)


def test_plus_new_menu_items():
    from systemu.interface.dashboard import plus_new_menu_items
    # The global +New action surfaces exactly the two creation entry points.
    assert plus_new_menu_items() == ["Record session", "Submit task"]
