import textwrap
from tools.lint_ui_styles import find_violations, new_violations


def test_flags_raw_hex_literal():
    src = 'x = "#ef4444"\n'
    v = find_violations(src, "f.py")
    assert any("raw hex" in m.message for m in v)


def test_flags_inline_fstring_style():
    src = 'el.style(f"color: {c}")\n'
    v = find_violations(src, "f.py")
    assert any("inline .style(f" in m.message for m in v)


def test_clean_primitive_code_has_no_violations():
    src = textwrap.dedent('''
        from systemu.interface.design import button
        button("Go", variant="primary")
    ''')
    assert find_violations(src, "f.py") == []


def test_three_digit_hex_in_css_var_context_is_ok():
    src = 'x = "#fff"\n'
    assert find_violations(src, "f.py") == []


def test_new_violations_detects_count_increase():
    assert new_violations({"a:hex": 3}, {"a:hex": 2}) == {"a:hex": 3}


def test_new_violations_ignores_count_decrease():
    assert new_violations({"a:hex": 1}, {"a:hex": 3}) == {}


def test_new_violations_detects_brand_new_key():
    assert "b:hex" in new_violations({"b:hex": 1}, {})


def test_new_violations_unchanged_count_is_clean():
    assert new_violations({"a:hex": 2}, {"a:hex": 2}) == {}
