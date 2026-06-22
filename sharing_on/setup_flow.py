"""First-run CLI setup — safe, secure key + model configuration.

The gap this closes: `pip install systemu` users never run install.py, so
they reach `daemon start` with no API key and a dead config. This module is
the pip-first onboarding: `sharing_on setup` (and an auto-prompt the first
time `daemon start` finds no key) walks the operator through:

  1. OpenRouter key — entered with getpass (NEVER echoed to the terminal or
     scrollback), validated live against the API BEFORE it is stored, and
     written to a 0600-permission .env.
  2. Model preset — stored as the PRESET NAME (SYSTEMU_MODEL_PRESET=balanced),
     not the resolved model ids. This is the hard lesson from the
     deepseek/deepseek-v4 incident: baking resolved ids into .env meant a
     later preset fix could not rescue the install. Storing the name lets
     model-id corrections ship in releases and apply automatically.
  3. Output folder — where produced files land.

stdlib + python-dotenv only (no systemu import) so the dependency direction
stays sharing_on ← systemu, and install.py can reuse it too. Every external
input (getpass / input / validator) is injectable for keyless tests.
"""
from __future__ import annotations

import getpass as _getpass
import os
import re
import stat
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Reuse the canonical no-preset defaults so "validated working" ids are the
# single source of truth.
try:
    from sharing_on.model_presets import PRESETS
    _PRESET_NAMES = [n for n in ("balanced", "quality", "budget")
                     if n in PRESETS] or ["balanced", "quality", "budget"]
except Exception:  # pragma: no cover - presets always import in practice
    _PRESET_NAMES = ["balanced", "quality", "budget"]

_DEFAULT_OUTPUT = str(Path.home() / "Documents" / "systemu-output")


# ── Key handling ─────────────────────────────────────────────────────────────

