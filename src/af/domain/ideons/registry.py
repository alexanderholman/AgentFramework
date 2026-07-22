"""Domain contracts and deterministic key policy for Ideon persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .canonical import composite_key_for
from .models import IdeonGraph, IdeonIdentity, IdeonRef

MAX_MANIFEST_PRIME_KEY = (1 << 64) - 1
_MILLER_RABIN_64_BASES = (2, 325, 9_375, 28_178, 450_775, 9_780_504, 1_795_265_022)


class IdeonRegistryError(Exception):
    """Base class for all Ideon store failures."""


class IdeonConflictError(IdeonRegistryError, ValueError):
    """Base class for attempts to reuse immutable identities inconsistently."""


class StableKeyConflictError(IdeonConflictError):
    """A stable key or known identity was paired inconsistently."""


class IdentityConflictError(IdeonConflictError):
    """A revision attempted to change the identity paired with a stable key."""


class RevisionConflictError(IdeonConflictError):
    """A revision digest was observed with different canonical graph bytes."""


class NamespaceConflictError(IdeonConflictError):
    """A graph does not belong to a namespace-scoped store."""


class IdeonNotFoundError(IdeonRegistryError, LookupError):
    """An exact Ideon reference or stable key is absent from a store."""


def validate_stable_key(key: int) -> None:
    """Enforce the positive-natural key contract and reserve zero for control."""

    if isinstance(key, bool) or not isinstance(key, int) or key <= 0:
        raise ValueError("stable key must be a natural number greater than zero")


def is_prime_number(value: int) -> bool:
    """Return exact primality within the declared unsigned 64-bit manifest lane."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 2
        or value > MAX_MANIFEST_PRIME_KEY
    ):
        return False
    small_primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    if value in small_primes:
        return True
    if any(value % prime == 0 for prime in small_primes):
        return False

    odd_component = value - 1
    power_of_two = 0
    while odd_component % 2 == 0:
        power_of_two += 1
        odd_component //= 2

    for base in _MILLER_RABIN_64_BASES:
        if base % value == 0:
            continue
        witness = pow(base, odd_component, value)
        if witness in (1, value - 1):
            continue
        for _ in range(power_of_two - 1):
            witness = pow(witness, 2, value)
            if witness == value - 1:
                break
        else:
            return False
    return True


def validate_prime_key(key: int) -> None:
    """Require an odd mathematical prime; two stays in the constructed-key lane."""

    validate_stable_key(key)
    if key > MAX_MANIFEST_PRIME_KEY:
        raise ValueError("prime Ideon keys are bounded to an unsigned 64-bit manifest lane")
    if key == 2 or not is_prime_number(key):
        raise ValueError("prime Ideon key must be an odd mathematical prime")


def validate_constructed_key(key: int) -> None:
    """Require the even key lane used by non-prime Ideon identities."""

    validate_stable_key(key)
    if key % 2 != 0:
        raise ValueError("non-prime Ideon key must be even")


@runtime_checkable
class IdeonKeyPolicy(Protocol):
    """Strategy contract for constructed, non-prime stable identity keys."""

    def key_for(self, identity: IdeonIdentity) -> int:
        """Return a positive stable key for ``identity``."""


@dataclass(frozen=True, slots=True)
class CanonicalKeyPolicy:
    """Collision-free even canonical-byte keys for non-prime identities."""

    def key_for(self, identity: IdeonIdentity) -> int:
        key = composite_key_for(identity)
        validate_constructed_key(key)
        return key


@runtime_checkable
class IdeonStore(Protocol):
    """Persistence boundary consumed by domain/application services."""

    @property
    def version(self) -> str:
        """Return the declared registry version."""

    def intern(self, graph: IdeonGraph, *, stable_key: int | None = None) -> IdeonRef:
        """Return the canonical reference, storing the graph when absent."""

    def register(self, graph: IdeonGraph, *, stable_key: int | None = None) -> IdeonRef:
        """Alias for :meth:`intern` used by explicit registration workflows."""

    def revise(self, stable_key: int, graph: IdeonGraph) -> IdeonRef:
        """Add an immutable compatible revision beneath an existing stable key."""

    def resolve(self, ref: IdeonRef) -> IdeonGraph:
        """Resolve the exact stable-key and revision pair."""

    def revisions(self, stable_key: int) -> tuple[IdeonRef, ...]:
        """Return all immutable revisions recorded for a stable key."""
