"""Systemu abstraction layer — backend-agnostic interfaces.

Every component that needs storage, queuing, events, or approvals imports
from here, never from a concrete implementation.  This decoupling is what
allows swapping file-based → SQLite → PostgreSQL without touching pipeline
code, dashboard pages, or the shadow runtime.

    from systemu.abstractions import IVault, ITaskQueue, IEventBroker, IApprovalGate
"""

from systemu.abstractions.vault import IVault
from systemu.abstractions.task_queue import ITaskQueue, TaskStatus
from systemu.abstractions.event_broker import IEventBroker
from systemu.abstractions.approval_gate import IApprovalGate

__all__ = [
    "IVault",
    "ITaskQueue",
    "TaskStatus",
    "IEventBroker",
    "IApprovalGate",
]
