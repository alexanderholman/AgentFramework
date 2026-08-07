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
from af.queue.worker_kernel import (
    BatchResult,
    CancellationPolicy,
    FailedJob,
    HeartbeatConfig,
    JobTag,
    OptimizationProvider,
    WaitAlertConfig,
    WaitAlertLevel,
    WorkerKernel,
    WorkerMetrics,
    WorkerState,
    WorkerSupervisor,
)

__all__ = (
    "BatchResult",
    "CancellationPolicy",
    "DatabaseQueueDriver",
    "FailedJob",
    "HeartbeatConfig",
    "JobTag",
    "OptimizationProvider",
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
    "WaitAlertConfig",
    "WaitAlertLevel",
    "WorkerKernel",
    "WorkerMetrics",
    "WorkerState",
    "WorkerSupervisor",
    "bind_queue_manager",
    "database_queue_registration",
    "sync_queue_registration",
)
