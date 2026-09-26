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
