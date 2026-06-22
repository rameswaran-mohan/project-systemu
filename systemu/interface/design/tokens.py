"""Refined Midnight Indigo design tokens — the ONLY source of colour/space/radius/type."""
from __future__ import annotations

TOKENS = {
    "color": {
        "bg": "#0e1016", "surface": "#171a24", "surface2": "#1e2230",
        "border": "#262a38", "text": "#e9ebf5", "muted": "#888da3",
        "accent": "#7376f2", "accent2": "#9a9df8", "success": "#34d27b",
        "warn": "#f1b24a", "danger": "#f0676b", "info": "#54c7f0",
    },
    "status": {
        "pending_approval": "warn", "proposed": "warn", "unassigned": "warn",
        "approved": "success", "deployed": "success", "enabled": "success",
        "forged": "info", "linked": "accent", "awakened": "accent",
        "assigned": "accent", "dormant": "muted", "retired": "danger",
        "partial": "warn",
        # W5.1: a pending operator ask (stuck-run question / credential
        # request) is a needs-attention state — tint it like one.
        "question": "warn",
    },
    "space": [4, 8, 12, 16, 24, 32],
    "radius": [7, 10, 14, 999],
    "type": [11, 12.5, 14, 17, 22, 30],
}


def _fmt(v):
    return f"{int(v)}" if float(v).is_integer() else f"{v}"


# W4.3: Inter + JetBrains Mono are vendored locally (latin-subset woff2 under
# systemu/interface/assets/fonts, served at /assets/fonts) instead of loaded
# from the Google Fonts CDN — offline-safe, no third-party request, no FOUT.
# SIL OFL 1.1 (see assets/fonts/OFL.txt). The route is registered in
# dashboard.run_dashboard via app.add_static_files.
_FONT_FILES = [
    ("Inter", 400, "inter-400.woff2"),
    ("Inter", 500, "inter-500.woff2"),
    ("Inter", 600, "inter-600.woff2"),
    ("Inter", 700, "inter-700.woff2"),
    ("Inter", 800, "inter-800.woff2"),
    ("JetBrains Mono", 400, "jetbrains-mono-400.woff2"),
    ("JetBrains Mono", 600, "jetbrains-mono-600.woff2"),
]


def _font_faces() -> str:
    return "\n".join(
        f"@font-face {{ font-family: '{fam}'; font-style: normal; "
        f"font-weight: {w}; font-display: swap; "
        f"src: url('/assets/fonts/{fn}') format('woff2'); }}"
        for fam, w, fn in _FONT_FILES
    )


