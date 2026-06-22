"""v0.9.3 tool_hygiene peer module tests."""
import os
import pytest
from pathlib import Path


class TestPathSecurity:
    def test_safe_resolve_basic(self, tmp_path):
        from systemu.runtime.tool_hygiene.path_security import safe_resolve
        resolved = safe_resolve("file.txt", root=tmp_path)
        assert str(tmp_path) in str(resolved)
        assert resolved.name == "file.txt"

    def test_safe_resolve_rejects_traversal(self, tmp_path):
        from systemu.runtime.tool_hygiene.path_security import safe_resolve, PathSecurityError
        with pytest.raises(PathSecurityError):
            safe_resolve("../../etc/passwd", root=tmp_path)

    def test_safe_resolve_rejects_absolute_outside_root(self, tmp_path):
        from systemu.runtime.tool_hygiene.path_security import safe_resolve, PathSecurityError
        with pytest.raises(PathSecurityError):
            safe_resolve("/etc/passwd", root=tmp_path)

    def test_safe_resolve_allows_subdir(self, tmp_path):
        from systemu.runtime.tool_hygiene.path_security import safe_resolve
        sub = tmp_path / "sub"
        sub.mkdir()
        resolved = safe_resolve("sub/file.txt", root=tmp_path)
        assert "sub" in resolved.parts
        assert resolved.name == "file.txt"


class TestUrlSafety:
    def test_https_allowed(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("https://example.com") is True

    def test_http_allowed(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("http://example.com") is True

    def test_file_scheme_rejected(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("file:///etc/passwd") is False

    def test_localhost_rejected(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("http://localhost:8080") is False
        assert is_url_safe("http://127.0.0.1") is False

    def test_private_ip_rejected(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("http://10.0.0.1") is False
        assert is_url_safe("http://192.168.1.1") is False
        assert is_url_safe("http://172.16.0.1") is False

    def test_empty_string_rejected(self):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe("") is False


class TestOutputLimits:
    def test_cap_output_under_limit(self):
        from systemu.runtime.tool_hygiene.output_limits import cap_output
        result = cap_output("hello", max_chars=100)
        assert result == "hello"

    def test_cap_output_truncates(self):
        from systemu.runtime.tool_hygiene.output_limits import cap_output
        result = cap_output("a" * 200, max_chars=50)
        assert len(result) <= 200  # cap + marker
        assert "truncated" in result.lower()

    def test_cap_output_zero_disables(self):
        from systemu.runtime.tool_hygiene.output_limits import cap_output
        result = cap_output("a" * 200, max_chars=0)
        assert result == "a" * 200

    def test_is_likely_binary_detects_null_bytes(self):
        from systemu.runtime.tool_hygiene.output_limits import is_likely_binary
        assert is_likely_binary(b"hello\x00world") is True

    def test_is_likely_binary_false_for_text(self):
        from systemu.runtime.tool_hygiene.output_limits import is_likely_binary
        assert is_likely_binary(b"hello world\nfoo bar") is False

    def test_is_likely_binary_empty_is_false(self):
        from systemu.runtime.tool_hygiene.output_limits import is_likely_binary
        assert is_likely_binary(b"") is False


class TestAnsiStrip:
    def test_strip_basic_color(self):
        from systemu.runtime.tool_hygiene.ansi_strip import strip_ansi
        s = "\x1b[31mhello\x1b[0m"
        assert strip_ansi(s) == "hello"

    def test_strip_cursor_moves(self):
        from systemu.runtime.tool_hygiene.ansi_strip import strip_ansi
        s = "before\x1b[2J\x1b[H after"
        assert strip_ansi(s) == "before after"

    def test_strip_no_ansi_unchanged(self):
        from systemu.runtime.tool_hygiene.ansi_strip import strip_ansi
        assert strip_ansi("plain text") == "plain text"

    def test_strip_empty(self):
        from systemu.runtime.tool_hygiene.ansi_strip import strip_ansi
        assert strip_ansi("") == ""
