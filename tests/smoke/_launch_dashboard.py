"""Subprocess entrypoint for the dashboard smoke harness (tests/test_smoke_dashboard_e2e.py).

Boots the REAL dashboard via run_dashboard — i.e. on uvicorn's actual event
loop (a SelectorEventLoop on Windows), the exact environment where BUG-4
(silent forged-tool subprocess failure) and BUG-1 (live timer never
scheduled) hid from the unit suite. Reads:
  SMOKE_PORT        — port to bind
  SYSTEMU_VAULT_DIR — throwaway vault (set by the test)
"""
import os

os.environ.setdefault("SYSTEMU_STORAGE", "file")
os.environ.setdefault("SYSTEMU_NON_INTERACTIVE", "true")
os.environ.setdefault("SYSTEMU_DASHBOARD_HOST", "127.0.0.1")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-dummy-smoke")

from sharing_on.config import Config
config = Config.from_env()            # honours SYSTEMU_VAULT_DIR
from systemu.interface.dashboard_state import AppState
AppState.create(config)
from systemu.interface.dashboard import run_dashboard

run_dashboard(config, port=int(os.environ["SMOKE_PORT"]), host="127.0.0.1")
