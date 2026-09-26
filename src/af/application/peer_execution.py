"""Signed, one-use leases dispatch only exact installed pure procedures."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from af.application.resource_leases import ResourceLeaseService
from af.domain.resources import Lease, LeaseState, ResourceBudget
from af.framework.content import (
    ContentRejected,
    EnvelopeSigner,
    EnvelopeVerifier,
    SignedEnvelope,
    canonical_json,
    content_digest,
)
from af.framework.contracts import Procedure, Verifier
from af.framework.errors import LeaseBudgetExceeded, LeaseConcurrencyExceeded
from af.framework.peers import PeerLease, PeerLedger, PeerProcedureExecutor
from af.framework.routing import ProcedureRegistry
from af.framework.types import Classification, ProcedureEffect


def _arguments(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 64:
        raise ContentRejected("invalid_arguments")
    result: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str or type(item) is not str or len(key) > 128:
            raise ContentRejected("invalid_arguments")
        result[key] = item
    return result


def _one_signer(envelope: SignedEnvelope, expected: str) -> None:
    # Checking the exact signer list prevents a trusted caller from appending a
    # forged host signature alongside its own valid signature.
    if len(envelope.signatures) != 1 or envelope.signatures[0].key_id != expected:
        raise ContentRejected("wrong_peer_signer")


def make_peer_request(
    signer: EnvelopeSigner,
    lease_envelope: SignedEnvelope,
    arguments: Mapping[str, str],
) -> SignedEnvelope:
    return signer.sign(
        "af.peer.request.v1",
        {
            "schema_version": "1.0.0",
            "lease": lease_envelope.to_dict(),
            "arguments": dict(arguments),
            "peer_key_id": signer.key_id,
        },
    )


class PeerExecutionService:
    def __init__(
        self,
        identity: EnvelopeSigner,
        trust: EnvelopeVerifier,
        registry: ProcedureRegistry,
        implementation_digests: Mapping[str, str],
        verifier: Verifier,
        ledger: PeerLedger,
        executor: PeerProcedureExecutor,
        resources: ResourceLeaseService,
        budget: ResourceBudget,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.identity, self.trust, self.registry = identity, trust, registry
        self.implementation_digests = dict(implementation_digests)
        self.verifier, self.ledger, self.executor = verifier, ledger, executor
        self.resources, self.budget, self.clock = resources, budget, clock

    def issue_lease(self, lease: PeerLease) -> SignedEnvelope:
        """Local operator action; never exposed by the peer execution HTTP endpoint."""
        if (
            lease.host_key_id != self.identity.key_id
            or not lease.issued_at <= self.clock() < lease.expires_at
        ):
            raise ContentRejected("invalid_lease_host_or_time")
        self._procedure(lease)
        envelope = self.identity.sign("af.peer.lease.v1", lease.to_dict())
        self.ledger.grant(lease, envelope)
        return envelope

    def revoke(self, lease_id: str) -> None:
        self.ledger.revoke(lease_id)

    def _procedure(self, lease: PeerLease) -> Procedure:
        procedure = self.registry.get(lease.procedure_id)
        descriptor = self.registry.descriptor(lease.procedure_id)
        if procedure is None or descriptor is None:
            raise ContentRejected("procedure_not_installed")
        if (
            descriptor.version != lease.procedure_version
            or descriptor.digest != lease.descriptor_digest
            or self.implementation_digests.get(lease.procedure_id) != lease.implementation_digest
        ):
            raise ContentRejected("procedure_digest_mismatch")
        if (
            descriptor.effect is not ProcedureEffect.READ_ONLY
            or not procedure.pure
            or not procedure.replay_safe
            or descriptor.requires_confirmation
        ):
            raise ContentRejected("procedure_not_peer_eligible")
        return procedure

    def execute(
        self,
        request: SignedEnvelope,
        *,
        disconnected: Callable[[], bool] | None = None,
    ) -> SignedEnvelope:
        payload = self.trust.verify(request, domain="af.peer.request.v1")
        if (
            set(payload) != {"schema_version", "lease", "arguments", "peer_key_id"}
            or payload["schema_version"] != "1.0.0"
        ):
            raise ContentRejected("request_schema")
        grant = SignedEnvelope.from_dict(payload["lease"])
        _one_signer(grant, self.identity.key_id)
        lease = PeerLease.from_dict(self.trust.verify(grant, domain="af.peer.lease.v1"))
        _one_signer(request, lease.peer_key_id)
        if payload["peer_key_id"] != lease.peer_key_id or lease.host_key_id != self.identity.key_id:
            raise ContentRejected("peer_identity_mismatch")
        arguments = _arguments(payload["arguments"])
        encoded = canonical_json(arguments).encode()
        if len(encoded) > lease.max_input_bytes:
            raise ContentRejected("input_budget")
        if content_digest(encoded) != lease.arguments_digest:
            raise ContentRejected("arguments_digest")
        procedure = self._procedure(lease)
        descriptor = self.registry.descriptor(lease.procedure_id)
        assert descriptor is not None
        classification = Classification(descriptor.intent, arguments, 1.0, "trusted-peer")
        procedure.validate(classification)
        if not self.ledger.admit(lease, request.digest, self.clock()):
            raise ContentRejected("replay_or_inactive_lease")
        resource_lease: Lease | None = None
        output: str | None = None
        status = "failed"
        try:
            if not self.ledger.current(lease, self.clock()):
                raise ContentRejected("lease_revoked")
            resource_lease = self.resources.create_lease(
                "peer-" + lease.lease_id,
                self.budget,
                lease.max_memory_mib,
                {
                    "scope": lease.scope_digest,
                    "peer": lease.peer_key_id,
                    "procedure": lease.descriptor_digest,
                    "data": lease.arguments_digest,
                    "egress": "none",
                    "effect": "read_only",
                },
                self.identity.key_id,
                lease.digest,
                invocation_id=request.digest,
                capability_digest=lease.descriptor_digest,
            )
            if resource_lease.fence_token is None:
                raise ContentRejected("resource_fence")
            resource_lease = self.resources.activate_lease(
                resource_lease.id, resource_lease.fence_token, lease.digest
            )

            def cancelled() -> bool:
                assert resource_lease is not None
                try:
                    self.trust.verify(request, domain="af.peer.request.v1")
                    self.trust.verify(grant, domain="af.peer.lease.v1")
                except (ContentRejected, ValueError):
                    return True
                current = self.resources.get_lease(resource_lease.id)
                return (
                    not self.ledger.current(lease, self.clock())
                    or (disconnected is not None and disconnected())
                    or current.state is not LeaseState.ACTIVE
                    or current.fence_token != resource_lease.fence_token
                    or (current.expires_at is not None and current.expires_at <= self.clock())
                )

            # Revalidate installed identity after admission; advertisements are never consulted.
            self._procedure(lease)
            if cancelled():
                raise ContentRejected("execution_cancelled")
            result = self.executor.execute(
                procedure,
                classification,
                self.verifier,
                lease,
                cancelled,
            )
            if cancelled():
                raise ContentRejected("lease_revoked")
            if len(result.output.encode()) > lease.max_output_bytes:
                raise ContentRejected("output_budget")
            output, status = result.output, "completed"
        except (LeaseBudgetExceeded, LeaseConcurrencyExceeded):
            status = "resource_exhausted"
        except ContentRejected as error:
            status = {
                "execution_timeout": "timed_out",
                "lease_revoked": "cancelled",
                "execution_cancelled": "cancelled",
            }.get(error.reason_code, "failed")
        except Exception:
            status = "failed"
        finally:
            if resource_lease is not None:
                current = self.resources.get_lease(resource_lease.id)
                if current.fence_token is not None:
                    if current.state is LeaseState.ACTIVE:
                        self.resources.release_lease(current.id, current.fence_token)
                    elif current.state is LeaseState.RESERVED:
                        self.resources.expire_lease(current.id, current.fence_token)
        if not self.ledger.finish(lease, status, self.clock()):
            output, status = None, "cancelled"
        return self.identity.sign(
            "af.peer.result.v1",
            {
                "schema_version": "1.0.0",
                "host_key_id": self.identity.key_id,
                "peer_key_id": lease.peer_key_id,
                "lease_digest": lease.digest,
                "request_digest": request.digest,
                "nonce": lease.nonce,
                "status": status,
                "output": output if status == "completed" else None,
                "verified": status == "completed",
                "connection": "direct",
            },
        )


def verify_peer_result(
    result: SignedEnvelope,
    *,
    request: SignedEnvelope,
    trust: EnvelopeVerifier,
    lease_current: Callable[[], bool],
) -> Mapping[str, object]:
    request_data: object = json.loads(request.payload_json)
    if not isinstance(request_data, dict):
        raise ContentRejected("result_binding")
    grant = SignedEnvelope.from_dict(request_data.get("lease"))
    lease = PeerLease.from_dict(trust.verify(grant, domain="af.peer.lease.v1"))
    _one_signer(grant, lease.host_key_id)
    _one_signer(result, lease.host_key_id)
    payload = trust.verify(result, domain="af.peer.result.v1")
    if (
        payload.get("request_digest") != request.digest
        or payload.get("lease_digest") != lease.digest
        or payload.get("nonce") != lease.nonce
        or payload.get("peer_key_id") != lease.peer_key_id
        or payload.get("host_key_id") != lease.host_key_id
        or payload.get("schema_version") != "1.0.0"
    ):
        raise ContentRejected("result_binding")
    if not lease_current():
        raise ContentRejected("lease_revoked")
    return payload
