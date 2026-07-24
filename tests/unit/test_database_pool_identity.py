from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import cast

import pytest

from af.framework.database_pool import DatabasePool
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseTransaction,
    UnsupportedDatabaseFeature,
)


class _DuplicateDriver:
    def __init__(self) -> None:
        self.features = frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.CANCELLATION,
            }
        )
        self.close_calls = 0

    def supports(self, feature: DatabaseFeature, /) -> bool:
        return feature in self.features

    def require(self, feature: DatabaseFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[DatabaseTransaction]:
        del read_only
        return nullcontext(cast(DatabaseTransaction, object()))

    def cancel(self) -> None:
        return

    def close(self) -> None:
        self.close_calls += 1


def test_pool_rejects_duplicate_driver_identity_and_closes_once() -> None:
    driver = _DuplicateDriver()

    with pytest.raises(DatabaseOperationError) as captured:
        DatabasePool(
            lambda: driver,  # type: ignore[arg-type]
            size=2,
            acquire_timeout=1,
        )

    assert captured.value.stage == "database.pool.connect"
    assert driver.close_calls == 1
