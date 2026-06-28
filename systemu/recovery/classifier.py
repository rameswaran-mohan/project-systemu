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
    # v0.9.50: a dependency awaiting operator approval / mid-install is DEP_PENDING
    # (transient), NOT a code bug — recognize the dependency-installer's messages so
    # the failure doesn't get misclassified and prematurely finalize a parked task.
    _low = error_text.lower()
    if ("needs operator approval to install" in _low
            or "blocked_pending_approval" in _low
            or "pending approval" in _low
            or "tools deps approve" in _low):
        return ClassifiedError(kind="DEP_PENDING")
    if "PermissionError" in error_text:
        return ClassifiedError(kind="FS_PERMISSION")
    return ClassifiedError(kind="DRY_RUN_FAILED_BUG")
