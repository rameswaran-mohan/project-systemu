"""Phase 4 Task 2 — the persistent right-rail composer.

The pure section-order helper is UI-free (unit-testable); the NiceGUI composer
is a thin shell over the two proven panes (inbox_rail + live_runs_pane).
"""


def test_section_titles_order():
    from systemu.interface.components.right_rail import right_rail_section_titles
    # Needs-you (Inbox glance) sits ABOVE Live (runs), per spec §4.2.
    assert right_rail_section_titles() == ["Needs you", "Live"]


def test_render_right_rail_is_callable():
    from systemu.interface.components.right_rail import render_right_rail
    assert callable(render_right_rail)