def build_global_css() -> str:
    color_vars = "\n".join(f"    --color-{n}: {h};" for n, h in TOKENS["color"].items())
    space_vars = "\n".join(f"    --space-{i}: {v}px;" for i, v in enumerate(TOKENS["space"]))
    radius_names = ["sm", "md", "lg", "pill"]
    radius_vars = "\n".join(f"    --radius-{radius_names[i]}: {v}px;" for i, v in enumerate(TOKENS["radius"]))
    type_names = ["xs", "sm", "md", "lg", "xl", "xxl"]
    type_vars = "\n".join(f"    --type-{type_names[i]}: {_fmt(v)}px;" for i, v in enumerate(TOKENS["type"]))
    pill_tint = "\n".join(
        f".s-pill--{name} {{ background: color-mix(in srgb, var(--color-{name}) 20%, transparent);"
        f" color: var(--color-{name});"
        f" border: 1px solid color-mix(in srgb, var(--color-{name}) 40%, transparent); }}"
        for name in TOKENS["color"]
    )
    return f"""{_font_faces()}

:root {{
{color_vars}
{space_vars}
{radius_vars}
{type_vars}
}}

body, html {{ background: radial-gradient(1200px 600px at 78% -8%, #191d33 0%, var(--color-bg) 55%) !important; color: var(--color-text) !important; font-family: 'Inter','Segoe UI',system-ui,-apple-system,sans-serif; margin: 0; }}
.nicegui-content {{ background: transparent !important; min-height: 100vh; }}

/* W4.4 a11y: a visible keyboard-focus ring. The Quasar flatten layer strips
   default outlines, leaving keyboard users with no focus indicator. :focus-visible
   shows it for keyboard/AT navigation only (not on mouse click). */
*:focus-visible {{ outline: 2px solid var(--color-accent2); outline-offset: 2px; border-radius: var(--radius-sm); }}
.q-btn:focus-visible, a:focus-visible, .s-btn:focus-visible, .q-tab:focus-visible {{ outline: 2px solid var(--color-accent2); outline-offset: 2px; }}

.s-card {{ background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-3); transition: border-color .2s, box-shadow .2s; }}
.s-card:hover {{ border-color: var(--color-accent); box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-accent) 30%, transparent); }}

.s-pill {{ display: inline-block; padding: 2px 9px; border-radius: var(--radius-pill); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; line-height: 1.7; }}
{pill_tint}

.s-btn {{ border-radius: var(--radius-sm); padding: 6px 14px; font-size: var(--type-md); font-weight: 600; border: 1px solid transparent; cursor: pointer; }}
.s-btn--primary {{ background: var(--color-accent); color: #fff; }}
.s-btn--ghost {{ background: transparent; color: var(--color-text); border-color: var(--color-border); }}
.s-btn--danger {{ background: var(--color-danger); color: #fff; }}
.s-btn--success {{ background: var(--color-success); color: #fff; }}
.s-btn--warn {{ background: var(--color-warn); color: #fff; }}

.s-input {{ background: var(--color-surface2); color: var(--color-text); border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 6px 10px; font-size: var(--type-md); }}

.s-table {{ width: 100%; border-collapse: collapse; }}
.s-table th, .s-table td {{ padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--color-border); text-align: left; }}
.s-table th {{ color: var(--color-muted); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }}

.s-tabs {{ border-bottom: 1px solid var(--color-border); }}

/* ── token utility classes (page migration) ── */
.s-muted {{ color: var(--color-muted); }}
.s-sep {{ background: var(--color-border); margin: var(--space-2) 0; }}
.s-text-warn {{ color: var(--color-warn); }}
.s-text-danger {{ color: var(--color-danger); }}
.s-page-title {{ font-size: var(--type-xl); font-weight: 800; color: var(--color-text); }}
.s-dialog-title {{ font-size: var(--type-lg); font-weight: 800; color: var(--color-text); }}
.s-section-head {{ font-size: var(--type-sm); font-weight: 700; color: var(--color-muted); margin-bottom: var(--space-0); }}
.s-cell {{ color: var(--color-text); font-size: var(--type-md); }}
.s-cell--bold {{ font-weight: 600; }}
.s-row-box {{ background: var(--color-surface2); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); gap: var(--space-2); }}
.s-warn-badge {{ color: var(--color-warn); font-size: 1.1em; margin-left: var(--space-0); }}
.s-italic {{ font-style: italic; }}
.s-step-num {{ color: var(--color-accent); min-width: 24px; }}
.s-trace-icon {{ min-width: 16px; }}
.s-search {{ width: 360px; }}
.s-input-full {{ width: 100%; }}
.s-dialog {{ min-width: 680px; max-width: 800px; max-height: 80vh; overflow-y: auto; }}
.s-dialog-sm {{ min-width: 420px; }}

/* ── inline banners (persistent advisories — e.g. Bypass danger banner) ── */
.s-banner {{ display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--type-sm); font-weight: 600; }}
.s-banner--danger {{ background: color-mix(in srgb, var(--color-danger) 18%, transparent); color: var(--color-danger); border: 1px solid color-mix(in srgb, var(--color-danger) 45%, transparent); }}
.s-banner--warn {{ background: color-mix(in srgb, var(--color-warn) 18%, transparent); color: var(--color-warn); border: 1px solid color-mix(in srgb, var(--color-warn) 45%, transparent); }}
.s-banner--info {{ background: color-mix(in srgb, var(--color-info) 16%, transparent); color: var(--color-info); border: 1px solid color-mix(in srgb, var(--color-info) 40%, transparent); }}

/* ── unified inbox card accents (highlighted safe-default / destructive treatment) ── */
.s-card--danger {{ border-color: color-mix(in srgb, var(--color-danger) 55%, transparent); }}
.s-safe-default {{ background: color-mix(in srgb, var(--color-success) 14%, transparent); border: 1px solid color-mix(in srgb, var(--color-success) 40%, transparent); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); color: var(--color-success); font-size: var(--type-sm); font-weight: 600; }}
.s-field-label {{ color: var(--color-muted); font-size: var(--type-xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }}
.s-mono {{ font-family: 'JetBrains Mono','SF Mono','Consolas',monospace; font-size: var(--type-xs); color: var(--color-muted); }}

/* ── right-rail item cards (W7.3 — stacked: pill / wrapping title / actions) ── */
.s-rail-item {{ display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; margin-bottom: 8px; }}
.s-rail-title {{ font-size: var(--type-sm); color: var(--color-text); line-height: 1.35; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
.s-rail-actions {{ display: flex; justify-content: flex-end; gap: 6px; }}
/* "Needs you" scroller: bound the section so a long pending list scrolls
   internally instead of pushing the "Live" pane below the fold. max-height =>
   shrink-to-fit when short, scroll when long. */
.s-rail-scroll {{ max-height: 260px; overflow-y: auto; }}

/* ── entity rows (shared tool/skill renderers — Phase 5 Slice 3) ── */
.s-text-success {{ color: var(--color-success); }}
.s-dryrun-cell {{ font-size: var(--type-xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }}
.s-dep-badge {{ display: inline-block; background: var(--color-surface2); color: var(--color-text); font-family: 'JetBrains Mono','SF Mono','Consolas',monospace; font-size: 11px; padding: 2px 8px; border-radius: var(--radius-sm); border: 1px solid var(--color-border); white-space: nowrap; }}
.s-tool-chip {{ display: inline-block; background: var(--color-surface2); color: var(--color-text); font-family: 'JetBrains Mono','SF Mono','Consolas',monospace; font-size: 11px; padding: 2px 8px; border-radius: 6px; border: 1px solid var(--color-border); }}
.s-skill-header {{ padding: 14px 18px; cursor: pointer; gap: 14px; }}
.s-skill-row {{ }}
.s-skill-row--deprecated {{ opacity: 0.72; }}
.s-skill-cat {{ display: inline-block; background: color-mix(in srgb, var(--cat, var(--color-muted)) 20%, transparent); color: var(--cat, var(--color-muted)); font-size: 10px; font-weight: 700; padding: 3px 10px; border-radius: 12px; letter-spacing: 0.06em; white-space: nowrap; }}
.s-skill-evidence {{ font-size: 11px; white-space: nowrap; }}
.s-skill-md {{ background: var(--color-surface2); }}

/* ── board-4 fidelity: flatten Quasar Material chrome to the bespoke flat look ── */
.q-btn {{ box-shadow: none !important; text-transform: none; letter-spacing: 0; font-weight: 600; }}
.q-btn .q-btn__content {{ text-transform: none; }}
/* Solid-variant buttons render via ``flat`` (transparent) + a tinted bg class,
   which otherwise leaves Quasar's default primary-blue text on a coloured box
   (~1.2 contrast — near-invisible).  Force readable per-variant text. */
.s-btn--primary, .s-btn--primary .q-btn__content,
.s-btn--danger,  .s-btn--danger  .q-btn__content,
.s-btn--success, .s-btn--success .q-btn__content {{ color: #ffffff !important; }}
.s-btn--warn, .s-btn--warn .q-btn__content {{ color: #1b1404 !important; }}
.s-btn--ghost, .s-btn--ghost .q-btn__content {{ color: var(--color-text) !important; }}
.q-card {{ box-shadow: none !important; }}
.q-field--outlined .q-field__control {{ border-radius: var(--radius-sm); }}
.q-menu {{ border: 1px solid var(--color-border); box-shadow: 0 10px 30px rgba(0,0,0,.5) !important; }}
{_legacy_compat_css()}"""


