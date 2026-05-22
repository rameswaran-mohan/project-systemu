#!/usr/bin/env python3
"""calculate_sma — Calculate Simple Moving Average for a given list of prices over a specified window

Parameters (via run() kwargs):
  prices (list[float], required): List of prices in chronological order (most recent last).
  window (int, optional): SMA window (e.g., 20 for 20-day SMA). Default 20.

Returns (dict):
  success (bool): True if calculation succeeded.
  sma (float|None): The SMA value for the most recent price, or None on failure.
  error (str|None): Error message or None.
"""
from __future__ import annotations

TOOL_META = {
    "name": "calculate_sma",
    "tool_type": "python_function",
    "dependencies": [],
}


def run(prices: list = None, window: int = 20) -> dict:
    """Calculate Simple Moving Average for the last `window` prices."""
    if prices is None or not isinstance(prices, list) or len(prices) == 0:
        return {"success": False, "sma": None, "error": "prices must be a non-empty list of numbers"}

    if not isinstance(window, int) or window <= 0:
        return {"success": False, "sma": None, "error": "window must be a positive integer"}

    if len(prices) < window:
        return {
            "success": False,
            "sma": None,
            "error": f"Not enough prices: need {window}, got {len(prices)}",
        }

    try:
        # Take the last `window` prices
        recent_prices = prices[-window:]

        # Ensure all elements are numeric
        numeric_prices = [float(p) for p in recent_prices]

        sma = sum(numeric_prices) / len(numeric_prices)

        return {"success": True, "sma": sma, "error": None}
    except (TypeError, ValueError) as exc:
        return {"success": False, "sma": None, "error": f"Invalid price data: {exc}"}
    except Exception as exc:
        return {"success": False, "sma": None, "error": str(exc)}