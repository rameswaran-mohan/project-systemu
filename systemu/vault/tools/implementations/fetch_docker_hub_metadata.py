#!/usr/bin/env python3
"""fetch_docker_hub_metadata — Retrieve latest tag metadata for a specific Docker image from the Docker Hub V2 API."""
from __future__ import annotations
import logging
import requests

log = logging.getLogger(__name__)

TOOL_META = {
    "name": "fetch_docker_hub_metadata",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(image_name: str = None, images=None, **kwargs) -> dict:
    """Fetch latest tag metadata for a library image from Docker Hub."""
    log.debug("[fetch_docker_hub_metadata] called with image_name=%r images=%r", image_name, images)

    if images is not None and image_name is None:
        lst = images if isinstance(images, list) else [images]
        if lst:
            raw = str(lst[0])
            image_name = raw.split("/")[-1].split(":")[0]

    if not image_name:
        for key, val in kwargs.items():
            if isinstance(val, str) and val.strip():
                image_name = val.strip().split("/")[-1].split(":")[0]
                break
            if isinstance(val, list) and val:
                image_name = str(val[0]).split("/")[-1].split(":")[0]
                break

    if not image_name:
        return {"success": False, "data": None, "error": "image_name is required"}

    url = f"https://hub.docker.com/v2/repositories/library/{image_name}/tags/"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            return {"success": False, "data": None, "error": f"Image {image_name!r} not found"}
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        if not results:
            return {"success": False, "data": None, "error": "No tags found for this image"}
        latest_tag = max(results, key=lambda x: x.get("last_updated", ""))
        return {
            "success": True,
            "data": {
                "image": image_name,
                "tag": latest_tag.get("name"),
                "digest": latest_tag.get("digest"),
                "last_pushed": latest_tag.get("last_updated"),
            },
            "error": None,
        }
    except requests.exceptions.RequestException as exc:
        return {"success": False, "data": None, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "data": None, "error": f"Unexpected error: {exc}"}
