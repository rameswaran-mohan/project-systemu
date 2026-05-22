#!/usr/bin/env python3
"""fetch_reddit_posts — Fetch top posts from a specific subreddit using the Reddit JSON API.

Parameters (via run() kwargs):
  subreddit (str, required): The name of the subreddit to query.
  limit (int, optional): Number of posts to retrieve. Default 10.
  time_frame (str, optional): Time range for top posts (e.g., 'week', 'month', 'year'). Default 'week'.

Returns (dict):
  success (bool): True if the operation succeeded.
  data (list): List of post objects containing title, score, num_comments, and url.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import requests

TOOL_META = {
    "name": "fetch_reddit_posts",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(subreddit: str, limit: int = 10, time_frame: str = "week", time_filter: str = None, **kwargs) -> dict:
    """Fetch top posts from a specific subreddit."""
    if time_filter is not None:
        time_frame = time_filter

    if not subreddit:
        return {"success": False, "data": [], "error": "subreddit is required"}

    url = f"https://www.reddit.com/r/{subreddit}/top.json"
    params = {"limit": limit, "t": time_frame}
    headers = {"User-Agent": "SystemU-Agent/1.0 (by /u/systemu_agent)"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        posts = []
        children = data.get("data", {}).get("children", [])
        for child in children:
            post_data = child.get("data", {})
            posts.append({
                "title": post_data.get("title"),
                "score": post_data.get("score"),
                "num_comments": post_data.get("num_comments"),
                "url": post_data.get("url")
            })

        return {"success": True, "data": posts, "error": None}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "data": [], "error": str(exc)}
    except Exception as exc:
        return {"success": False, "data": [], "error": f"Unexpected error: {str(exc)}"}
