from __future__ import annotations

import inspect
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.peer_execution import (
    PeerExecutionService,
    make_peer_request,
    verify_peer_result,
)
from af.application.resource_leases import ResourceLeaseService
from af.domain.procedures import CharacterCountProcedure
from af.domain.resources import LeaseState, ResourceBudget, ResourceIdentity, ResourceKind
from af.domain.verification import DeterministicVerifier
from af.framework.content import ContentRejected, canonical_json, content_digest
from af.framework.peers import PeerLease
from af.framework.routing import ProcedureRegistry
from af.framework.types import ProcedureResult
from af.infrastructure.peers import (
    LoopbackPeerServer,
    PeerHTTPClient,
    SQLitePeerLedger,
    SubprocessPeerExecutor,
    accept_pairing_invite,
    create_pairing_invite,
)
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.infrastructure.trusted_content import Ed25519Identity, Ed25519Trust

DIGEST = "sha256:" + "a" * 64
ARGS = {"text": "strawberry", "character": "r"}


def nodes(
    tmp_path: Path,
) -> tuple[
    PeerExecutionService,
    Ed25519Identity,
    Ed25519Trust,
    PeerLease,
    InMemoryLeaseRepository,
]:
    host, peer = Ed25519Identity.generate(), Ed25519Identity.generate()
    trust = Ed25519Trust({host.key_id: host.public_key_bytes()})
    accept_pairing_invite(create_pairing_invite(peer), expected_key_id=peer.key_id, trust=trust)
    registry = ProcedureRegistry()
    registry.register(CharacterCountProcedure())
    descriptor = registry.descriptor("character-count")
    assert descriptor is not None
    implementation = content_digest(Path(inspect.getfile(CharacterCountProcedure)).read_bytes())
    resources = InMemoryLeaseRepository()
    service = PeerExecutionService(
        host,
        trust,
        registry,
        {"character-count": implementation},
        DeterministicVerifier(),
        SQLitePeerLedger(tmp_path / "peer.sqlite3"),
        SubprocessPeerExecutor(),
        ResourceLeaseService(resources),
        ResourceBudget(
            ResourceIdentity(ResourceKind.RAM, "receiver", "peer-memory"),
            1024,
            max_duration=timedelta(minutes=5),
            max_concurrent=2,
        ),
    )
    lease = PeerLease(
        "lease-1",
        peer.key_id,
        host.key_id,
        "character-count",
        "1.0.0",
        descriptor.digest,
        implementation,
        content_digest(canonical_json(ARGS).encode()),
        DIGEST,
        "one-use-nonce",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(minutes=2),
    )
    return service, peer, trust, lease, resources


def test_two_signed_loopback_nodes_execute_installed_procedure_and_reject_replay(
    tmp_path: Path,
) -> None:
    service, peer, trust, lease, resources = nodes(tmp_path)
    grant = service.issue_lease(lease)
    request = make_peer_request(peer, grant, ARGS)
    with LoopbackPeerServer(service) as server:
        client = PeerHTTPClient(server.endpoint)
        signed_result = client.execute(request)
        result = verify_peer_result(
            signed_result,
            request=request,
            trust=trust,
            lease_current=lambda: True,
        )
        assert result["status"] == "completed"
        assert result["output"] == "3"
        assert result["connection"] == "direct"
        with pytest.raises(ContentRejected, match="replay_or_inactive_lease"):
            client.execute(request)
    assert all(item.state is LeaseState.RELEASED for item in resources.list())


def test_revocation_digest_mismatch_and_wrong_pairing_fingerprint_are_denied(
    tmp_path: Path,
) -> None:
    service, peer, trust, lease, resources = nodes(tmp_path)
    with pytest.raises(ContentRejected, match="pairing_fingerprint"):
        accept_pairing_invite(create_pairing_invite(peer), expected_key_id=DIGEST, trust=trust)
    grant = service.issue_lease(lease)
    bad_request = make_peer_request(peer, grant, {"text": "banana", "character": "a"})
    with pytest.raises(ContentRejected, match="arguments_digest"):
        service.execute(bad_request)
    service.revoke(lease.lease_id)
    with pytest.raises(ContentRejected, match="replay_or_inactive_lease"):
        service.execute(make_peer_request(peer, grant, ARGS))
    assert resources.list() == []


