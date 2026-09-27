"""Compose installed deterministic classifiers into the shared typed hierarchy."""

from __future__ import annotations

from af.domain.classifiers import CharacterCountClassifier, HelloClassifier, TimeClassifier
from af.framework.classification_hierarchy import (
    ChildRoute,
    ClassifierFailure,
    ClassifierNode,
    FunctionRoute,
    HierarchyRouter,
    NodeInput,
    NoRoute,
    RouteDecision,
)
from af.framework.contracts import Classifier
from af.framework.procedure_contributors import InstalledProcedureContribution
from af.framework.routing import ProcedureRegistry
from af.framework.types import Classification, ProcedureDescriptor, SelectionProposal, WorkRequest


class InstalledClassifierNode:
    """Translate existing classifier output; it never executes a procedure."""

    def __init__(self, classifier: Classifier, descriptor: ProcedureDescriptor) -> None:
        self._classifier, self._descriptor = classifier, descriptor

    def __call__(self, request: NodeInput) -> RouteDecision:
        classification = self._classifier.classify(WorkRequest(request.prompt))
        if classification is None:
            return NoRoute()
        if type(classification) is not Classification:
            return ClassifierFailure("malformed")
        if classification.intent == "unknown":
            return NoRoute()
        return FunctionRoute(
            SelectionProposal(
                procedure_id=self._descriptor.procedure_id,
                intent=classification.intent,
                arguments=classification.arguments,
                confidence=classification.confidence,
                version=self._descriptor.version,
                digest=self._descriptor.digest,
            )
        )


def default_classifier_hierarchy(
    registry: ProcedureRegistry,
    contributions: tuple[InstalledProcedureContribution, ...],
) -> HierarchyRouter:
    nodes = [
        ClassifierNode(
            "root", lambda _: ChildRoute("system"), children=("system", "text", "extensions")
        ),
        ClassifierNode("system", lambda _: ChildRoute("hello"), children=("hello", "date-time")),
    ]

    def install(identity: str, procedure_id: str, classifier: Classifier) -> None:
        descriptor = registry.descriptor(procedure_id)
        if descriptor is None:
            raise ValueError("classifier procedure is not registered")
        nodes.append(
            ClassifierNode(
                identity,
                InstalledClassifierNode(classifier, descriptor),
                procedures=(descriptor,),
                version=descriptor.version,
            )
        )

    install("hello", "hello", HelloClassifier())
    install("date-time", "current-time", TimeClassifier())
    install("text", "character-count", CharacterCountClassifier())
    children = tuple(f"extension.{item.procedure.id}" for item in contributions)
    nodes.append(
        ClassifierNode(
            "extensions",
            lambda _: ChildRoute(children[0]) if children else NoRoute(),
            children=children,
        )
    )
    for identity, contribution in zip(children, contributions, strict=True):
        install(identity, contribution.procedure.id, contribution.classifier)
    return HierarchyRouter(tuple(nodes), roots=("root",))
