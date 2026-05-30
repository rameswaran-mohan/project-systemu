"""Classify a dry-run error string into a RecoveryAction kind."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class ClassifiedError:
    kind: Literal["DEP_PENDING", "FS_PERMISSION", "DRY_RUN_FAILED_BUG"]
    missing_package: Optional[str] = None


_IMPORT_RE = re.compile(
    r"(?:ImportError|ModuleNotFoundError):\s*No module named ['\"]([^'\"]+)['\"]"
)


def classify_dry_run_error(error_text: str, missing_packages: Optional[list] = None) -> ClassifiedError:
    # v0.8.10: prefer a structured missing-packages list over regex sniffing.
    if missing_packages:
        return ClassifiedError(kind="DEP_PENDING", missing_package=missing_packages[0])
    if not error_text:
        return ClassifiedError(kind="DRY_RUN_FAILED_BUG")
    m = _IMPORT_RE.search(error_text)
    if m:
        return ClassifiedError(kind="DEP_PENDING", missing_package=m.group(1))
    if "PermissionError" in error_text:
        return ClassifiedError(kind="FS_PERMISSION")
    return ClassifiedError(kind="DRY_RUN_FAILED_BUG")
