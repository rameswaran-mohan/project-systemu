"""Systemu storage backends.

  FileVault     — wraps the original file-based Vault (current default)
  SqliteVault   — SQLAlchemy + SQLite (Phase 1)
  ParallelVault — writes to both during migration, reads from primary
"""
