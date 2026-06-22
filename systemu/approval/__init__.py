"""Systemu approval gate backends.

  NotificationApprovalGate — wraps notifications.py (CLI + vault queue, default)
  SqliteApprovalGate        — approvals table + polling (Phase 3, cross-process)
  RedisApprovalGate         — Redis BLPOP (Phase 4, cross-machine)
"""
