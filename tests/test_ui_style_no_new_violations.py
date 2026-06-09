"""Fails if any NEW raw-hex / inline-f-string-style violation is introduced in
systemu/interface (beyond the checked-in count baseline). Migrating a page =
shrinking tools/ui_style_baseline.txt; adding debt = a red test."""
from tools.lint_ui_styles import _scan_repo, _counts, _load_baseline, new_violations


def test_no_new_ui_style_violations():
    current = _counts(_scan_repo())
    baseline = _load_baseline()
    new = new_violations(current, baseline)
    assert not new, "New UI-style violations (compose primitives / use tokens):\n" + \
        "\n".join(f"  {k}: {current[k]} (baselined {baseline.get(k, 0)})" for k in sorted(new))
