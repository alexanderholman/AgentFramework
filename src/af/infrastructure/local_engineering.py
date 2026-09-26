"""Real finite local engineering: worktree, test-first gates, review and execution.

Only the bundled, reviewed nonblank-line-count template is supported. Arbitrary
model-provided source or shell commands are not inputs to this adapter.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from af.application.engineering import LocalEngineeringService
from af.domain.workflow_components import CountTotal, CountVector, SumCounts
from af.framework.context import ExecutionContext
from af.framework.engineering import (
    FAMILY,
    ComponentAdmission,
    EngineeringGate,
    EngineeringRouteProbe,
)
from af.framework.programs import (
    ProgramBudget,
    ProgramExecutor,
    ProgramRequest,
    ProgramResult,
    ProgramState,
)
from af.framework.version_control import VersionControlRequest
from af.framework.workflows import (
    EvidenceRequirement,
    WorkflowComponent,
    WorkflowDefinition,
    WorkflowError,
    WorkflowNode,
    WorkflowOutput,
)
from af.infrastructure.database import Database
from af.infrastructure.engineering_catalogue import SqlAlchemyEngineeringCatalogue
from af.infrastructure.git_version_control import GitVersionControlBackend
from af.infrastructure.programs import AsyncSubprocessExecutor
from af.infrastructure.workflows import SqlAlchemyWorkflowRepository

_TEMPLATE = '''"""Bounded Unicode nonblank LF-line count, generated from reviewed AF template v1."""

from __future__ import annotations

import json
import sys


def count_lines(text: str) -> int:
    result = 0
    for line in text.split("\\n"):
        if any(not character.isspace() for character in line):
            result += 1
    return result


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError("one bounded input file is required")
    with open(sys.argv[1], "rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("input is too large")
    payload: object = json.loads(data)
    if not isinstance(payload, dict) or set(payload) != {"texts"}:
        raise ValueError("expected texts")
    texts = payload["texts"]
    if not isinstance(texts, list) or not 1 <= len(texts) <= 128:
        raise ValueError("expected bounded text batch")
    if any(not isinstance(text, str) for text in texts):
        raise ValueError("texts must be strings")
    print(json.dumps({"counts": [count_lines(str(text)) for text in texts]}))


if __name__ == "__main__":
    main()
'''

_TESTS = '''"""Independent examples and adversarial contract cases before implementation."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("component.py")


class GeneratedContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("candidate", MODULE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_examples(self):
        cases = [("", 0), ("one", 1), ("one\\n\\ntwo\\n", 2),
                 (" \\n\\t\\n", 0), ("雪\\n川", 2), ("\\r\\n", 0)]
        for text, expected in cases:
            self.assertEqual(self.module.count_lines(text), expected)

    def test_parameterized_independent_reference(self):
        for size in range(1, 30):
            text = "\\n".join("entry" if index % 3 else "  " for index in range(size))
            self.assertEqual(self.module.count_lines(text), size - (size + 2) // 3)

    def test_external_json_contract_and_hostile_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "input.json"
            data.write_text(json.dumps({"texts": ["one\\ntwo", "", "three"]}))
            result = subprocess.run([sys.executable, "-I", str(MODULE), str(data)],
                                    capture_output=True, check=True, timeout=3)
            self.assertEqual(json.loads(result.stdout), {"counts": [2, 0, 1]})
            data.write_text(json.dumps({"texts": ["good", 42]}))
            result = subprocess.run([sys.executable, "-I", str(MODULE), str(data)],
                                    capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)

    def test_main_contract_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "input.json"
            with patch.object(sys, "argv", [str(MODULE)]):
                with self.assertRaises(ValueError):
                    self.module.main()
            with patch.object(sys, "argv", [str(MODULE), str(data)]):
                for payload in [[], {}, {"texts": "string"}, {"texts": []},
                                {"texts": ["x"] * 129}, {"texts": [42]}]:
                    data.write_text(json.dumps(payload))
                    with self.assertRaises(ValueError):
                        self.module.main()
                data.write_bytes(b"x" * 65537)
                with self.assertRaises(ValueError):
                    self.module.main()
                data.write_text(json.dumps({"texts": ["one\\ntwo", ""]}))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.module.main()
                self.assertEqual(json.loads(output.getvalue()), {"counts": [2, 0]})


if __name__ == "__main__":
    unittest.main()
'''
_TEMPLATE_DIGEST = hashlib.sha256(_TEMPLATE.encode()).hexdigest()


class EngineeringReviewer(Protocol):
    @property
    def identity(self) -> str: ...
    def review(self, source: bytes, gates: tuple[EngineeringGate, ...]) -> bool: ...


class FiniteTemplateReviewer:
    identity = "af.independent-finite-template-review.v1"

    def review(self, source: bytes, gates: tuple[EngineeringGate, ...]) -> bool:
        if hashlib.sha256(source).hexdigest() != _TEMPLATE_DIGEST:
            return False
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if imports != {"json", "sys"}:
            return False
        forbidden = {"eval", "exec", "compile", "__import__"}
        if any(isinstance(node, ast.Name) and node.id in forbidden for node in ast.walk(tree)):
            return False
        return {gate.name for gate in gates} == {
            "test-first",
            "tests",
            "lint",
            "format",
            "typing",
            "coverage",
            "security",
            "compatibility",
            "governance",
        } and all(gate.passed for gate in gates)


def _execute(
    executor: ProgramExecutor,
    argv: tuple[str, ...],
    cwd: Path,
    context: ExecutionContext,
    *,
    seconds: float = 30,
) -> ProgramResult:
    if context.cancellation.cancelled:
        raise WorkflowError("engineering_cancelled")
    if context.deadline is not None:
        seconds = min(seconds, (context.deadline - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        raise WorkflowError("engineering_deadline_expired")

    async def run() -> ProgramResult:
        process = asyncio.create_task(
            executor.execute(
                ProgramRequest(
                    argv=argv,
                    working_directory=cwd,
                    budget=ProgramBudget(
                        timeout=timedelta(seconds=seconds), max_output_bytes=65_536
                    ),
                )
            )
        )
        cancelled = asyncio.create_task(context.cancellation.wait())
        try:
            done, _ = await asyncio.wait({process, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                process.cancel()
                await asyncio.gather(process, return_exceptions=True)
                raise WorkflowError("engineering_cancelled")
            return await process
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    # Installed procedures have a synchronous contract and can run inside the
    # application's async bus. The explicit context crosses this bounded bridge.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="af-engineering-program") as pool:
        return pool.submit(asyncio.run, run()).result()


class PinnedLineCounter:
    def __init__(self, admission: ComponentAdmission, executor: ProgramExecutor) -> None:
        self._admission = admission
        self._executor = executor

    def source(self) -> bytes:
        path = Path(self._admission.artifact_path)
        try:
            if path.is_symlink() or self._admission.component_digest != _TEMPLATE_DIGEST:
                raise WorkflowError("admitted_component_artifact_changed")
            with path.open("rb") as stream:
                source = stream.read(65_537)
        except OSError as error:
            raise WorkflowError("admitted_component_artifact_unavailable") from error
        if hashlib.sha256(source).hexdigest() != self._admission.component_digest:
            raise WorkflowError("admitted_component_artifact_changed")
        return source

    def execute(
        self,
        data: bytes,
        predecessors: Mapping[str, bytes],
        context: ExecutionContext,
        *,
        effect_key: str,
    ) -> WorkflowOutput:
        del effect_key
        if predecessors:
            raise WorkflowError("line_counter_requires_root_input")
        source = self.source()
        with tempfile.TemporaryDirectory(prefix="af-component-input-") as directory:
            # Execute the checked immutable bytes, closing the artifact substitution race.
            path = Path(directory) / "component.py"
            path.write_bytes(source)
            input_path = Path(directory) / "input.json"
            descriptor = os.open(input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            result = _execute(
                self._executor,
                (sys.executable, "-I", str(path), str(input_path)),
                Path(self._admission.worktree),
                context,
                seconds=5,
            )
        if result.state != ProgramState.COMPLETED:
            raise WorkflowError("admitted_component_execution_failed")
        vector = CountVector.model_validate_json(result.stdout)
        return WorkflowOutput(data=vector.model_dump_json().encode())

    def verify(
        self, data: bytes, predecessors: Mapping[str, bytes], output: WorkflowOutput
    ) -> bool:
        del predecessors
        payload = json.loads(data)
        expected = [
            sum(bool(line.strip()) for line in text.split("\n")) for text in payload["texts"]
        ]
        return list(CountVector.model_validate_json(output.data).counts) == expected


class GitTemplateEngineer:
    def __init__(
        self,
        repository: Path,
        executor: ProgramExecutor,
        reviewer: EngineeringReviewer | None = None,
    ) -> None:
        self._repository = repository.resolve()
        self._executor = executor
        self._reviewer = reviewer or FiniteTemplateReviewer()

    def _git(self, cwd: Path, context: ExecutionContext, *arguments: str) -> str:
        result = _execute(
            self._executor,
            (
                "/usr/bin/env",
                "-i",
                f"PATH={os.defpath}",
                "GIT_CONFIG_NOSYSTEM=1",
                f"GIT_CONFIG_GLOBAL={os.devnull}",
                f"GIT_CONFIG_SYSTEM={os.devnull}",
                "GIT_ALLOW_PROTOCOL=file",
                "GIT_TERMINAL_PROMPT=0",
                "GIT_PROTOCOL_FROM_USER=0",
                "LC_ALL=C",
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=AF Local Engineering",
                "-c",
                "user.email=af@localhost",
                *arguments,
            ),
            cwd,
            context,
            seconds=20,
        )
        if result.state != ProgramState.COMPLETED:
            raise WorkflowError("local_engineering_git_failed")
        return result.stdout.decode().strip()

    def _gate(
        self,
        name: str,
        argv: tuple[str, ...],
        cwd: Path,
        context: ExecutionContext,
        *,
        expect_failure: bool = False,
    ) -> EngineeringGate:
        result = _execute(self._executor, argv, cwd, context)
        passed = result.state == ProgramState.COMPLETED
        if expect_failure:
            passed = (
                result.state == ProgramState.FAILED
                and result.exit_code in {1, 5}
                and b"FileNotFoundError" in result.stderr
            )
        evidence = json.dumps(
            {
                "name": name,
                "state": result.state.value,
                "exit_code": result.exit_code,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace"),
            },
            sort_keys=True,
        ).encode()
        (cwd / "_af_engineering" / f"{name}.json").write_bytes(evidence)
        if not passed:
            raise WorkflowError(f"engineering_gate_failed:{name}")
        return EngineeringGate(
            name=name,
            passed=True,
            evidence_digest=hashlib.sha256(evidence).hexdigest(),
            elapsed_seconds=result.elapsed.total_seconds(),
        )

    @staticmethod
    def _record_gate(name: str, passed: bool, evidence: object, directory: Path) -> EngineeringGate:
        encoded = json.dumps(evidence, sort_keys=True).encode()
        (directory / f"{name}.json").write_bytes(encoded)
        if not passed:
            raise WorkflowError(f"engineering_gate_failed:{name}")
        return EngineeringGate(
            name=name,
            passed=True,
            evidence_digest=hashlib.sha256(encoded).hexdigest(),
            elapsed_seconds=0,
        )

    def engineer(
        self, family: str, context: ExecutionContext, *, route_history: tuple[str, ...]
    ) -> ComponentAdmission:
        if family != FAMILY:
            raise WorkflowError("unsupported_engineering_family")
        coordination = GitVersionControlBackend().initialize(
            VersionControlRequest(cwd=self._repository)
        )
        manifest = coordination.manifest
        head = manifest.repository.head_sha
        identity = hashlib.sha256(f"{family}:{_TEMPLATE_DIGEST}".encode()).hexdigest()[:16]
        worktree = Path(manifest.coordination.worktree_root) / f"engineer-{identity}"
        branch = f"af/engineer-{identity}"
        if worktree.exists():
            raise WorkflowError("engineering_worktree_requires_recovery")
        self._git(
            Path(manifest.coordination.repository_path),
            context,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            head,
        )
        directory = worktree / "_af_engineering"
        directory.mkdir(mode=0o700)
        specification = {
            "lower_route_history": route_history,
            "family": FAMILY,
            "objective": "Count nonblank LF-separated Unicode lines across a bounded text batch",
            "missing_component": "text.nonblank-line-count-batch",
            "reused_component": "number.sum-counts@1.0.0",
            "gap": {
                "request_family": FAMILY,
                "input_schema": "text.line-batch.v1",
                "required_output_schema": "number.scalar.v1",
                "reachable_schemas": ["text.line-batch.v1"],
                "reason": "no_approved_typed_path",
                "missing": "text to number-vector transform",
            },
            "inputs": {"texts": "1..128 strings; combined UTF-8 <=60000 bytes"},
            "outputs": "Nonnegative integer total, independently checked",
            "invariants": ["LF separator", "Unicode whitespace-only line is blank", "pure result"],
            "non_goals": ["arbitrary generated source", "remote execution", "public publication"],
            "effects": [
                "local isolated coordination worktree",
                "private catalogue and workflow state",
            ],
            "egress": "none",
            "threats": [
                "artifact substitution",
                "stale admission",
                "replayed promotion",
                "input overflow",
            ],
            "mitigations": [
                "template and manifest digests",
                "HMAC catalogue",
                "bounded argv subprocess",
            ],
            "cancellation": "terminate process group and reject unadmitted candidate",
            "rollback": "atomic route invalidation; historical admission and worktree retained",
            "compatibility": "stdlib only, Python >=3.12; no application database schema changes",
            "provenance": "Bundled AF finite source template; repository copyright applies",
            "correction_owner": "configured local repository operator",
            "definition_of_done": "all gates pass; atomic admission; verified execution",
            "source_head": head,
        }
        (directory / "specification.json").write_text(json.dumps(specification, sort_keys=True))
        tests = directory / "test_component.py"
        tests.write_text(_TESTS)
        gates = [
            self._gate(
                "test-first",
                (sys.executable, "-I", str(tests)),
                worktree,
                context,
                expect_failure=True,
            )
        ]
        source = directory / "component.py"
        source.write_text(_TEMPLATE)
        coverage_data = str(directory / ".coverage-candidate")
        gates.append(
            self._gate(
                "tests",
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "--rcfile",
                    os.devnull,
                    "--data-file",
                    coverage_data,
                    "--include",
                    str(source),
                    str(tests),
                ),
                worktree,
                context,
            )
        )
        gates.append(
            self._gate(
                "coverage",
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "coverage",
                    "report",
                    "--rcfile",
                    os.devnull,
                    "--data-file",
                    coverage_data,
                    "--include",
                    str(source),
                    "--fail-under=90",
                ),
                worktree,
                context,
            )
        )
        gates.append(
            self._gate(
                "format",
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "ruff",
                    "format",
                    "--check",
                    "--isolated",
                    str(source),
                ),
                worktree,
                context,
            )
        )
        gates.append(
            self._gate(
                "lint",
                (sys.executable, "-I", "-m", "ruff", "check", "--isolated", str(source)),
                worktree,
                context,
            )
        )
        gates.append(
            self._gate(
                "typing",
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "mypy",
                    "--strict",
                    "--config-file",
                    os.devnull,
                    "--cache-dir",
                    str(directory / ".type-cache"),
                    str(source),
                ),
                worktree,
                context,
            )
        )
        gates.append(
            self._record_gate(
                "security",
                source.read_text() == _TEMPLATE,
                {
                    "digest": _TEMPLATE_DIGEST,
                    "imports": ["json", "sys"],
                    "no_network": True,
                    "arbitrary_source_allowed": False,
                    "runtime_input_file_is_private": True,
                },
                directory,
            )
        )
        sample = directory / "compatibility-input.json"
        sample.write_text(json.dumps({"texts": ["one\n\ntwo", "three"]}))
        integration = _execute(
            self._executor,
            (sys.executable, "-I", str(source), str(sample)),
            worktree,
            context,
            seconds=5,
        )
        compatible = integration.state == ProgramState.COMPLETED and sys.version_info >= (3, 12)
        try:
            vector = CountVector.model_validate_json(integration.stdout)
            reused = SumCounts()
            encoded = vector.model_dump_json().encode()
            total = reused.execute(encoded, {}, context, effect_key="candidate-compatibility")
            compatible = (
                compatible
                and reused.verify(encoded, {}, total)
                and CountTotal.model_validate_json(total.data).total == 3
            )
        except ValueError:
            compatible = False
        gates.append(
            self._record_gate(
                "compatibility",
                compatible,
                {
                    "python": list(sys.version_info[:3]),
                    "dependencies": "stdlib",
                    "input_schema": "text.line-batch.v1",
                    "output_schema": "number.vector.v1",
                    "aggregate": "number.sum-counts@1.0.0",
                    "candidate_composite_test": "independent expected total 3",
                    "candidate_output_sha256": hashlib.sha256(integration.stdout).hexdigest(),
                },
                directory,
            )
        )
        unchanged = self._git(self._repository, context, "rev-parse", "HEAD") == head
        gates.append(
            self._record_gate(
                "governance",
                unchanged,
                {
                    "source_head_unchanged": unchanged,
                    "worktree": str(worktree),
                    "branch": branch,
                    "scope": "only _af_engineering artifacts",
                    "provenance": "af bundled finite template v1",
                    "rollback": "invalidate signed route and retain evidence",
                    "specification_sha256": hashlib.sha256(
                        (directory / "specification.json").read_bytes()
                    ).hexdigest(),
                },
                directory,
            )
        )
        started = time.monotonic()
        review_passed = self._reviewer.review(source.read_bytes(), tuple(gates))
        review_evidence = json.dumps(
            {
                "reviewer": self._reviewer.identity,
                "passed": review_passed,
                "source_sha256": _TEMPLATE_DIGEST,
                "gate_digests": {gate.name: gate.evidence_digest for gate in gates},
            },
            sort_keys=True,
        ).encode()
        (directory / "independent-review.json").write_bytes(review_evidence)
        if not review_passed:
            raise WorkflowError("independent_engineering_review_failed")
        gates.append(
            EngineeringGate(
                name="independent-review",
                passed=True,
                evidence_digest=hashlib.sha256(review_evidence).hexdigest(),
                elapsed_seconds=time.monotonic() - started,
            )
        )
        if self._git(self._repository, context, "rev-parse", "HEAD") != head:
            raise WorkflowError("engineering_source_head_changed")
        self._git(
            worktree,
            context,
            "add",
            "--",
            str(source),
            str(tests),
            str(directory / "specification.json"),
            *(str(directory / f"{gate.name}.json") for gate in gates),
        )
        self._git(
            worktree, context, "commit", "-m", "Engineer bounded nonblank line-count capability"
        )
        candidate_head = self._git(worktree, context, "rev-parse", "HEAD")
        return ComponentAdmission(
            component_digest=_TEMPLATE_DIGEST,
            artifact_path=str(source),
            worktree=str(worktree),
            branch=branch,
            source_head=head,
            candidate_head=candidate_head,
            gates=tuple(gates),
            reviewer=self._reviewer.identity,
            created_at=datetime.now(UTC),
            definition=WorkflowDefinition(
                workflow_id=FAMILY,
                version="1",
                nodes=(
                    WorkflowNode(
                        node_id="lines",
                        component_id="text.nonblank-line-count-batch",
                        component_digest=_TEMPLATE_DIGEST,
                    ),
                    WorkflowNode(
                        node_id="total",
                        component_id="number.sum-counts",
                        component_digest=hashlib.sha256(
                            b"af.number.sum-counts.v1:CountVector:CountTotal"
                        ).hexdigest(),
                        dependencies=(EvidenceRequirement(node_id="lines", minimum_bytes=1),),
                    ),
                ),
            ),
            lower_route_history=route_history,
        )

    def component(self, admission: ComponentAdmission) -> WorkflowComponent:
        executor = PinnedLineCounter(admission, self._executor)
        executor.source()
        return WorkflowComponent(
            admission.component_id,
            admission.version,
            admission.component_digest,
            True,
            executor,
        )


def create_local_engineering_service(
    database: Database,
    repository: Path,
    *,
    authentication_key: bytes,
    reviewer: EngineeringReviewer | None = None,
    route_probe: EngineeringRouteProbe | None = None,
) -> LocalEngineeringService:
    executor = AsyncSubprocessExecutor()
    aggregate = WorkflowComponent(
        "number.sum-counts",
        "1.0.0",
        hashlib.sha256(b"af.number.sum-counts.v1:CountVector:CountTotal").hexdigest(),
        True,
        SumCounts(),
    )
    return LocalEngineeringService(
        SqlAlchemyEngineeringCatalogue(database, authentication_key=authentication_key),
        GitTemplateEngineer(repository, executor, reviewer),
        SqlAlchemyWorkflowRepository(database, authentication_key=authentication_key),
        aggregate,
        route_probe=route_probe,
    )
