#!/usr/bin/env python3
"""Take a screenshot of the entire screen or a specified region."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "take_screenshot",
    "tool_type": "system",
    "dependencies": ["mss", "Pillow"],
}


def run(**kwargs) -> dict:
    output_path: str = kwargs.get("output_path", "")
    region: dict = kwargs.get("region", None)

    if not output_path:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": "output_path is required"}

    try:
        import mss
        import mss.tools

        out = Path(output_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)

        with mss.mss() as sct:
            if region:
                mon = {
                    "left": int(region.get("left", 0)),
                    "top": int(region.get("top", 0)),
                    "width": int(region.get("width", 800)),
                    "height": int(region.get("height", 600)),
                }
                sct_img = sct.grab(mon)
                try:
                    from PIL import Image
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    img.save(str(out))
                    width, height = sct_img.size
                except ImportError:
                    mss.tools.to_png(sct_img.rgb, sct_img.size, output=str(out))
                    width, height = sct_img.size
            else:
                sct_img = sct.grab(sct.monitors[1])
                mss.tools.to_png(sct_img.rgb, sct_img.size, output=str(out))
                width, height = sct_img.size

        return {"success": True, "output_path": str(out), "width": width, "height": height, "error": None}

    except Exception as exc:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": str(exc)}
