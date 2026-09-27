from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from af.application.workflows import WorkflowService
from af.framework.context import ExecutionContext, Principal
from af.framework.workflows import (
    EvidenceRequirement,
    NodeState,
    WorkflowComponent,
    WorkflowDefinition,
    WorkflowError,
    WorkflowNode,
    WorkflowOutput,
)
from af.infrastructure.database import Database
from af.infrastructure.workflows import SqlAlchemyWorkflowRepository


class Component:
    def __init__(self, *, verified: bool = True) -> None:
        self.calls: list[str] = []
        self.verified = verified

    def execute(self, data, predecessors, context, *, effect_key):
        self.calls.append(effect_key)
        return WorkflowOutput(data=data + b"x")

    def verify(self, data, predecessors, output):
        return self.verified


def setup_workflow(tmp_path: Path, *, verified=True, replay_safe=True):
    database = Database(f"sqlite:///{tmp_path / 'workflows.db'}")
    store = SqlAlchemyWorkflowRepository(database, authentication_key=b"k" * 32)
    handler = Component(verified=verified)
    component = WorkflowComponent("append", "1", "a" * 64, replay_safe, handler)
    service = WorkflowService(store, (component,))
    definition = WorkflowDefinition(
        workflow_id="demo",
        version="1",
        nodes=(
            WorkflowNode(node_id="first", component_id="append", component_digest="a" * 64),
            WorkflowNode(
                node_id="second",
                component_id="append",
                component_digest="a" * 64,
                dependencies=(EvidenceRequirement(node_id="first", minimum_bytes=1),),
            ),
        ),
    )
    context = ExecutionContext(principal=Principal("alex", "user"))
    run = service.submit(definition, b"input", context)
    return database, store, service, handler, context, run


def test_restart_resumes_verified_work_without_repeating_committed_effects(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    first = service.resume(run.run_id, context, max_nodes=1)
    assert first.nodes[0].state == NodeState.COMPLETED
    assert first.nodes[1].state == NodeState.BLOCKED
    restarted = WorkflowService(store, service.components)
    done = restarted.resume(run.run_id, context)
    assert all(node.state == NodeState.COMPLETED for node in done.nodes)
    restarted.resume(run.run_id, context)
    assert len(handler.calls) == 2
    assert len(store.history(run.run_id)) > 2
    database.close()


def test_failed_verifier_blocks_dependent_even_after_restart(tmp_path: Path):
    database, _store, service, handler, context, run = setup_workflow(tmp_path, verified=False)
    result = service.resume(run.run_id, context)
    assert result.nodes[0].state == NodeState.INCONCLUSIVE
    assert result.nodes[1].state == NodeState.BLOCKED
    service.resume(run.run_id, context)
    assert len(handler.calls) == 1
    database.close()


def test_checkpoint_rejects_tampering_scope_change_and_component_change(tmp_path: Path):
    database, store, service, _handler, _context, run = setup_workflow(tmp_path)
    changed = ExecutionContext(principal=Principal("another", "user"))
    with pytest.raises(WorkflowError, match="authority"):
        service.resume(run.run_id, changed)
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE af_workflow_runs SET payload = :payload"), {"payload": "{}"}
        )
    with pytest.raises(WorkflowError, match="authentication"):
        store.get(run.run_id)
    database.close()


def test_cancel_before_admission_has_no_effect_and_records_checkpoint_policy(tmp_path: Path):
    database, _store, service, handler, context, run = setup_workflow(tmp_path)
    cancelled = service.cancel(run.run_id, context)
    assert cancelled.cancelled
    assert cancelled.checkpoint_policy == "retained"
    assert all(node.state == NodeState.CANCELLED for node in cancelled.nodes)
    service.resume(run.run_id, context)
    assert not handler.calls
    database.close()


def test_unknown_eta_is_none_and_observed_eta_is_interval(tmp_path: Path):
    database, _store, service, _handler, context, run = setup_workflow(tmp_path)
    progress = service.progress(run.run_id)
    assert progress.eta_seconds is None
    assert progress.fraction == 0
    service.resume(run.run_id, context, max_nodes=1)
    progress = service.progress(run.run_id)
    assert progress.fraction == 0.5
    assert progress.eta_seconds is not None
    assert progress.eta_seconds[0] <= progress.eta_seconds[1]
    assert progress.assumptions
    database.close()


