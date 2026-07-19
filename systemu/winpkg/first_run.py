"""E2 — the first-run wizard (SPEC §14 E2 step 4; AC4).

Provider key entry -> provider verify -> hand off to the T3 consult when the
provider is live, else to the deterministic palette.

**The DEC-8 rule is the whole point of this module: the UI never renders a
stored key back.** That is enforced here structurally rather than by
convention:

  * :class:`ProviderKeyReceipt` has no field that can hold the raw key. What
    the wizard hands upward is a mask (``<redacted:1234>``) and the name of the
    backend that accepted it — there is nothing to accidentally render.
  * Every error path runs through :func:`_scrub`, because the value can arrive
    back at us inside somebody *else's* exception text. A keyring backend that
    echoes the credential into its error message would otherwise launder the
    secret straight into a log line, and the DEC-8 assertion would still pass
    when read naively (we never *deliberately* logged it).

That second point is the reason this is not simply "don't log the key".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from systemu.runtime.credentials.store import mask_secret

#: Where the wizard sends the operator once the key step is done (SPEC §14 E2).
HANDOFF_T3_CONSULT = "t3_consult"
HANDOFF_DETERMINISTIC_PALETTE = "deterministic_palette"

#: The credential name the provider key is stored under.
DEFAULT_KEY_NAME = "llm_api_key"


class SupportsCredentialSet(Protocol):
    """The slice of :class:`~systemu.runtime.credentials.store.CredentialStore`
    this module needs. Declared so tests can drive a real store OR a failing
    one without the module importing a concrete backend."""

    def set(self, key: str, value: str) -> str: ...


class ProviderKeyRejected(Exception):
    """The key could not be stored. The message NEVER contains the key value."""


def _scrub(text: str, secret: str) -> str:
    """Remove ``secret`` from ``text``.

    Defends against a backend that echoes the credential inside its own
    exception message. Short secrets are not special-cased away: if the value
    appears at all, it goes.
    """
    if not secret or not text:
        return text
    return text.replace(secret, mask_secret(secret))


@dataclass(frozen=True)
class ProviderKeyReceipt:
    """Proof a key was stored, carrying no way to recover it.

    Deliberately has no ``value``/``key``/``raw`` field — see the module
    docstring. ``masked`` is safe to render anywhere.
    """

    key_name: str
    masked: str
    backend: str

    def __str__(self) -> str:          # pragma: no cover - trivial
        return f"{self.key_name}={self.masked} (stored in {self.backend})"


@dataclass(frozen=True)
class FirstRunResult:
    """Outcome of the wizard."""

    receipt: Optional[ProviderKeyReceipt]
    provider_live: bool
    handoff: str
    notes: tuple = field(default=())


def record_provider_key(
    raw_key: str,
    *,
    store: SupportsCredentialSet,
    key_name: str = DEFAULT_KEY_NAME,
) -> ProviderKeyReceipt:
    """Store the operator's provider key and return a masked receipt.

    Raises :class:`ProviderKeyRejected` — with a scrubbed message — if the key
    is blank or the store refuses it.
    """
    if raw_key is None or not str(raw_key).strip():
        # No scrub needed (there is nothing to leak) but we still say nothing
        # about the input beyond "it was empty".
        raise ProviderKeyRejected("no provider key was entered")

    key = str(raw_key).strip()

    try:
        backend = store.set(key_name, key)
    except Exception as exc:                     # noqa: BLE001 - re-raised, scrubbed
        raise ProviderKeyRejected(
            f"the provider key could not be stored: {_scrub(str(exc), key)}"
        ) from None                              # `from None`: the original
        # traceback could carry the value in a frame local / __context__ repr.

    return ProviderKeyReceipt(
        key_name=key_name,
        masked=mask_secret(key),
        backend=str(backend),
    )


def decide_handoff(provider_live: bool) -> str:
    """Where the wizard sends the operator next (SPEC §14 E2 step 4).

    Live provider -> the T3 "Set the table" consult. Otherwise the deterministic
    palette, which needs no model.
    """
    return HANDOFF_T3_CONSULT if provider_live else HANDOFF_DETERMINISTIC_PALETTE


def run_first_run(
    raw_key: Optional[str],
    *,
    store: SupportsCredentialSet,
    verify_provider,
    key_name: str = DEFAULT_KEY_NAME,
) -> FirstRunResult:
    """The wizard, as a pure-ish orchestration over injected effects.

    ``verify_provider`` is a zero-arg callable returning ``True`` when the
    provider answered. It is injected rather than imported so this module never
    performs network I/O of its own.
    """
    receipt: Optional[ProviderKeyReceipt] = None
    notes: list = []

    if raw_key is not None and str(raw_key).strip():
        receipt = record_provider_key(raw_key, store=store, key_name=key_name)
    else:
        notes.append("no provider key entered; continuing without a model")

    provider_live = False
    if receipt is not None:
        try:
            provider_live = bool(verify_provider())
        except Exception as exc:                 # noqa: BLE001
            # Scrub with the *raw* key: a failing verify call may quote the
            # credential it just tried to use.
            notes.append(f"provider verification failed: {_scrub(str(exc), str(raw_key).strip())}")
            provider_live = False

    return FirstRunResult(
        receipt=receipt,
        provider_live=provider_live,
        handoff=decide_handoff(provider_live),
        notes=tuple(notes),
    )
