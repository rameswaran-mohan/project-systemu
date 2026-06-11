"""W4.3 — Inter + JetBrains Mono are vendored locally, not loaded from a CDN.

Pins: the global CSS declares @font-face rules pointing at /assets/fonts, the
woff2 files (+ OFL license) actually ship in the package, and the Google Fonts
<link> is gone from the dashboard.
"""
from __future__ import annotations

import pathlib

import systemu.interface


_FONTS_DIR = pathlib.Path(systemu.interface.__file__).parent / "assets" / "fonts"
_EXPECTED = [
    "inter-400.woff2", "inter-500.woff2", "inter-600.woff2",
    "inter-700.woff2", "inter-800.woff2",
    "jetbrains-mono-400.woff2", "jetbrains-mono-600.woff2",
]


class TestVendoredFontFiles:
    def test_all_woff2_files_present(self):
        missing = [f for f in _EXPECTED if not (_FONTS_DIR / f).is_file()]
        assert not missing, f"missing vendored fonts: {missing}"

    def test_files_are_non_trivial(self):
        # A truncated/empty download would silently break rendering.
        for f in _EXPECTED:
            assert (_FONTS_DIR / f).stat().st_size > 5_000, f"{f} looks truncated"

    def test_ofl_license_shipped(self):
        ofl = _FONTS_DIR / "OFL.txt"
        assert ofl.is_file(), "SIL OFL requires the license to ship with the fonts"
        body = ofl.read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in body
        assert "Inter" in body and "JetBrains Mono" in body


class TestGlobalCssFontFaces:
    def test_font_faces_reference_local_assets(self):
        from systemu.interface.design.tokens import build_global_css
        css = build_global_css()
        assert "@font-face" in css
        assert "/assets/fonts/inter-400.woff2" in css
        assert "/assets/fonts/jetbrains-mono-600.woff2" in css
        assert "format('woff2')" in css
        # No CDN reference leaks into the stylesheet.
        assert "googleapis" not in css and "gstatic" not in css

    def test_all_seven_faces_emitted(self):
        from systemu.interface.design.tokens import build_global_css, _FONT_FILES
        css = build_global_css()
        assert css.count("@font-face") == len(_FONT_FILES) == 7


class TestNoCdnLink:
    def test_dashboard_no_google_fonts_link(self):
        import inspect
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert "fonts.googleapis.com" not in src, \
            "the Google Fonts CDN <link> must be gone — fonts are vendored now"
        assert "fonts.gstatic.com" not in src


class TestPackagingIncludesFonts:
    def test_pyproject_ships_woff2(self):
        # The wheel must carry the fonts or pip installs fall back to system fonts.
        import pathlib as _pl
        root = _pl.Path(systemu.interface.__file__).parents[2]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert "interface/assets/fonts/*.woff2" in pyproject
