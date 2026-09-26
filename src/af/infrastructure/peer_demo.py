"""Executable two-identity peer qualification over real loopback HTTP sockets."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

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


def run_peer_demo() -> dict[str, object]:
    """Exercise pairing, exact lease, isolated execution, signed result and replay denial."""
    with TemporaryDirectory(prefix="af-peer-demo-") as directory:
        host, peer = Ed25519Identity.generate(), Ed25519Identity.generate()
        host_trust = Ed25519Trust({host.key_id: host.public_key_bytes()})
        accept_pairing_invite(
            create_pairing_invite(peer), expected_key_id=peer.key_id, trust=host_trust
        )
        peer_trust = Ed25519Trust({peer.key_id: peer.public_key_bytes()})
        accept_pairing_invite(
            create_pairing_invite(host), expected_key_id=host.key_id, trust=peer_trust
        )
        registry = ProcedureRegistry()
        registry.register(CharacterCountProcedure())
        descriptor = registry.descriptor("character-count")
        assert descriptor is not None
        implementation = content_digest(Path(inspect.getfile(CharacterCountProcedure)).read_bytes())
        resources = InMemoryLeaseRepository()
        ledger = SQLitePeerLedger(Path(directory) / "ledger.sqlite3")
        service = PeerExecutionService(
            host,
            host_trust,
            registry,
            {descriptor.procedure_id: implementation},
            DeterministicVerifier(),
            ledger,
            SubprocessPeerExecutor(),
            ResourceLeaseService(resources),
            ResourceBudget(
                ResourceIdentity(ResourceKind.RAM, "demo-host", "peer-memory"),
                512,
                max_duration=timedelta(minutes=3),
                max_concurrent=1,
            ),
        )
        arguments = {"text": "strawberry", "character": "r"}
        now = datetime.now(UTC)
        lease = PeerLease(
            uuid4().hex,
            peer.key_id,
            host.key_id,
            descriptor.procedure_id,
            descriptor.version,
            descriptor.digest,
            implementation,
            content_digest(canonical_json(arguments).encode()),
            content_digest(b"read-only demonstration"),
            uuid4().hex,
            now,
            now + timedelta(minutes=2),
        )
        request = make_peer_request(peer, service.issue_lease(lease), arguments)
        with LoopbackPeerServer(service) as server:
            client = PeerHTTPClient(server.endpoint)
            result = verify_peer_result(
                client.execute(request),
                request=request,
                trust=peer_trust,
                lease_current=lambda: ledger.current(lease, datetime.now(UTC)),
            )
            replay_rejected = False
            try:
                client.execute(request)
            except ContentRejected as error:
                replay_rejected = error.reason_code == "replay_or_inactive_lease"
        service.revoke(lease.lease_id)
        released = all(item.state is LeaseState.RELEASED for item in resources.list())
        return {
            "status": result["status"],
            "output": result["output"],
            "verified": result["verified"],
            "transport": "loopback-http",
            "execution": "spawned-installed-procedure",
            "signed": True,
            "replay_rejected": replay_rejected,
            "leases_released": released,
            "revocation_observed": not ledger.current(lease, datetime.now(UTC)),
        }


if __name__ == "__main__":
    print(json.dumps(run_peer_demo(), sort_keys=True))
