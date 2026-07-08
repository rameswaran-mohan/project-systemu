"""R-SEC1: dashboard passphrase HASH exposed on Config.

The dashboard passphrase is stored as a scrypt HASH (never the raw
passphrase). For Docker/headless deployment the hash is supplied via the
SYSTEMU_DASHBOARD_PASSPHRASE_HASH env var; this test pins that the value
flows onto the Config dataclass through Config.from_env().
"""


def test_config_exposes_passphrase_hash(monkeypatch):
    from sharing_on.config import Config
    monkeypatch.setenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", "scrypt$14$8$1$aa$bb")
    c = Config.from_env()
    assert c.dashboard_passphrase_hash == "scrypt$14$8$1$aa$bb"


def test_config_passphrase_hash_default_empty(monkeypatch):
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PASSPHRASE_HASH", raising=False)
    from sharing_on.config import Config
    assert Config.from_env().dashboard_passphrase_hash == ""