def _legacy_compat_css() -> str:
    """Back-compat CSS for legacy classes that live pages still reference and
    that the token-driven blocks above do NOT emit. Kept until the page
    migration (later task) removes the last referencing markup. Colours are
    ported to ``var(--color-*)`` tokens — no raw legacy hex — so the
    single-source palette stays authoritative.

    Currently the only legacy selectors still referenced by pages are the
    responsive-sidebar family (used in ``dashboard.py``):
    ``.s-sidebar``, ``.s-sidebar-header``, ``.s-sidebar-label``,
    ``.s-sidebar-group``, ``.s-sidebar-footer``, ``.s-sidebar-toggle``,
    ``.s-sidebar-backdrop`` and the ``body.s-sidebar-open`` toggle state.
    """
    return """
/* ── legacy compat: responsive sidebar (still referenced by dashboard.py) ── */
.s-sidebar-toggle {
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 1100;
    width: 36px;
    height: 36px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--color-border);
    background: var(--color-surface);
    color: var(--color-text);
    font-size: 18px;
    cursor: pointer;
    transition: background 0.15s;
}
.s-sidebar-toggle:hover {
    background: var(--color-surface2);
}

.s-sidebar-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 999;
    cursor: pointer;
}

@media (max-width: 768px) {
    .s-sidebar {
        width: 64px !important;
        min-width: 64px !important;
        padding-left: 4px !important;
        padding-right: 4px !important;
        z-index: 1000;
        transition: width 0.2s ease, min-width 0.2s ease;
    }
    .s-sidebar-label,
    .s-sidebar-footer {
        display: none !important;
    }
    .s-sidebar-toggle {
        display: block;
    }

    body.s-sidebar-open .s-sidebar {
        width: 220px !important;
        min-width: 220px !important;
        padding-left: 12px !important;
        padding-right: 12px !important;
    }
    body.s-sidebar-open .s-sidebar-label,
    body.s-sidebar-open .s-sidebar-footer {
        display: revert !important;
    }
    body.s-sidebar-open .s-sidebar-backdrop {
        display: block;
    }
}

/* ── persistent right rail (Phase 4) ── */
.s-rail {
    width: 300px;
    min-width: 300px;
    background: var(--color-surface);
    border-left: 1px solid var(--color-border);
    padding: 24px 16px;
    height: 100vh;
    position: sticky;
    top: 0;
    overflow-y: auto;
}
@media (max-width: 1100px) {
    .s-rail { display: none !important; }
}

/* ── global ＋New menu (Phase 4) ── */
.s-menu {
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    min-width: 200px;
}
"""
