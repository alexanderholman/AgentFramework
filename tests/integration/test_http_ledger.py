from concurrent.futures import ThreadPoolExecutor

import pytest

from af.application.http_operations import ApiRejection, StoredResponse
from af.infrastructure.http_operations import SQLiteHttpOperations


def test_durable_claim_replay_request_mismatch_and_pending_never_reexecutes(tmp_path):
    path = tmp_path / "api.db"
    store = SQLiteHttpOperations(path, retention_seconds=10)
    assert store.claim("scope", "key", "request", now=1) is None
    store.close()
    store = SQLiteHttpOperations(path, retention_seconds=10)
    with pytest.raises(ApiRejection, match="uncertain"):
        store.claim("scope", "key", "request", now=1000)
    with pytest.raises(ApiRejection, match="mismatch"):
        store.claim("scope", "key", "different", now=1000)
    response = StoredResponse(201, ((b"content-type", b"application/json"),), b'{"id":"1"}')
    store.complete("scope", "key", "request", response, now=1000)
    assert store.claim("scope", "key", "request", now=1001) == response
    assert store.claim("other-tenant", "key", "request", now=1001) is None
    assert store.claim("scope", "key", "new", now=1010) is None
    store.close()


def test_concurrent_process_like_connections_have_one_claim_and_bounded_rate(tmp_path):
    path = tmp_path / "api.db"
    stores = [SQLiteHttpOperations(path, requests_per_window=3) for _ in range(8)]

    def claim(store):
        try:
            store.claim("scope", "key", "request", now=1)
            return 1
        except ApiRejection:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, stores)) == 1
    for store in stores[:3]:
        store.admit("principal", now=1)
    with pytest.raises(ApiRejection) as error:
        stores[3].admit("principal", now=2)
    assert error.value.status == 429
    stores[3].admit("other", now=2)
    stores[0].admit("principal", now=61)
    for store in stores:
        store.close()


def test_capacity_corruption_and_fenced_completion_are_fail_closed(tmp_path):
    import sqlite3

    path = tmp_path / "bounded.db"
    store = SQLiteHttpOperations(path, max_records=1, max_principals=1)
    store.admit("one", now=1)
    with pytest.raises(ApiRejection, match="capacity"):
        store.admit("two", now=1)
    store.claim("scope", "one", "request", now=1)
    with pytest.raises(ApiRejection, match="capacity"):
        store.claim("scope", "two", "request", now=1)
    response = StoredResponse(200, (), b"ok")
    with pytest.raises(ApiRejection, match="fenced"):
        store.complete("scope", "one", "wrong", response, now=1)
    with pytest.raises(TypeError):
        store.complete("scope", "one", "request", object(), now=1)
    store.complete("scope", "one", "request", response, now=1)
    with pytest.raises(ApiRejection, match="fenced"):
        store.complete("scope", "one", "request", response, now=2)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE af_http_replays SET headers='malformed'")
    with pytest.raises(ApiRejection, match="invalid"):
        store.claim("scope", "one", "request", now=2)
    store.close()


@pytest.mark.parametrize(
    "option,value",
    [
        ("retention_seconds", 0),
        ("max_records", True),
        ("requests_per_window", 100001),
        ("window_seconds", -1),
        ("max_principals", 0),
    ],
)
def test_ledger_rejects_invalid_configuration_before_files(tmp_path, option, value):
    path = tmp_path / "never-created.db"
    with pytest.raises(ValueError):
        SQLiteHttpOperations(path, **{option: value})
    assert not path.exists()


def test_ledger_rejects_unsafe_path_and_invalid_clock_identifiers(tmp_path):
    target = tmp_path / "target"
    target.touch()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        SQLiteHttpOperations(link)
    store = SQLiteHttpOperations(":memory:")
    for now in (True, float("nan"), float("inf"), "yesterday"):
        with pytest.raises(ValueError):
            store.admit("principal", now=now)
    with pytest.raises(ValueError):
        store.claim("", "key", "request", now=1)
    store.close()


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (500, (), b"error"),
        (True, (), b"ok"),
        (200, (), b"x" * 1048577),
        (200, ((b"key", b"x" * 8193),), b"ok"),
        (200, (("text", b"bad"),), b"ok"),
    ],
)
def test_only_bounded_definitive_responses_can_be_committed(status, headers, body):
    with pytest.raises(ValueError):
        StoredResponse(status, headers, body)


def test_ledger_file_with_private_replay_bodies_has_private_permissions(tmp_path):
    path = tmp_path / "ledger.db"
    store = SQLiteHttpOperations(path)
    assert path.stat().st_mode & 0o777 == 0o600
    store.close()
