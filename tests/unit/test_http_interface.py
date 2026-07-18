from __future__ import annotations

from af.interfaces.http import _trusted_hosts_for


def test_trusted_hosts_for_loopback_and_non_loopback_hosts() -> None:
    assert _trusted_hosts_for("localhost") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("127.0.0.1") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("::1") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("[::1]") == ["127.0.0.1", "::1", "[::1]", "localhost"]
    assert _trusted_hosts_for("example.com") == ["example.com"]
