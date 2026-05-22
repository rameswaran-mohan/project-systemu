#!/usr/bin/env python3
"""fetch_nse_stock_data — Fetch live NSE stock data from the NSE India API and return a list of stock dictionaries.

Parameters (via run() kwargs):
  endpoint (str, optional): API endpoint to use: 'quote' for live quotes of all symbols, or 'equity-master' for full list. Default 'quote'.
  symbols (list[str], optional): Optional list of specific NSE symbols to fetch (e.g., ['RELIANCE', 'TCS']). If empty, fetch all available equities. Default [].

Returns (dict):
  success (bool): True if the API call succeeded.
  data (list[dict]): List of stock objects with fields: symbol, lastPrice, change, pChange, totalTradedVolume, dayHigh, dayLow, open, previousClose, lastUpdateTime.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations

import requests

TOOL_META = {
    "name": "fetch_nse_stock_data",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}

# Base URL for NSE India equity stock indices API
NSE_API_BASE = "https://www.nseindia.com/api/equity-stockIndices"

# Default headers to mimic a browser request
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

# Mapping of endpoint names to index query parameter values
ENDPOINT_MAP = {
    "quote": "ALL",
    "equity-master": "ALL",
}


def run(endpoint: str = "quote", symbols: list[str] | None = None) -> dict:
    """Fetch live NSE stock data from the NSE India API.

    Returns:
        success (bool): True if the operation succeeded.
        data (list[dict]): List of stock objects.
        error (str|None): Error message on failure, None on success.
    """
    if symbols is None:
        symbols = []

    # Validate endpoint
    if endpoint not in ENDPOINT_MAP:
        return {
            "success": False,
            "data": [],
            "error": f"Invalid endpoint '{endpoint}'. Must be one of: {', '.join(ENDPOINT_MAP.keys())}",
        }

    index_param = ENDPOINT_MAP[endpoint]
    params = {"index": index_param}

    try:
        # Make the API request with a timeout
        response = requests.get(
            NSE_API_BASE,
            params=params,
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        # Parse JSON response
        json_data = response.json()

        # Extract the 'data' array from the response
        raw_data = json_data.get("data", [])

        if not isinstance(raw_data, list):
            return {
                "success": False,
                "data": [],
                "error": "Unexpected response format: 'data' field is not a list",
            }

        # Normalize fields to match the return schema
        normalized_data = []
        for item in raw_data:
            normalized_item = {
                "symbol": item.get("symbol", ""),
                "lastPrice": item.get("lastPrice", 0.0),
                "change": item.get("change", 0.0),
                "pChange": item.get("pChange", 0.0),
                "totalTradedVolume": item.get("totalTradedVolume", 0),
                "dayHigh": item.get("dayHigh", 0.0),
                "dayLow": item.get("dayLow", 0.0),
                "open": item.get("open", 0.0),
                "previousClose": item.get("previousClose", 0.0),
                "lastUpdateTime": item.get("lastUpdateTime", ""),
            }
            normalized_data.append(normalized_item)

        # Filter by symbols if provided
        if symbols:
            symbols_set = {s.upper() for s in symbols}
            filtered_data = [
                item for item in normalized_data
                if item["symbol"].upper() in symbols_set
            ]
            normalized_data = filtered_data

        return {
            "success": True,
            "data": normalized_data,
            "error": None,
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "data": [],
            "error": "Request timed out after 30 seconds",
        }
    except requests.exceptions.HTTPError as exc:
        return {
            "success": False,
            "data": [],
            "error": f"HTTP error occurred: {exc}",
        }
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "data": [],
            "error": "Failed to connect to NSE API. Check network connectivity.",
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "data": [],
            "error": f"Request failed: {exc}",
        }
    except ValueError as exc:
        return {
            "success": False,
            "data": [],
            "error": f"Failed to parse JSON response: {exc}",
        }
    except Exception as exc:
        return {
            "success": False,
            "data": [],
            "error": f"Unexpected error: {exc}",
        }