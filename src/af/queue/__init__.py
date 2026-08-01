"""Queue contracts, manager, and built-in drivers."""

from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    UnsupportedQueueFeature,
)
from af.queue.database import DatabaseQueueDriver
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueDriver, QueueFeature, QueueManager, bind_queue_manager
from af.queue.sync import SyncQueueDriver
from af.queue.sync_manager import sync_queue_registration

__all__ = (
    "DatabaseQueueDriver",
    "QueueDispatchDisposition",
    "QueueDispatchReceipt",
    "QueueDriver",
    "QueueFeature",
    "QueueJob",
    "QueueManager",
    "QueueOperationError",
    "QueueReservation",
    "SyncQueueDriver",
    "UnsupportedQueueFeature",
    "bind_queue_manager",
    "database_queue_registration",
    "sync_queue_registration",
)