def test_timeout_and_exhaustion_produce_signed_terminal_result_and_cleanup(tmp_path: Path) -> None:
    service, peer, trust, lease, resources = nodes(tmp_path)
    timed = replace(lease, max_wall_ms=1)
    request = make_peer_request(peer, service.issue_lease(timed), ARGS)
    result = verify_peer_result(
        service.execute(request), request=request, trust=trust, lease_current=lambda: True
    )
    assert result["status"] == "timed_out"
    assert result["output"] is None
    assert all(item.state is LeaseState.RELEASED for item in resources.list())
    exhausted = replace(lease, lease_id="large", nonce="large", max_memory_mib=2048)
    request = make_peer_request(peer, service.issue_lease(exhausted), ARGS)
    result = verify_peer_result(
        service.execute(request), request=request, trust=trust, lease_current=lambda: True
    )
    assert result["status"] == "resource_exhausted"


def test_durable_nonce_survives_restart_and_interrupted_recovery(tmp_path: Path) -> None:
    service, peer, _trust, lease, _ = nodes(tmp_path)
    grant = service.issue_lease(lease)
    request = make_peer_request(peer, grant, ARGS)
    ledger = SQLitePeerLedger(tmp_path / "peer.sqlite3")
    assert ledger.admit(lease, request.digest, datetime.now(UTC))
    reopened = SQLitePeerLedger(tmp_path / "peer.sqlite3")
    assert reopened.recover_interrupted() == (lease.lease_id,)
    assert not reopened.admit(lease, request.digest, datetime.now(UTC))
    with pytest.raises(ContentRejected):
        service.execute(request)


def test_result_binding_and_requester_revocation_reject_signed_but_ineligible_result(
    tmp_path: Path,
) -> None:
    service, peer, trust, lease, _ = nodes(tmp_path)
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    result = service.execute(request)
    with pytest.raises(ContentRejected, match="lease_revoked"):
        verify_peer_result(result, request=request, trust=trust, lease_current=lambda: False)
    substituted = make_peer_request(
        peer, service.issue_lease(replace(lease, lease_id="other", nonce="other")), ARGS
    )
    with pytest.raises(ContentRejected, match="result_binding"):
        verify_peer_result(result, request=substituted, trust=trust, lease_current=lambda: True)


def test_peer_supplied_commands_wrong_grant_signer_and_oversized_inputs_are_denied(
    tmp_path: Path,
) -> None:
    service, peer, _trust, lease, resources = nodes(tmp_path)
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    raw = json.loads(request.payload_json)
    raw["command"] = "echo peer code"
    with pytest.raises(ContentRejected, match="request_schema"):
        service.execute(peer.sign("af.peer.request.v1", raw))
    forged = peer.sign("af.peer.lease.v1", lease.to_dict())
    with pytest.raises(ContentRejected, match="wrong_peer_signer"):
        service.execute(make_peer_request(peer, forged, ARGS))
    for malformed in ([], {"character": []}):
        raw = json.loads(request.payload_json)
        raw["arguments"] = malformed
        with pytest.raises(ContentRejected, match="invalid_arguments"):
            service.execute(peer.sign("af.peer.request.v1", raw))
    raw = json.loads(request.payload_json)
    raw["peer_key_id"] = DIGEST
    with pytest.raises(ContentRejected, match="peer_identity_mismatch"):
        service.execute(peer.sign("af.peer.request.v1", raw))
    small = replace(lease, lease_id="small", nonce="small", max_input_bytes=1)
    with pytest.raises(ContentRejected, match="input_budget"):
        service.execute(make_peer_request(peer, service.issue_lease(small), ARGS))
    assert resources.list() == []


def test_host_identity_version_implementation_and_pure_scope_are_required(tmp_path: Path) -> None:
    service, _peer, _trust, lease, _resources = nodes(tmp_path)
    for change, reason in (
        ({"host_key_id": DIGEST}, "invalid_lease_host"),
        ({"procedure_id": "uninstalled"}, "procedure_not_installed"),
        ({"implementation_digest": DIGEST}, "procedure_digest_mismatch"),
    ):
        with pytest.raises(ContentRejected, match=reason):
            service.issue_lease(replace(lease, **change))

    class EffectfulCount(CharacterCountProcedure):
        pure = False

    registry = ProcedureRegistry()
    registry.register(EffectfulCount())
    service.registry = registry
    descriptor = registry.descriptor("character-count")
    assert descriptor is not None
    with pytest.raises(ContentRejected, match="procedure_not_peer_eligible"):
        service.issue_lease(replace(lease, descriptor_digest=descriptor.digest))


