from __future__ import annotations

import pytest

from af.framework.container import Container, Scope
from af.framework.errors import InvalidBinding, ServiceNotBound


class Counter:
    pass


def test_singleton_binding_is_created_once() -> None:
    container = Container()
    container.bind(Counter, lambda _: Counter(), scope=Scope.SINGLETON)

    assert container.resolve(Counter) is container.resolve(Counter)


def test_transient_binding_is_created_each_time() -> None:
    container = Container()
    container.bind(Counter, lambda _: Counter(), scope=Scope.TRANSIENT)

    assert container.resolve(Counter) is not container.resolve(Counter)


def test_missing_binding_fails_explicitly() -> None:
    with pytest.raises(ServiceNotBound):
        Container().resolve(Counter)


def test_duplicate_binding_requires_explicit_replacement() -> None:
    container = Container()
    container.instance(Counter, Counter())

    with pytest.raises(InvalidBinding):
        container.instance(Counter, Counter())

    replacement = Counter()
    container.instance(Counter, replacement, replace=True)
    assert container.resolve(Counter) is replacement
