"""Fenced resource-lease adapter for queue worker admission permits."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import timedelta
from math import isfinite
from threading import Condition, Event, RLock, Thread

from af.application.resource_leases import ResourceLeaseService
from af.domain.resources import Lease, LeaseState, ResourceBudget
from af.framework.errors import (
    LeaseBudgetExceeded,
    LeaseConcurrencyExceeded,
    LeaseFenceTokenStale,
)

__all__ = ("FencedResourceLeaseAdmissionGate", "FencedResourceLeasePermit")


def _release_and_reconcile(service: ResourceLeaseService, lease: Lease) -> Lease:
    token = lease.fence_token
    assert token is not None
    released = service.release_lease(lease.id, token)
    released_token = released.fence_token
    assert released_token is not None
    return service.reconcile_lease(released.id, released_token)


def _reconcile_failed_activation(service: ResourceLeaseService, lease_id: str) -> Lease:
    lease = service.get_lease(lease_id)
    if lease.state in {LeaseState.RESERVED, LeaseState.ACTIVE}:
        token = lease.fence_token
        assert token is not None
        lease = service.expire_lease(lease.id, token)
    if lease.state in {
        LeaseState.EXPIRED,
        LeaseState.RELEASED,
        LeaseState.REVOKED,
    }:
        token = lease.fence_token
        assert token is not None
        lease = service.reconcile_lease(lease.id, token)
    if lease.state is not LeaseState.RECONCILED:
        raise RuntimeError("worker resource activation cleanup did not reconcile lease")
    return lease


class FencedResourceLeasePermit:
    """Own one active fenced lease through renewal, release, and reconciliation."""

    def __init__(
        self,
        service: ResourceLeaseService,
        lease: Lease,
        *,
        authorization_digest: str,
        renewal_interval: timedelta | None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        if not isinstance(service, ResourceLeaseService):
            raise TypeError("worker resource permit requires ResourceLeaseService")
        if not isinstance(lease, Lease) or lease.state is not LeaseState.ACTIVE:
            raise ValueError("worker resource permit requires an active lease")
        if not authorization_digest:
            raise ValueError("worker resource permit requires authorization evidence")
        if renewal_interval is not None and renewal_interval <= timedelta(0):
            raise ValueError("worker resource renewal interval must be positive")
        if not callable(thread_factory):
            raise TypeError("worker resource thread factory must be callable")
        self._service = service
        self._lease = lease
        self._authorization_digest = authorization_digest
        self._renewal_interval = renewal_interval
        self._stop = Event()
        self._lock = RLock()
        self._release_condition = Condition(self._lock)
        self._failure: BaseException | None = None
        self._released = False
        self._release_in_progress = False
        self._thread: Thread | None = None
        if renewal_interval is not None:
            self._thread = thread_factory(
                target=self._renew_loop,
                name="af-worker-resource-renewal",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException as startup_failure:
                self._stop.set()
                try:
                    if self._thread.ident is not None:
                        self._thread.join()
                except BaseException as cleanup_failure:
                    raise BaseExceptionGroup(
                        "worker resource renewal startup and cleanup both failed",
                        (startup_failure, cleanup_failure),
                    ) from None
                raise

    @property
    def lease(self) -> Lease:
        with self._lock:
            return self._lease

    def renew(self) -> Lease:
        """Synchronously renew with the latest owned fence and authorization digest."""

        with self._lock:
            if self._released:
                raise RuntimeError("worker resource permit is released")
            if self._release_in_progress:
                raise RuntimeError("worker resource permit is releasing")
            if self._failure is not None:
                raise self._failure
            token = self._lease.fence_token
            assert token is not None
            renewed = self._service.renew_lease(
                self._lease.id,
                token,
                authorization_digest=self._authorization_digest,
            )
            self._lease = renewed
            return renewed

    def release(self) -> None:
        """Stop renewal, release the latest fence, and reconcile exactly once."""

        with self._release_condition:
            while self._release_in_progress:
                self._release_condition.wait()
            if self._released:
                return
            self._release_in_progress = True
        renewal_failure: BaseException | None = None
        release_failure: BaseException | None = None
        externally_revoked = False
        try:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join()
            with self._lock:
                renewal_failure = self._failure
                lease = self._lease
            persisted = self._service.get_lease(lease.id)
            externally_revoked = persisted.state is LeaseState.REVOKED
            owned_token = lease.fence_token
            persisted_token = persisted.fence_token
            if (
                persisted.state is LeaseState.ACTIVE
                and owned_token is not None
                and persisted_token is not None
                and persisted_token.value != owned_token.value
            ):
                # The persisted active lease belongs to a newer fencing generation.
                # Relinquish this local permit without mutating the other owner's lease.
                with self._lock:
                    self._released = True
                if renewal_failure is not None:
                    raise renewal_failure
                raise LeaseFenceTokenStale(lease.id, persisted_token.value, owned_token.value)
            if persisted.state in {
                LeaseState.EXPIRED,
                LeaseState.RELEASED,
                LeaseState.REVOKED,
                LeaseState.RECONCILED,
            }:
                lease = persisted
                with self._lock:
                    self._lease = lease
            if lease.state is LeaseState.ACTIVE:
                token = lease.fence_token
                assert token is not None
                lease = self._service.release_lease(lease.id, token)
                with self._lock:
                    self._lease = lease
            if lease.state in {
                LeaseState.EXPIRED,
                LeaseState.RELEASED,
                LeaseState.REVOKED,
            }:
                token = lease.fence_token
                assert token is not None
                lease = self._service.reconcile_lease(lease.id, token)
                with self._lock:
                    self._lease = lease
            if lease.state is not LeaseState.RECONCILED:
                raise RuntimeError("worker resource permit cleanup did not reconcile lease")
            with self._lock:
                self._released = True
            if externally_revoked and isinstance(renewal_failure, LeaseFenceTokenStale):
                renewal_failure = None
        except BaseException as error:
            release_failure = error
        finally:
            with self._release_condition:
                self._release_in_progress = False
                self._release_condition.notify_all()
        if release_failure is renewal_failure:
            release_failure = None
        if renewal_failure is not None and release_failure is not None:
            raise BaseExceptionGroup(
                "worker resource renewal and release both failed",
                (renewal_failure, release_failure),
            ) from None
        if release_failure is not None:
            raise release_failure.with_traceback(release_failure.__traceback__)
        if renewal_failure is not None:
            raise renewal_failure.with_traceback(renewal_failure.__traceback__)

    def _renew_loop(self) -> None:
        assert self._renewal_interval is not None
        while not self._stop.wait(self._renewal_interval.total_seconds()):
            if self._stop.is_set():
                return
            try:
                self.renew()
            except BaseException as error:
                with self._lock:
                    self._failure = error
                return


class FencedResourceLeaseAdmissionGate:
    """Acquire one authorization-bound fenced capacity lease per admitted delivery."""

    def __init__(
        self,
        service: ResourceLeaseService,
        *,
        budget: ResourceBudget,
        quantity: float,
        scope: Mapping[str, str],
        authorized_by: str,
        authorization_digest: str,
        lease_id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        renewal_interval: timedelta | None = None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        if not isinstance(service, ResourceLeaseService):
            raise TypeError("worker resource gate requires ResourceLeaseService")
        if not isinstance(budget, ResourceBudget):
            raise TypeError("worker resource gate requires ResourceBudget")
        if (
            not isinstance(quantity, (int, float))
            or isinstance(quantity, bool)
            or not isfinite(quantity)
            or quantity <= 0
        ):
            raise ValueError("worker resource quantity must be positive")
        if (
            not isinstance(budget.max_quantity, (int, float))
            or isinstance(budget.max_quantity, bool)
            or not isfinite(budget.max_quantity)
            or budget.max_quantity <= 0
        ):
            raise ValueError("worker resource budget maximum must be finite and positive")
        if quantity > budget.max_quantity:
            raise ValueError("worker resource quantity must not exceed budget maximum")
        if not authorized_by or not authorization_digest:
            raise ValueError("worker resource gate requires authorization evidence")
        if not callable(lease_id_factory) or not callable(thread_factory):
            raise TypeError("worker resource gate factories must be callable")
        resolved_scope = dict(scope)
        if any(
            type(key) is not str or type(value) is not str for key, value in resolved_scope.items()
        ):
            raise ValueError("worker resource scope must contain string identities")
        duration = budget.max_duration
        resolved_interval = renewal_interval
        if resolved_interval is None and duration is not None:
            resolved_interval = duration / 2
        if resolved_interval is not None and (
            resolved_interval <= timedelta(0) or duration is None or resolved_interval >= duration
        ):
            raise ValueError("worker resource renewal interval must precede lease duration")
        self._service = service
        self._budget = budget
        self._quantity = float(quantity)
        self._scope = resolved_scope
        self._authorized_by = authorized_by
        self._authorization_digest = authorization_digest
        self._lease_id_factory = lease_id_factory
        self._renewal_interval = resolved_interval
        self._thread_factory = thread_factory

    def __call__(
        self,
        *,
        lane: str | None,
        active_jobs: int,
        max_concurrency: int,
    ) -> bool | FencedResourceLeasePermit:
        if type(active_jobs) is not int or active_jobs < 0:
            raise ValueError("worker resource active-job count must be a non-negative integer")
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("worker maximum concurrency must be a positive integer")
        capacity = self._budget.max_concurrent
        if active_jobs >= max_concurrency or (capacity is not None and active_jobs >= capacity):
            return False
        scope = dict(self._scope)
        if lane is not None:
            existing = scope.get("queue_lane")
            if existing is not None and existing != lane:
                return False
            scope["queue_lane"] = lane
        lease_id = self._lease_id_factory()
        try:
            created = self._service.create_lease(
                lease_id,
                self._budget,
                self._quantity,
                scope,
                self._authorized_by,
                self._authorization_digest,
            )
        except (LeaseBudgetExceeded, LeaseConcurrencyExceeded):
            return False
        token = created.fence_token
        assert token is not None
        try:
            active = self._service.activate_lease(
                created.id,
                token,
                self._authorization_digest,
            )
        except BaseException as activation_failure:
            try:
                _reconcile_failed_activation(self._service, created.id)
            except BaseException as cleanup_failure:
                raise BaseExceptionGroup(
                    "worker resource activation and cleanup both failed",
                    (activation_failure, cleanup_failure),
                ) from None
            raise
        try:
            return FencedResourceLeasePermit(
                self._service,
                active,
                authorization_digest=self._authorization_digest,
                renewal_interval=self._renewal_interval,
                thread_factory=self._thread_factory,
            )
        except BaseException as startup_failure:
            try:
                _release_and_reconcile(self._service, active)
            except BaseException as cleanup_failure:
                raise BaseExceptionGroup(
                    "worker resource permit startup and cleanup both failed",
                    (startup_failure, cleanup_failure),
                ) from None
            raise