def test_post_dispatch_revocation_failure_and_output_budget_discard_results(tmp_path: Path) -> None:
    service, peer, trust, lease, resources = nodes(tmp_path)

    class RevokingExecutor:
        def execute(self, *args: object) -> ProcedureResult:
            service.revoke(lease.lease_id)
            return ProcedureResult("3")

    service.executor = RevokingExecutor()
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    result = verify_peer_result(
        service.execute(request), request=request, trust=trust, lease_current=lambda: True
    )
    assert result["status"] == "cancelled"
    assert result["output"] is None
    assert all(item.state is LeaseState.RELEASED for item in resources.list())

    class OversizedExecutor:
        def execute(self, *args: object) -> ProcedureResult:
            return ProcedureResult("x" * 50_000)

    service.executor = OversizedExecutor()
    other = replace(lease, lease_id="other", nonce="other")
    request = make_peer_request(peer, service.issue_lease(other), ARGS)
    assert json.loads(service.execute(request).payload_json)["status"] == "failed"

    class FailingExecutor:
        def execute(self, *args: object) -> ProcedureResult:
            raise RuntimeError("private failure details")

    service.executor = FailingExecutor()
    another = replace(lease, lease_id="another", nonce="another")
    request = make_peer_request(peer, service.issue_lease(another), ARGS)
    result_json = service.execute(request).payload_json
    assert json.loads(result_json)["status"] == "failed"
    assert "private failure" not in result_json


def test_key_revocation_during_execution_discards_result(tmp_path: Path) -> None:
    service, peer, trust, lease, resources = nodes(tmp_path)

    class RevokingKeyExecutor:
        def execute(self, *args: object) -> ProcedureResult:
            trust.revoke(peer.key_id)
            return ProcedureResult("3")

    service.executor = RevokingKeyExecutor()
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    result = json.loads(service.execute(request).payload_json)
    assert result["status"] == "cancelled"
    assert result["output"] is None
    assert all(item.state is LeaseState.RELEASED for item in resources.list())


def test_persistent_ledger_refuses_grant_reuse_expiry_and_nonce_collision(tmp_path: Path) -> None:
    service, peer, _trust, lease, _resources = nodes(tmp_path)
    grant = service.issue_lease(lease)
    assert service.issue_lease(lease) == grant
    with pytest.raises(ContentRejected, match="lease_identity_conflict"):
        service.issue_lease(replace(lease, scope_digest="sha256:" + "b" * 64))
    with pytest.raises(ContentRejected, match="nonce_reused"):
        service.issue_lease(replace(lease, lease_id="different"))
    request = make_peer_request(peer, grant, ARGS)
    assert not service.ledger.admit(lease, request.digest, lease.expires_at)
    with pytest.raises(ValueError):
        service.ledger.finish(lease, "invented", datetime.now(UTC))


def test_pairing_expiry_version_and_invalid_material_require_explicit_repair(
    tmp_path: Path,
) -> None:
    _service, peer, trust, _lease, _resources = nodes(tmp_path)
    original = json.loads(create_pairing_invite(peer).payload_json)
    for changes, reason in (
        ({"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}, "pairing_expired"),
        ({"schema_version": "2"}, "pairing_version"),
        ({"public_key": "invalid"}, "pairing_fingerprint"),
    ):
        with pytest.raises(ContentRejected, match=reason):
            accept_pairing_invite(
                peer.sign("af.peer.invite.v1", {**original, **changes}),
                expected_key_id=peer.key_id,
                trust=trust,
            )


def test_disconnected_peer_is_not_silently_retried_elsewhere(tmp_path: Path) -> None:
    service, peer, _trust, lease, _resources = nodes(tmp_path)
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    with socket.socket() as closed_listener:
        closed_listener.bind(("127.0.0.1", 0))
        port = closed_listener.getsockname()[1]
        with pytest.raises(ContentRejected, match="peer_disconnected"):
            PeerHTTPClient(f"http://127.0.0.1:{port}/execute", timeout=0.1).execute(request)
    with pytest.raises(ValueError, match="loopback"):
        PeerHTTPClient("https://example.com/execute")


@pytest.mark.parametrize(
    "event,args",
    [
        ("socket.connect", ()),
        ("subprocess.Popen", ()),
        ("os.remove", ()),
        ("open", ("file", "w", 0)),
        ("open", ("file", None, 2)),
    ],
)
def test_installed_read_only_worker_defense_denies_effects(
    event: str, args: tuple[object, ...]
) -> None:
    from af.infrastructure.peers import _deny_effects

    with pytest.raises(PermissionError):
        _deny_effects(event, args)
    _deny_effects("open", ("file", "r", 0))
    _deny_effects("benign", ())
