#!/usr/bin/env python3
"""calculate_rsi — Calculate the Relative Strength Index (RSI) for a given list of closing prices over a specified period.

Parameters (via run() kwargs):
  closing_prices (list[float], required): List of closing prices in chronological order (most recent last).
  period (int, optional): RSI period. Default 14.

Returns (dict):
  success (bool): True if calculation succeeded.
  rsi (float|None): The RSI value for the most recent price, or None on failure.
  error (str|None): Error message or None.
"""
from __future__ import annotations

TOOL_META = {
    "name": "calculate_rsi",
    "tool_type": "python_function",
    "dependencies": [],
}


def run(closing_prices: list, period: int = 14) -> dict:
    """Calculate the Relative Strength Index (RSI) for the given closing prices.

    Uses Wilder's smoothing method: first average gain/loss is a simple average over
    the initial period; subsequent values use exponential smoothing with alpha = 1/period.

    Returns:
        success (bool): True if the operation succeeded.
        rsi (float|None): The RSI value for the most recent price, None on failure.
        error (str|None): Error message on failure, None on success.
    """
    # Validate required parameters
    if not closing_prices or not isinstance(closing_prices, list):
        return {"success": False, "rsi": None, "error": "closing_prices must be a non-empty list"}
    if len(closing_prices) < 2:
        return {"success": False, "rsi": None, "error": "At least 2 closing prices are required to compute RSI"}
    if period < 1:
        return {"success": False, "rsi": None, "error": "period must be >= 1"}

    try:
        # Convert to floats for safety
        prices = [float(p) for p in closing_prices]
        n = len(prices)

        # Compute price changes
        changes = [prices[i] - prices[i - 1] for i in range(1, n)]

        # Need at least period changes for the first average
        if len(changes) < period:
            return {
                "success": False,
                "rsi": None,
                "error": f"Need at least {period} price changes (got {len(changes)}). Provide at least {period + 1} closing prices.",
            }

        # Separate gains and losses
        gains = [max(c, 0.0) for c in changes]
        losses = [max(-c, 0.0) for c in changes]

        # First average gain/loss: simple average over the first `period` changes
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Wilder's smoothing for subsequent values
        alpha = 1.0 / period
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * (1 - alpha)) + (gains[i] * alpha)
            avg_loss = (avg_loss * (1 - alpha)) + (losses[i] * alpha)

        # Compute RSI
        if avg_loss == 0.0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # Round to 2 decimal places for readability
        rsi = round(rsi, 2)

        return {"success": True, "rsi": rsi, "error": None}

    except (IndexError, ValueError, TypeError) as exc:
        return {"success": False, "rsi": None, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "rsi": None, "error": str(exc)}