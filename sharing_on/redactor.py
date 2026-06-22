"""PII redactor — masks sensitive data in event content before LLM processing."""

from __future__ import annotations

import re
from typing import List, Tuple


# Compiled regex patterns for PII detection
_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        "[EMAIL_REDACTED]",
    ),
    (
        "api_key",
        re.compile(
            r"\b(sk-[a-zA-Z0-9]{20,}|"
            r"sk-or-v1-[a-zA-Z0-9]{20,}|"
            r"ghp_[a-zA-Z0-9]{36}|"
            r"ghs_[a-zA-Z0-9]{36}|"
            r"glpat-[a-zA-Z0-9\-]{20,}|"
            r"xox[baprs]-[a-zA-Z0-9\-]{10,})\b"
        ),
        "[API_KEY_REDACTED]",
    ),
    (
        "bearer_token",
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer [TOKEN_REDACTED]",
    ),
    (
        "password_field",
        re.compile(
            r"(password|passwd|pwd|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{4,}['\"]?",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
    (
        "ipv4",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        "[IP_REDACTED]",
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b"),
        "[CARD_REDACTED]",
    ),
]


def redact(
    text: str,
    redact_emails: bool = True,
    redact_ips: bool = False,
    redact_api_keys: bool = True,
) -> str:
    """Apply PII redaction rules to text.

    Args:
        text: The input text to redact.
        redact_emails: Whether to mask email addresses.
        redact_ips: Whether to mask IP addresses (often needed in instructions).
        redact_api_keys: Whether to mask API keys and tokens.

    Returns:
        The redacted text.
    """
    for name, pattern, replacement in _PATTERNS:
        # Skip disabled categories
        if name == "email" and not redact_emails:
            continue
        if name == "ipv4" and not redact_ips:
            continue
        if name in ("api_key", "bearer_token", "password_field") and not redact_api_keys:
            continue

        text = pattern.sub(replacement, text)

    return text


def redact_dict(data: dict, **kwargs) -> dict:
    """Recursively redact string values in a dictionary."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = redact(value, **kwargs)
        elif isinstance(value, dict):
            result[key] = redact_dict(value, **kwargs)
        elif isinstance(value, list):
            result[key] = [
                redact(v, **kwargs) if isinstance(v, str) else v
                for v in value
            ]
        else:
            result[key] = value
    return result
