"""Linux UI Introspector.

Uses AT-SPI2 (via pyatspi) to resolve UI components at specific screen coordinates
in Linux/GNOME/KDE environments.
"""

import logging

from sharing_on.collectors.introspectors.base import BaseUIIntrospector

logger = logging.getLogger(__name__)

class LinuxUIIntrospector(BaseUIIntrospector):
    """
    Linux-specific introspector.
    """
    name = "ui_introspect_linux"

    def _introspect_task(self, coord_dict: dict) -> None:
        """Query the Linux at-spi2 accessibility tree at X, Y."""
        x = coord_dict.get("x", 0)
        y = coord_dict.get("y", 0)
        btn = coord_dict.get("button", "left")

        try:
            import pyatspi
            # Linux at-spi2 logic goes here for Phase 2/3.
            # We would use pyatspi.Registry.getDesktop(0) to traverse the tree
            
            # For now, emit an enriched click with "Linux Native" label
            self._emit_enriched_click(x, y, btn, "Linux Native (Hit-Test Pending)")
        except Exception as e:
            logger.debug(f"Linux Introspection failed: {e}")
            self._emit_enriched_click(x, y, btn, "Unknown")
