"""A surface that consumes the one mint correctly -- the badge lint must report
nothing here (the self-test's negative half). Never imported.

Docstrings may say "verified" and "claimed" freely; a field NAME such as
``{"verified": ...}`` is not a verdict.
"""


def render(status, execution_id, ui):
    from systemu.runtime import verification_badge as vb
    badge = vb.run_badge(status, execution_id)
    v = vb.view(badge)
    ui.label(v.label).classes(v.pill_class)
    return {"verified": v.state == vb.VERIFIED, "status": "failed"}
