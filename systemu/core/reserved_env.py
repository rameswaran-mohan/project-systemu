"""R-4 -- environment names a tool-declared credential may NEVER use.

A tool declares the credentials it needs (``Tool.requires_credentials``); for a
forged tool those declarations are LLM output. At run time the registry's
credential gate PROMOTES each resolved credential into this process's
environment (``CredentialResolver.promote_to_env``: ``os.environ[key] = value``)
and every subprocess inherits it. So a declared key is a WRITE to process
configuration under a name the model chose: ``SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS``
widened the SSRF allowance to 169.254.169.254, ``PATH`` / ``PYTHONPATH`` would
redirect what executes, ``HTTPS_PROXY`` would route traffic, ``SSLKEYLOGFILE``
would leak TLS keys.

The reserved set is every name the runtime or the interpreter (or the
subprocesses it starts) reads as configuration. A name is reserved when ANY of:

  (i)  the runtime reads it: every ``SYSTEMU_*`` / ``SHARING_ON_*`` name
       (prefix), plus every other name ``systemu/`` or ``sharing_on/`` reads --
       :data:`RUNTIME_READ_NAMES`, kept complete by
       ``tests/test_reserved_env_names.py``, which AST-scans the tree and FAILS
       on a read this set does not cover (it can never go stale silently);
  (ii) the interpreter, the OS or a subprocess the runtime starts reads it:
       :data:`INTERPRETER_NAMES` and :data:`RESERVED_PREFIXES`, each with the
       reason it is dangerous.

Matching is CASE-INSENSITIVE: Windows environment names are, and ``http_proxy``
is honoured beside ``HTTP_PROXY`` by every HTTP stack. Pure; no imports beyond
the standard library; never raises.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional

# ---------------------------------------------------------------------------
# (ii) prefixes -- the whole family is configuration.
# ---------------------------------------------------------------------------
RESERVED_PREFIXES: Dict[str, str] = {
    "SYSTEMU_": "systemu runtime configuration (every SYSTEMU_* name is read by the runtime)",
    "SHARING_ON_": "systemu runtime configuration (sharing_on config and gateways)",
    "PYTHON": "Python interpreter configuration (PYTHONPATH, PYTHONHOME, PYTHONSTARTUP, "
              "PYTHONSAFEPATH, PYTHON_KEYRING_BACKEND ...): code and module search path",
    "LD_": "dynamic loader on Linux (LD_PRELOAD / LD_LIBRARY_PATH inject native code)",
    "DYLD_": "dynamic loader on macOS (DYLD_INSERT_LIBRARIES injects native code)",
    "PIP_": "pip configuration (PIP_INDEX_URL / PIP_EXTRA_INDEX_URL / PIP_TRUSTED_HOST "
            "redirect the packages a forged tool's dependencies install from)",
    "UV_": "uv installer configuration (index URL, cache, python path)",
    "GIT_": "git configuration (GIT_SSH_COMMAND, GIT_EXEC_PATH, GIT_CONFIG_* run commands)",
    "NPM_CONFIG_": "npm configuration (registry and script settings for node tooling)",
    "PLAYWRIGHT_": "Playwright browser location and download host (the web tools)",
    "LC_": "locale (decides text encoding of subprocess I/O and of the runtime)",
    "XDG_": "per-user config / data / cache locations (keyring, browser profiles, caches)",
    "CONDA_": "conda environment selection (which interpreter and packages run)",
    "DBUS_": "session bus address (the Linux keyring backend talks to it)",
    "KEYRING_": "keyring backend selection and properties (where secrets are read from)",
}

# ---------------------------------------------------------------------------
# (ii) exact interpreter / OS / subprocess names.
# ---------------------------------------------------------------------------
INTERPRETER_NAMES: Dict[str, str] = {
    "PATH": "executable search path: decides which program every subprocess runs",
    "PATHEXT": "Windows executable extensions: decides what counts as a runnable file",
    "VIRTUAL_ENV": "active virtualenv: tools and installers resolve the interpreter from it",
    "HOME": "user home: config files, keyring, ~/.netrc, ~/.gitconfig, Path.home()",
    "USERPROFILE": "Windows user home (Path.home() on Windows)",
    "HOMEDRIVE": "Windows home drive (expanduser fallback)",
    "HOMEPATH": "Windows home path (expanduser fallback)",
    "APPDATA": "Windows roaming app data (pip.ini, tool configs, keyring files)",
    "LOCALAPPDATA": "Windows local app data (runtime data, browser installs, caches)",
    "PROGRAMDATA": "Windows machine-wide app data (machine pip.ini and configs)",
    "ALLUSERSPROFILE": "Windows machine-wide profile (alias of PROGRAMDATA)",
    "TEMP": "temporary directory: tempfile writes and later reads files here",
    "TMP": "temporary directory (Windows and tempfile fallback)",
    "TMPDIR": "temporary directory (POSIX tempfile)",
    "HTTP_PROXY": "routes every plain-HTTP request (urllib, httpx, requests, pip, git)",
    "HTTPS_PROXY": "routes every HTTPS request",
    "ALL_PROXY": "routes every request (curl, httpx socks)",
    "NO_PROXY": "decides which hosts bypass the proxy",
    "FTP_PROXY": "routes FTP requests (urllib)",
    "SSL_CERT_FILE": "trusted CA bundle: a planted CA defeats TLS verification",
    "SSL_CERT_DIR": "trusted CA directory (same)",
    "REQUESTS_CA_BUNDLE": "trusted CA bundle for requests / pip",
    "CURL_CA_BUNDLE": "trusted CA bundle for curl / requests",
    "OPENSSL_CONF": "OpenSSL configuration file: can load engines / providers (native code)",
    "OPENSSL_MODULES": "OpenSSL provider module directory (native code)",
    "SSLKEYLOGFILE": "makes TLS stacks WRITE session keys to a file (traffic decryption)",
    "COMSPEC": "Windows command interpreter run by shell=True subprocesses",
    "SYSTEMROOT": "Windows system directory: DLL search and many system tools need it",
    "WINDIR": "Windows directory (system tools resolve paths from it)",
    "SYSTEMDRIVE": "Windows system drive",
    "PROGRAMFILES": "Windows program files location (installed tools are found there)",
    "OS": "OS identification read by the runtime and passed to tool subprocesses",
    "PSMODULEPATH": "PowerShell module search path (code that PowerShell loads)",
    "SHELL": "POSIX login shell used by shell-spawning tools",
    "USER": "user name (getpass, git identity)",
    "USERNAME": "Windows user name (getpass, identity)",
    "LOGNAME": "POSIX user name (getpass)",
    "LNAME": "user name fallback read by getpass",
    "TZ": "time zone: changes every timestamp the runtime records",
    "LANG": "locale and default encoding of subprocess I/O",
    "LANGUAGE": "locale message language",
    "NODE_OPTIONS": "flags for every node process (--require loads arbitrary code)",
    "NODE_PATH": "node module search path (code that node loads)",
    "WAYLAND_DISPLAY": "display server the browser tools attach to (read by the runtime)",
    "DISPLAY": "X display the browser tools attach to",
}

# ---------------------------------------------------------------------------
# (i) every other name systemu/ or sharing_on/ reads. Derived by the AST scan in
# tests/test_reserved_env_names.py (string literals and module constants passed
# to os.environ.get / os.environ[...] / os.getenv / environ.setdefault / pop /
# "in os.environ", plus the names behind the dynamic read sites that test pins).
# ---------------------------------------------------------------------------
# The provider credential / endpoint names (OPENROUTER_API_KEY and its siblings)
# are NOT listed here: their one declaration is
# systemu.runtime.provider_status.PROVIDER_SPECS (the F19 fence forbids naming
# them anywhere else), and :func:`_provider_env_names` consumes it.
RUNTIME_READ_NAMES: FrozenSet[str] = frozenset({
    "DATABASE_URL", "HTTPS_PROXY", "HTTP_PROXY",
    "HUEY_IMMEDIATE", "HUEY_WORKERS", "JOBMANAGER_MAX_CONCURRENT_EXECUTE",
    "JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH", "LOCALAPPDATA",
    "OPENROUTER_BASE_URL", "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONIOENCODING", "REDIS_URL", "WAYLAND_DISPLAY", "XDG_CACHE_HOME", "XDG_SESSION_TYPE",
    # dynamic sites (see the test's baseline): the cloud-folder census and the
    # restricted subprocess environment of the local tool backend
    "ONEDRIVE", "ONEDRIVECOMMERCIAL", "ONEDRIVECONSUMER",
    "PATH", "LANG", "OS", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
    "HOME", "LC_ALL", "PYTHONPATH",
})


def _provider_env_names() -> FrozenSet[str]:
    """The provider env names, from their ONE declaration (provider_status).

    Imported lazily: this module is a leaf the core models import. If the table
    cannot be read the names are still covered by :data:`RUNTIME_READ_NAMES`'s
    witness test in CI; at run time an unreadable table is logged loudly."""
    try:
        from systemu.runtime.provider_status import PROVIDER_SPECS
        return frozenset(str(s.env).upper() for s in PROVIDER_SPECS)
    except Exception:  # pragma: no cover - the table is part of the package
        import logging
        logging.getLogger(__name__).warning(
            "[reserved_env] provider table unreadable -- provider names not reserved")
        return frozenset()


def reserved_reason(name: object) -> Optional[str]:
    """Why ``name`` may not be a tool-declared credential, or ``None``.

    Case-insensitive. A non-string or empty name is reserved (fail-closed:
    nothing unnamed is ever promoted)."""
    if type(name) is not str or not name.strip():
        return "not a usable variable name"
    upper = name.strip().upper()
    if upper in INTERPRETER_NAMES:
        return INTERPRETER_NAMES[upper]
    for prefix, why in RESERVED_PREFIXES.items():
        if upper.startswith(prefix):
            return why
    if upper in RUNTIME_READ_NAMES:
        return "read by the systemu runtime as configuration"
    if upper in _provider_env_names():
        return "a model-provider credential or endpoint the runtime reads"
    return None
