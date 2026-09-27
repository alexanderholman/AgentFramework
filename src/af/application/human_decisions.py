"""A finite reference clarification journey over installed deterministic procedures."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from af.framework.context import ExecutionContext, derive_context, use_context
from af.framework.contracts import Agent, Classifier
from af.framework.errors import AFError
from af.framework.human_decisions import (
    HumanChoice,
    HumanDecisionError,
    HumanDecisionKind,
    HumanDecisionRepository,
    PendingHumanDecision,
)
from af.framework.routing import LadderRouter, ProcedureRegistry
from af.framework.types import (
    Classification,
    ProcedureEffect,
    ProcedureRisk,
    WorkRequest,
    normalize_prompt,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _authority(context: ExecutionContext) -> str:
    return _digest(
        json.dumps(
            {
                "principal": asdict(context.principal) if context.principal else None,
                "tenant": context.tenant_id,
                "scope": context.effective_scope,
            },
            sort_keys=True,
        )
    )


class HumanDecisionService:
    """Choices come from the installed classifier/catalogue, never client-supplied code.

    The reference ambiguous form is ``count characters in \"text\"``. It asks which
    character to count. ``confirm: <deterministic request>`` explicitly requests a
    pause. This finite grammar does not claim universal natural-language understanding.
    """

    def __init__(
        self,
        repository: HumanDecisionRepository,
        agent: Agent,
        classifier: Classifier,
        registry: ProcedureRegistry,
        router: LadderRouter,
        *,
        policy_digest: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository, self._agent, self._classifier = repository, agent, classifier
        self._registry, self._router, self._policy, self._clock = (
            registry,
            router,
            policy_digest,
            clock,
        )

    def _active(self, context: ExecutionContext) -> None:
        if context.cancellation.cancelled or (
            context.deadline is not None and context.deadline.timestamp() <= self._clock()
        ):
            raise HumanDecisionError()

    def _choice(
        self,
        classification: Classification,
        label: str,
        choice_id: str,
        execution_prompt: str = "Human-authorized character count",
    ) -> HumanChoice:
        procedure = self._router.route(classification, prompt="human decision")
        descriptor = self._registry.descriptor(procedure.id)
        if descriptor is None or (
            not descriptor.pure
            or not descriptor.replay_safe
            or descriptor.effect is not ProcedureEffect.READ_ONLY
            or descriptor.risk is not ProcedureRisk.NONE
            or descriptor.requires_confirmation
        ):
            raise HumanDecisionError()
        procedure.validate(classification)
        return HumanChoice(
            choice_id=choice_id,
            label=label,
            consequence="Run this installed local pure procedure once.",
            procedure_id=procedure.id,
            procedure_digest=descriptor.digest,
            intent=classification.intent,
            execution_prompt=execution_prompt,
            arguments=dict(classification.arguments),
        )

    def submit(
        self, prompt: str, context: ExecutionContext, *, expires_in: int = 300
    ) -> dict[str, object]:
        self._active(context)
        prompt = normalize_prompt(prompt)
        if type(expires_in) is not int or not 1 <= expires_in <= 3600:
            raise HumanDecisionError()
        ambiguous = re.fullmatch(r'count characters in "([^"\r\n]{1,4096})"', prompt, re.I)
        choices: list[HumanChoice] = []
        kind = HumanDecisionKind.CONFIRMATION
        try:
            if ambiguous:
                kind = HumanDecisionKind.CLARIFICATION
                text = ambiguous.group(1)
                characters = sorted(set(text.casefold()))
                if len(characters) > 16 or any(not c.isprintable() for c in characters):
                    return {"kind": HumanDecisionKind.DENIED.value, "reason": "choice_limit"}
                for index, character in enumerate(characters):
                    choices.append(
                        self._choice(
                            Classification(
                                "text.character_count",
                                {"text": text, "character": character},
                                1.0,
                                "human-clarification",
                            ),
                            f"Count {character!r}",
                            str(index),
                        )
                    )
            else:
                if not prompt.startswith("confirm: "):
                    return {
                        "kind": HumanDecisionKind.NO_ROUTE.value,
                        "reason": "unsupported_decision_form",
                    }
                classification = self._classifier.classify(WorkRequest(prompt[9:]))
                if classification is None or classification.intent == "unknown":
                    return {
                        "kind": HumanDecisionKind.NO_ROUTE.value,
                        "reason": "no_installed_route",
                    }
                choices.append(
                    self._choice(
                        classification, "Confirm this exact operation", "confirm", prompt[9:]
                    )
                )
        except AFError:
            return {"kind": HumanDecisionKind.DENIED.value, "reason": "procedure_policy"}
        now = self._clock()
        secret = secrets.token_urlsafe(32)
        expires = now + expires_in
        if context.deadline is not None:
            expires = min(expires, context.deadline.timestamp())
        decision = PendingHumanDecision(
            decision_id=uuid4().hex,
            kind=kind,
            request_digest=_digest(prompt),
            authority_digest=_authority(context),
            policy_digest=self._policy,
            response_digest=_digest(secret),
            choices=tuple(choices),
            created_at=now,
            expires_at=expires,
        )
        self._validate_active_decision(decision, context)
        self._repository.create(decision)
        return {**self._public(decision), "response_secret": secret}

    @staticmethod
    def _public(decision: PendingHumanDecision) -> dict[str, object]:
        return {
            "kind": decision.kind.value,
            "decision_id": decision.decision_id,
            "request_digest": decision.request_digest,
            "expires_at": decision.expires_at,
            "choices": [
                {
                    "choice_id": c.choice_id,
                    "label": c.label,
                    "consequence": c.consequence,
                    "procedure_id": c.procedure_id,
                    "procedure_digest": c.procedure_digest,
                }
                for c in decision.choices
            ],
        }

    def _pending(self, decision_id: str, context: ExecutionContext) -> PendingHumanDecision:
        self._active(context)
        decision = self._repository.get(decision_id)
        self._validate_active_decision(decision, context)
        return decision

    def _validate_active_decision(
        self, decision: PendingHumanDecision, context: ExecutionContext
    ) -> None:
        self._active(context)
        if (
            decision.expires_at <= self._clock()
            or decision.created_at > self._clock()
            or not hmac.compare_digest(decision.authority_digest, _authority(context))
            or decision.policy_digest != self._policy
        ):
            raise HumanDecisionError()

    def inspect(self, decision_id: str, context: ExecutionContext) -> dict[str, object]:
        return self._public(self._pending(decision_id, context))

    def revoke(self, decision_id: str, context: ExecutionContext) -> dict[str, object]:
        self._repository.revoke(self._pending(decision_id, context))
        return {"kind": HumanDecisionKind.DENIED.value, "reason": "revoked"}

    def resume(
        self, decision_id: str, response_secret: str, choice_id: str, context: ExecutionContext
    ) -> dict[str, object]:
        decision = self._pending(decision_id, context)
        if (
            type(response_secret) is not str
            or len(response_secret) > 128
            or not response_secret.isascii()
            or not hmac.compare_digest(decision.response_digest, _digest(response_secret))
        ):
            raise HumanDecisionError()
        choice = next((c for c in decision.choices if c.choice_id == choice_id), None)
        if choice is None:
            raise HumanDecisionError()
        classification = Classification(
            choice.intent, dict(choice.arguments), 1.0, "human-response"
        )
        current = self._choice(
            classification, choice.label, choice.choice_id, choice.execution_prompt
        )
        if current != choice:
            raise HumanDecisionError()
        self._validate_active_decision(decision, context)
        self._repository.claim(decision)
        # Claim before execution: a crash cannot replay an accepted response. The
        # inspecting operator sees an unavailable decision and must make a new request.
        try:
            self._validate_active_decision(decision, context)
            expires = datetime.fromtimestamp(decision.expires_at, UTC)
            deadline = min(context.deadline, expires) if context.deadline else expires
            execution_context = derive_context(context, deadline=deadline)
            with use_context(execution_context):
                outcome = self._agent.handle_classification(
                    WorkRequest(prompt=choice.execution_prompt),
                    classification,
                    expected_procedure_id=choice.procedure_id,
                )
            self._validate_active_decision(decision, context)
            if (
                not outcome.verified
                or outcome.status != "completed"
                or outcome.route != choice.procedure_id
                or outcome.intent != choice.intent
            ):
                raise HumanDecisionError()
        except BaseException:
            self._repository.finish(decision_id, "inconclusive")
            raise
        self._repository.finish(decision_id, "completed")
        return {"kind": HumanDecisionKind.COMPLETED.value, "result": asdict(outcome)}
