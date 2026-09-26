"""Read-only exhaustion of the actually configured deterministic routing catalogue."""

from __future__ import annotations

from collections.abc import Callable

from af.framework.context import ExecutionContext
from af.framework.contracts import Classifier
from af.framework.engineering import EngineeringRequest, EngineeringRouteEvidence
from af.framework.routing import ProcedureRegistry
from af.framework.types import WorkRequest


class InstalledEngineeringRouteProbe:
    """Never execute a selected procedure or turn an unavailable selector into NoRoute.

    Resolver callbacks keep the probe lazy so the admitted contribution may itself
    depend on the engineering service during application composition.
    """

    def __init__(
        self,
        classifier: Callable[[], Classifier],
        registry: Callable[[], ProcedureRegistry],
        *,
        selector_configured: bool = False,
    ) -> None:
        self._classifier = classifier
        self._registry = registry
        self._selector_configured = selector_configured

    def probe(
        self, request: EngineeringRequest, context: ExecutionContext
    ) -> EngineeringRouteEvidence:
        if context.cancellation.cancelled:
            return EngineeringRouteEvidence(status="cancelled", history=("context:cancelled",))
        classifier, registry = self._classifier(), self._registry()
        history: list[str] = []
        for index, text in enumerate(request.texts):
            classification = classifier.classify(WorkRequest("count nonblank lines:" + text))
            if classification is not None:
                candidates = tuple(
                    procedure for procedure in registry.all() if procedure.supports(classification)
                )
                if candidates:
                    return EngineeringRouteEvidence(
                        status="available", history=(f"item:{index}:existing-installed-route",)
                    )
                # Non-unknown classification with no installed implementation is misconfiguration.
                if classification.intent != "unknown":
                    return EngineeringRouteEvidence(
                        status="unavailable",
                        history=(f"item:{index}:classified-procedure-unavailable",),
                    )
            if self._selector_configured:
                return EngineeringRouteEvidence(
                    status="unavailable",
                    history=("configured-selector-requires-independent-exhaustion",),
                )
            history.append(
                f"item:{index}:deterministic-classifier-and-installed-catalogue-exhausted"
            )
        return EngineeringRouteEvidence(status="no-route-exhausted", history=tuple(history))
