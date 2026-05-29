"""Back-compat shim — Overview was renamed to Console in v0.8.8.

Any importer of build_overview_page continues to work; it now points at the
Console page builder.
"""
from systemu.interface.pages.console import build_console_page

# Back-compat alias — external importers (dashboard route, tests) keep working.
build_overview_page = build_console_page
