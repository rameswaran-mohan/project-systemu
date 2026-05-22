#!/usr/bin/env python3
"""Resize an image file, maintaining aspect ratio when only one dimension is given."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "image_resize",
    "tool_type": "file",
    "dependencies": ["Pillow"],
}


def run(**kwargs) -> dict:
    input_path: str = kwargs.get("input_path", "")
    output_path: str = kwargs.get("output_path", "")
    width: int = int(kwargs.get("width", 0))
    height: int = int(kwargs.get("height", 0))
    quality: int = int(kwargs.get("quality", 85))

    if not input_path:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": "input_path is required"}
    if not output_path:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": "output_path is required"}
    if width == 0 and height == 0:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": "At least one of width or height must be non-zero"}

    try:
        from PIL import Image

        src = Path(input_path).expanduser()
        dst = Path(output_path).expanduser()

        if not src.exists():
            return {"success": False, "output_path": "", "width": 0, "height": 0, "error": f"File not found: {src}"}

        dst.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(str(src))
        orig_width, orig_height = img.size

        if width > 0 and height > 0:
            new_size = (width, height)
        elif width > 0:
            ratio = width / orig_width
            new_size = (width, max(1, round(orig_height * ratio)))
        else:
            ratio = height / orig_height
            new_size = (max(1, round(orig_width * ratio)), height)

        img_resized = img.resize(new_size, Image.LANCZOS)

        save_kwargs: dict = {}
        fmt = dst.suffix.lower()
        if fmt in (".jpg", ".jpeg"):
            save_kwargs["quality"] = quality
            if img_resized.mode in ("RGBA", "P"):
                img_resized = img_resized.convert("RGB")
        elif fmt == ".png":
            compress = max(0, min(9, round((100 - quality) / 11)))
            save_kwargs["compress_level"] = compress

        img_resized.save(str(dst), **save_kwargs)
        final_width, final_height = img_resized.size

        return {"success": True, "output_path": str(dst), "width": final_width, "height": final_height, "error": None}

    except Exception as exc:
        return {"success": False, "output_path": "", "width": 0, "height": 0, "error": str(exc)}