def test_stale_worker_is_not_replayed_for_non_idempotent_component(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path, replay_safe=False)
    running = run.nodes[0].model_copy(
        update={
            "state": NodeState.RUNNING,
            "attempt": 1,
            "fence": "old-worker",
            "lease_until": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    store.save(run.model_copy(update={"nodes": (running, run.nodes[1])}), expected_revision=0)
    recovered = service.resume(run.run_id, context)
    assert recovered.nodes[0].state == NodeState.NEEDS_HUMAN
    assert recovered.nodes[0].reason == "unproven_effect_after_worker_loss"
    assert not handler.calls
    database.close()


def test_definition_rejects_cycles_and_missing_dependencies():
    with pytest.raises(ValueError, match="acyclic"):
        WorkflowDefinition(
            workflow_id="cycle",
            version="1",
            nodes=(
                WorkflowNode(
                    node_id="one",
                    component_id="append",
                    component_digest="a" * 64,
                    dependencies=(EvidenceRequirement(node_id="two"),),
                ),
                WorkflowNode(
                    node_id="two",
                    component_id="append",
                    component_digest="a" * 64,
                    dependencies=(EvidenceRequirement(node_id="one"),),
                ),
            ),
        )


def test_duplicate_claim_and_stale_revision_cannot_regress_completion(tmp_path: Path):
    from af.framework.workflows import WorkflowConflict

    database, store, service, handler, context, run = setup_workflow(tmp_path)
    done = service.resume(run.run_id, context)
    with pytest.raises(WorkflowConflict):
        store.save(run, expected_revision=run.revision)
    assert store.get(run.run_id) == done
    assert len(handler.calls) == 2
    database.close()


def test_signed_status_without_output_evidence_fails_closed(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    wrong = run.nodes[0].model_copy(update={"state": NodeState.COMPLETED})
    store.save(run.model_copy(update={"nodes": (wrong, run.nodes[1])}), expected_revision=0)
    with pytest.raises(WorkflowError, match="verifier evidence"):
        service.resume(run.run_id, context)
    assert not handler.calls
    database.close()


def test_evidence_invalidation_blocks_completed_dependents_and_keeps_history(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    done = service.resume(run.run_id, context)
    invalid = service.invalidate(run.run_id, "first", context)
    assert all(record.state == NodeState.INCONCLUSIVE for record in invalid.nodes)
    assert done in store.history(run.run_id)
    service.resume(run.run_id, context)
    assert len(handler.calls) == 2
    database.close()


class Resources:
    def __init__(self, admitted=True, valid=True):
        self.admitted = admitted
        self.is_valid = valid
        self.acquired = 0
        self.released = 0

    def acquire(self, context, node, *, effect_key):
        self.acquired += 1
        return "resource-lease" if self.admitted else None

    def valid(self, lease):
        return self.is_valid

    def release(self, lease):
        self.released += 1


def test_resource_contention_is_blocked_and_revoked_lease_executes_nothing(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    resources = Resources(admitted=False)
    gated = WorkflowService(store, service.components, resources=resources)
    blocked = gated.resume(run.run_id, context)
    assert blocked.nodes[0].state == NodeState.BLOCKED
    assert resources.acquired == 1
    assert not handler.calls
    resources.admitted = True
    resources.is_valid = False
    rejected = gated.resume(run.run_id, context)
    assert rejected.nodes[0].reason == "resource_lease_revoked"
    assert resources.released == 1
    assert not handler.calls
    database.close()


def test_cancelled_in_flight_result_is_fenced_and_resource_released(tmp_path: Path):
    database, store, service, _handler, context, run = setup_workflow(tmp_path)
    resources = Resources()

    class Cancelling(Component):
        def execute(self, data, predecessors, child, *, effect_key):
            service.cancel(run.run_id, context)
            return super().execute(data, predecessors, child, effect_key=effect_key)

    component = WorkflowComponent("append", "1", "a" * 64, False, Cancelling())
    worker = WorkflowService(store, (component,), resources=resources)
    result = worker.resume(run.run_id, context)
    assert result.cancelled
    assert all(node.state == NodeState.CANCELLED for node in result.nodes)
    assert all(node.output is None for node in result.nodes)
    assert resources.released == 1
    database.close()


def test_pure_worker_loss_retries_with_stable_effect_key(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    running = run.nodes[0].model_copy(
        update={
            "state": NodeState.RUNNING,
            "attempt": 1,
            "fence": "old-worker",
            "lease_until": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    store.save(run.model_copy(update={"nodes": (running, run.nodes[1])}), expected_revision=0)
    result = service.resume(run.run_id, context)
    assert result.nodes[0].state == NodeState.COMPLETED
    assert result.nodes[0].attempt == 2
    assert handler.calls[0] == f"{run.run_id}:first:" + "a" * 64
    database.close()


def test_live_worker_fence_prevents_second_executor(tmp_path: Path):
    database, store, service, handler, context, run = setup_workflow(tmp_path)
    running = run.nodes[0].model_copy(
        update={
            "state": NodeState.RUNNING,
            "attempt": 1,
            "fence": "worker",
            "lease_until": datetime.now(UTC) + timedelta(minutes=1),
        }
    )
    claimed = store.save(
        run.model_copy(update={"nodes": (running, run.nodes[1])}), expected_revision=0
    )
    assert service.resume(run.run_id, context) == claimed
    assert not handler.calls
    database.close()


def test_inspection_revalidates_checkpoint_authority(tmp_path: Path):
    database, _store, service, _handler, context, run = setup_workflow(tmp_path)
    assert service.inspect(run.run_id, context) == run
    with pytest.raises(WorkflowError, match="authority"):
        service.inspect(run.run_id, ExecutionContext(principal=Principal("intruder", "user")))
    database.close()


def test_live_cancellation_reaches_cooperative_handler_and_releases_lease(tmp_path: Path):
    import threading

    database, store, service, _handler, context, run = setup_workflow(tmp_path)
    started = threading.Event()
    release = threading.Event()
    observed = []
    resources = Resources()

    class Cooperative(Component):
        def execute(self, data, predecessors, child, *, effect_key):
            started.set()
            while not child.cancellation.cancelled and not release.wait(0.005):
                pass
            observed.append(child.cancellation.cancelled)
            return WorkflowOutput(data=b"result")

    worker = WorkflowService(
        store,
        (WorkflowComponent("append", "1", "a" * 64, False, Cooperative()),),
        resources=resources,
    )
    thread = threading.Thread(target=worker.resume, args=(run.run_id, context))
    thread.start()
    try:
        assert started.wait(1)
        service.cancel(run.run_id, context)
        thread.join(timeout=0.5)
        assert not thread.is_alive()
        assert observed == [True]
        assert resources.released == 1
    finally:
        release.set()
        thread.join(timeout=1)
        database.close()


def test_long_stage_heartbeats_its_claim_without_repeating_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from threading import Condition

    database, store, _service, _handler, context, run = setup_workflow(tmp_path)
    heartbeats = Condition()
    heartbeat_count = 0
    lease_by_fence = {}
    original_save = store.save

    def observe_save(updated, *, expected_revision):
        nonlocal heartbeat_count
        active = next(
            (
                node
                for node in updated.nodes
                if node.state == NodeState.RUNNING and node.fence is not None
            ),
            None,
        )
        result = original_save(updated, expected_revision=expected_revision)
        if active is not None and active.lease_until is not None:
            previous = lease_by_fence.get(active.fence)
            lease_by_fence[active.fence] = active.lease_until
            if previous is not None and active.lease_until > previous:
                with heartbeats:
                    heartbeat_count += 1
                    heartbeats.notify_all()
        return result

    monkeypatch.setattr(store, "save", observe_save)

    class Slow(Component):
        def execute(self, data, predecessors, child, *, effect_key):
            with heartbeats:
                target = heartbeat_count + 4
                assert heartbeats.wait_for(lambda: heartbeat_count >= target, timeout=8)
            return super().execute(data, predecessors, child, effect_key=effect_key)

    handler = Slow()
    worker = WorkflowService(
        store,
        (WorkflowComponent("append", "1", "a" * 64, False, handler),),
        claim_lifetime=timedelta(milliseconds=900),
    )
    result = worker.resume(run.run_id, context)
    assert all(node.state == NodeState.COMPLETED for node in result.nodes)
    assert len(handler.calls) == 2
    database.close()


def test_checkpoint_policy_compatibility_payload_and_node_binding_are_enforced(tmp_path):
    from af.application.workflows import LocalWorkflowPolicy

    database, store, service, _handler, context, run = setup_workflow(tmp_path)

    class ChangedPolicy(LocalWorkflowPolicy):
        version = "new-policy"

    with pytest.raises(WorkflowError, match="policy"):
        WorkflowService(store, service.components, policy=ChangedPolicy()).resume(
            run.run_id, context
        )
    variants = [
        (
            {"definition": run.definition.model_copy(update={"compatibility_version": 2})},
            "compatibility",
        ),
        ({"input_digest": "wrong"}, "input digest"),
        ({"nodes": tuple(reversed(run.nodes))}, "nodes"),
    ]
    for update, expected in variants:
        current = store.get(run.run_id)
        changed = run.model_copy(update={**update, "revision": current.revision})
        store.save(changed, expected_revision=current.revision)
        with pytest.raises(WorkflowError, match=expected):
            service.resume(run.run_id, context)
    database.close()


def test_expired_deadline_and_cancelled_token_execute_no_effect(tmp_path):
    from dataclasses import replace

    database, _store, service, handler, context, run = setup_workflow(tmp_path)
    context.cancellation.cancel()
    assert service.resume(run.run_id, context).cancelled
    assert not handler.calls
    with pytest.raises(WorkflowError, match="deadline"):
        service.submit(
            run.definition,
            b"input",
            replace(
                context,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
                cancellation=type(context.cancellation)(),
            ),
        )
    with pytest.raises(ValueError, match="max_nodes"):
        service.resume(run.run_id, context, max_nodes=0)
    with pytest.raises(WorkflowError, match="node not found"):
        service.invalidate(run.run_id, "missing", context)
    database.close()


def test_corrupt_authenticated_checkpoint_schema_and_identity_fail_closed(tmp_path):
    import hashlib
    import hmac

    from af.framework.workflows import WorkflowConflict

    database, store, _service, _handler, _context, run = setup_workflow(tmp_path)
    with pytest.raises(WorkflowConflict):
        store.create(run)
    with pytest.raises(WorkflowError, match="revision zero"):
        store.create(run.model_copy(update={"revision": 1}))
    with pytest.raises(WorkflowError, match="not found"):
        store.get("missing")
    with pytest.raises(ValueError, match="key"):
        SqlAlchemyWorkflowRepository(database, authentication_key=b"short")
    with pytest.raises(WorkflowConflict, match="revision"):
        store.save(run, expected_revision=2)
    payloads = [
        ("{}", "schema"),
        (run.model_copy(update={"run_id": "another"}).model_dump_json(), "identity"),
    ]
    for payload, reason in payloads:
        signature = hmac.new(b"k" * 32, payload.encode(), hashlib.sha256).hexdigest()
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE af_workflow_runs SET payload=:payload, signature=:signature"),
                {"payload": payload, "signature": signature},
            )
        with pytest.raises(WorkflowError, match=reason):
            store.get(run.run_id)
    database.close()


def test_checkpoint_head_rollback_is_rejected_against_append_only_history(tmp_path):
    database, store, service, _handler, context, run = setup_workflow(tmp_path)
    service.resume(run.run_id, context, max_nodes=1)
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE af_workflow_runs SET revision=0"))
    with pytest.raises(WorkflowError, match="rollback"):
        store.get(run.run_id)
    database.close()


def test_concurrent_checkpoint_reads_observe_one_committed_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    database, store, _service, _handler, _context, run = setup_workflow(tmp_path)
    started, completed = Event(), Event()

    def advance():
        current = run
        started.set()
        try:
            for _ in range(60):
                current = store.save(current, expected_revision=current.revision)
        finally:
            completed.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(advance)
            assert started.wait(2)
            revisions = []
            while not completed.is_set():
                revisions.append(store.get(run.run_id).revision)
            writer.result(5)
        assert revisions == sorted(revisions)
        assert store.get(run.run_id).revision == 60
    finally:
        database.close()
