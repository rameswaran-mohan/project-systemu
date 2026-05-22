"""Systemu event broker backends.

  MemoryEventBroker  — wraps the current in-memory EventBus (default)
  SqliteEventBroker  — events table + polling (Phase 3)
  RedisEventBroker   — Redis Streams (Phase 4)
"""
