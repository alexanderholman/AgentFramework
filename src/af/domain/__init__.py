"""AF reference-domain capabilities."""

from af.domain.feature_flags import (
    EvaluationContext,
    FeatureFlag,
    FeatureFlagEvaluator,
    FeatureFlagManager,
    FeatureFlagState,
    FeatureFlagStore,
    RolloutStrategy,
)

__all__ = [
    "EvaluationContext",
    "FeatureFlag",
    "FeatureFlagEvaluator",
    "FeatureFlagManager",
    "FeatureFlagState",
    "FeatureFlagStore",
    "RolloutStrategy",
]
