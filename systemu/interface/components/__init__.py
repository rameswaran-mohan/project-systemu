"""Reusable NiceGUI components for the Systemu dashboard.

Each module here exposes a single ``build_<name>(...)`` function that
renders one focused widget.  Components are deliberately small and
side-effect free — they read from the AppState vault, register
optional refresh timers, and never own routing.

The Overview page composes these into expansion cards; full-route
pages keep their existing implementations so external bookmarks
don't break.
"""