def validate_openrouter_key(key: str, *, timeout: int = 10) -> Tuple[bool, str]:
    """Probe OpenRouter to confirm the key works. (True, "") on success.

    GET /api/v1/models — cheapest endpoint, no tokens burned. Honors
    HTTP(S)_PROXY env. Connection errors are NOT a hard fail (air-gapped /
    proxy installs proceed with a warning); only a 401 is a definite reject.
    """
    if not key:
        return False, "Key is empty"
    import urllib.error
    import urllib.request

    proxies = {k: v for k, v in (
        ("http", os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")),
        ("https", os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
    ) if v}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": "systemu-setup"},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies) if proxies else
        urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return (True, "") if resp.status == 200 else \
                (False, f"Unexpected HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid key (HTTP 401 from OpenRouter)"
        return False, f"HTTP error {e.code}"
    except Exception as e:  # URLError / timeout / proxy — soft fail
        return False, f"Could not reach OpenRouter ({e}) — network/proxy?"


def mask_key(key: str) -> str:
    """Display-safe key: provider prefix + last 4. Never the full secret."""
    key = (key or "").strip()
    if len(key) <= 8:
        return "****" if key else "(none)"
    return f"{key[:6]}…{key[-4:]}"


# ── .env read / merge / write (0600) ─────────────────────────────────────────

def _parse_env(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if m:
            v = m.group(2)
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def write_env_vars(updates: Dict[str, str], *, env_path: Path) -> Path:
    """Merge ``updates`` into the .env at ``env_path`` (preserving other keys),
    write it, and lock perms to 0600. Returns the path written."""
    env_path = Path(env_path)
    existing = _parse_env(env_path.read_text(encoding="utf-8-sig")) \
        if env_path.exists() else {}
    existing.update({k: v for k, v in updates.items() if v is not None})
    body = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(body, encoding="utf-8")
    try:  # 0600 — owner read/write only (no-op semantics on Windows, harmless)
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return env_path


def key_present(env_path: Optional[Path] = None) -> bool:
    """True when an OpenRouter key is reachable from the process env or .env."""
    if (os.environ.get("OPENROUTER_API_KEY") or "").strip():
        return True
    p = Path(env_path) if env_path else (Path.cwd() / ".env")
    try:
        if p.exists():
            return bool(_parse_env(
                p.read_text(encoding="utf-8-sig")).get("OPENROUTER_API_KEY", "").strip())
    except Exception:
        pass
    return False


# W14: each provider stores its credential under its own env var (one key
# per provider in v1; ollama stores a base_url, no key).
_PROVIDER_CRED_ENV = {
    "openrouter": "OPENROUTER_API_KEY", "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
    "ollama": "OLLAMA_URL",
}


def anthropic_available() -> bool:
    """W14: True when the optional `anthropic` extra is importable — setup
    uses this to gate/explain the Anthropic option."""
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


_PROVIDER_DEFAULT_MODEL = {
    "openrouter": "deepseek/deepseek-v4-flash",
    "google": "google/gemini-3-flash-preview",
    "anthropic": "anthropic/claude-sonnet-4.5",
    "openai": "openai/gpt-4o",
    "ollama": "ollama/llama3.1",
}
_SINGLE_CHOICE = {"1": "openrouter", "2": "google", "3": "openai",
                  "4": "anthropic", "5": "ollama"}
_TIER_CHOICE = ["openrouter", "google", "openai", "anthropic", "ollama"]
_TIER_LABELS = {1: "Tier 1 (deep reasoning)", 2: "Tier 2 (code / structured)",
                3: "Tier 3 (fast / formatting)"}


def _collect_model(provider, input_fn, print_fn):
    default = _PROVIDER_DEFAULT_MODEL.get(provider, "")
    return ((input_fn(f"    Model id for {provider} [{default}]: ") or default)
            .strip())


def _collect_credential(provider, getpass_fn, input_fn, print_fn):
    if provider == "ollama":
        return ((input_fn("    Ollama base URL [http://localhost:11434]: ")
                 or "http://localhost:11434").strip())
    if provider == "anthropic" and not anthropic_available():
        print_fn("    note: the 'anthropic' package isn't installed yet — "
                 "run `pip install 'systemu[anthropic]'` before using it.")
    return (getpass_fn(f"    {provider} API key (hidden): ") or "").strip()


def _interactive_provider_specs(*, getpass_fn, input_fn, print_fn):
    """Ask the provider question. Returns None when the operator picks the
    simple OpenRouter path (caller falls through to the one-key+preset flow),
    or a 3-item tier_specs list for a single non-OpenRouter provider or a
    per-tier mix. Credentials are reused across tiers that share a provider —
    one provider = one key (the operator's 'same token' case)."""
    print_fn("\nStep 1 — Which LLM provider?")
    print_fn("  1. OpenRouter  (recommended — one key, 200+ models)")
    print_fn("  2. Google      (needs a Google AI key)")
    print_fn("  3. OpenAI      (needs an OpenAI key)")
    print_fn("  4. Anthropic   (needs an Anthropic key + the [anthropic] extra)")
    print_fn("  5. Ollama      (local models, no key)")
    print_fn("  6. Different provider per tier (advanced)")
    raw = (input_fn("  Choice [1]: ") or "1").strip()

    if raw in _SINGLE_CHOICE:
        prov = _SINGLE_CHOICE[raw]
        if prov == "openrouter":
            return None  # simple path — best UX for the common case
        print_fn(f"\n  Using {prov} for all three tiers.")
        cred = _collect_credential(prov, getpass_fn, input_fn, print_fn)
        model = _collect_model(prov, input_fn, print_fn)
        # one provider + one key applies to all three tiers
        return [{"provider": prov, "model": model, "credential": cred}
                for _ in range(3)]

    if raw == "6":
        specs, seen = [], {}
        for i in (1, 2, 3):
            print_fn(f"\n  {_TIER_LABELS[i]} — provider:")
            for n, p in enumerate(_TIER_CHOICE, 1):
                print_fn(f"    {n}. {p}")
            if i > 1:
                print_fn("    s. same as tier 1")
            c = (input_fn("    Choice [1]: ") or "1").strip().lower()
            if c == "s" and i > 1:
                prov = specs[0]["provider"]
            elif c.isdigit() and 1 <= int(c) <= len(_TIER_CHOICE):
                prov = _TIER_CHOICE[int(c) - 1]
            else:
                prov = "openrouter"
            # credential BEFORE model (consistent with the single-provider
            # path; reused silently when this provider already appeared).
            if prov in seen:
                cred = seen[prov]
                print_fn(f"    (reusing the {prov} credential from an earlier tier)")
            else:
                cred = _collect_credential(prov, getpass_fn, input_fn, print_fn)
                seen[prov] = cred
            model = _collect_model(prov, input_fn, print_fn)
            specs.append({"provider": prov, "model": model, "credential": cred})
        return specs

    # unrecognized → safest default: OpenRouter simple path
    return None


def _run_tier_specs(tier_specs, *, output_dir, env_path) -> Dict[str, object]:
    """Write a 3-item tier_specs list to .env: per-tier provider + model +
    each provider's credential (de-duped — one key per provider)."""
    updates: Dict[str, str] = {}
    messages: List[str] = []
    seen_cred: Dict[str, str] = {}
    for i, spec in enumerate(tier_specs[:3], start=1):
        prov = (spec.get("provider") or "").strip().lower()
        model = (spec.get("model") or "").strip()
        cred = (spec.get("credential") or "").strip()
        if prov and prov != "auto":
            updates[f"SYSTEMU_TIER{i}_PROVIDER"] = prov
        if model:
            updates[f"SYSTEMU_TIER{i}_MODEL"] = model
        env_name = _PROVIDER_CRED_ENV.get(prov)
        if env_name and cred:
            if seen_cred.get(prov) and seen_cred[prov] != cred:
                messages.append(
                    f"Tier {i}: {prov} credential differs from an earlier tier; "
                    f"v1 keeps one per provider — using the latest.")
            seen_cred[prov] = cred
            updates[env_name] = cred
    provs = [updates.get(f"SYSTEMU_TIER{i}_PROVIDER", "auto") for i in (1, 2, 3)]
    messages.append(f"Per-tier providers: {', '.join(provs)}.")
    if output_dir:
        try:
            Path(output_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messages.append(f"Could not create {output_dir}: {exc}")
        updates["SYSTEMU_OUTPUT_DIR"] = output_dir
    if updates:
        write_env_vars(updates, env_path=Path(env_path))
    return {
        "key_set": any(updates.get(_PROVIDER_CRED_ENV.get(p, "")) for p in seen_cred),
        "validated": False,
        "tier_providers": {i: updates.get(f"SYSTEMU_TIER{i}_PROVIDER", "auto") for i in (1, 2, 3)},
        "output_dir": updates.get("SYSTEMU_OUTPUT_DIR"),
        "env_path": str(env_path),
        "messages": messages,
    }


# ── The wizard ───────────────────────────────────────────────────────────────

def run_setup(
    *,
    interactive: bool = True,
    key: Optional[str] = None,
    preset: Optional[str] = None,
    output_dir: Optional[str] = None,
    env_path: Optional[Path] = None,
    validate: bool = True,
    tier_specs: Optional[List[dict]] = None,
    getpass_fn: Callable[[str], str] = _getpass.getpass,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    validate_fn: Callable[..., Tuple[bool, str]] = validate_openrouter_key,
) -> Dict[str, object]:
    """Run the setup wizard. Returns a summary dict; never raises on bad input.

    Two configuration shapes:
      * Simple (key + preset): one OpenRouter key for all tiers. The
        historical path; used when ``tier_specs`` is None.
      * Per-tier (W14): ``tier_specs`` = a 3-item list of
        ``{provider, model, credential}`` (tier 1, 2, 3). Writes
        SYSTEMU_TIER{N}_PROVIDER / SYSTEMU_TIER{N}_MODEL and each provider's
        credential env var, de-duping a credential already collected for the
        same provider in this run (the operator's "ask per tier, reuse same
        provider" requirement).

    Non-interactive: only explicitly-passed values are written (CI).
    """
    env_path = Path(env_path) if env_path else (Path.cwd() / ".env")
    updates: Dict[str, str] = {}
    messages: List[str] = []
    validated = False

    # ── Per-tier path (W14): explicit tier_specs (CLI flags) ────────────────
    if tier_specs:
        return _run_tier_specs(tier_specs, output_dir=output_dir, env_path=env_path)

    # ── Interactive provider choice (W14b) ──────────────────────────────────
    # The fix for "setup only asked for an OpenRouter key": ask which provider
    # FIRST. OpenRouter → the simple one-key+preset flow below (best UX for
    # the common case). Anything else (or per-tier) builds tier_specs and
    # routes through the same writer the CLI flags use.
    if interactive and key is None:
        built = _interactive_provider_specs(
            getpass_fn=getpass_fn, input_fn=input_fn, print_fn=print_fn)
        if built is not None:
            return _run_tier_specs(built, output_dir=output_dir, env_path=env_path)
        # built is None → operator chose OpenRouter → continue to simple path.

    # ── 1. Key (OpenRouter simple path) ─────────────────────────────────────
    chosen_key = key
    if chosen_key is None and interactive:
        print_fn("\nStep 1 of 3 — OpenRouter API key")
        print_fn("  Get one at https://openrouter.ai/keys . Input is hidden.")
        for _ in range(3):
            entered = (getpass_fn("  Paste your key (blank to skip): ") or "").strip()
            if not entered:
                messages.append("No key set — set it later with `sharing_on setup`.")
                break
            if validate:
                ok, why = validate_fn(entered)
                if ok:
                    chosen_key = entered
                    messages.append(f"Key validated and saved ({mask_key(entered)}).")
                    validated = True
                    break
                if "401" in why:
                    print_fn(f"  ✗ {why} — try again.")
                    continue
                # network/proxy: store as-is, can't verify
                chosen_key = entered
                messages.append(f"Key saved unvalidated ({why}).")
                break
            chosen_key = entered
            break
    if chosen_key:
        updates["OPENROUTER_API_KEY"] = chosen_key
        if not validated and key is not None and validate:
            # Programmatic key with validation requested (install.py path).
            ok, why = validate_fn(chosen_key)
            validated = ok
            messages.append("Key validated." if ok else f"Key unvalidated: {why}")

    # ── 2. Model preset (store the NAME, not resolved ids) ──────────────────
    chosen_preset = preset
    if chosen_preset is None and interactive:
        print_fn("\nStep 2 of 3 — Model preset (changeable any time in Settings)")
        opts = _PRESET_NAMES + ["later"]
        for i, name in enumerate(opts, 1):
            tag = "  (recommended)" if name == "balanced" else ""
            print_fn(f"   {i}. {name}{tag}")
        raw = (input_fn("  Choice [1]: ") or "1").strip()
        idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(opts) else 0
        chosen_preset = opts[idx]
    if chosen_preset and chosen_preset != "later":
        updates["SYSTEMU_MODEL_PRESET"] = chosen_preset
        messages.append(f"Model preset: {chosen_preset}.")

    # ── 3. Output folder ────────────────────────────────────────────────────
    chosen_out = output_dir
    if chosen_out is None and interactive:
        print_fn("\nStep 3 of 3 — Output folder (where produced files land)")
        chosen_out = (input_fn(f"  Folder [{_DEFAULT_OUTPUT}]: ")
                      or _DEFAULT_OUTPUT).strip()
    if chosen_out:
        try:
            Path(chosen_out).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messages.append(f"Could not create {chosen_out}: {exc}")
        updates["SYSTEMU_OUTPUT_DIR"] = chosen_out

    if updates:
        write_env_vars(updates, env_path=env_path)

    return {
        "key_set": "OPENROUTER_API_KEY" in updates,
        "validated": validated,
        "preset": updates.get("SYSTEMU_MODEL_PRESET"),
        "output_dir": updates.get("SYSTEMU_OUTPUT_DIR"),
        "env_path": str(env_path),
        "messages": messages,
    }
