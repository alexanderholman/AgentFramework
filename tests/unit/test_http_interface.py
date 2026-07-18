from __future__ import annotations

import pytest

from af.framework.errors import InvalidProviderConfiguration
from af.interfaces.http import _trusted_hosts_for


def test_trusted_hosts_for_loopback_and_non_loopback_hosts() -> None:
    assert _trusted_hosts_for("localhost") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("127.0.0.1") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("::1") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("[::1]") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("example.com", ("example.com",)) == ["example.com"]


def test_trusted_hosts_for_requires_explicit_hosts_for_non_loopback_binds() -> None:
    with pytest.raises(InvalidProviderConfiguration):
        _trusted_hosts_for("0.0.0.0")

    assert _trusted_hosts_for("0.0.0.0", ("af.internal", "192.168.1.20")) == [
        "192.168.1.20",
        "af.internal",
    ]
