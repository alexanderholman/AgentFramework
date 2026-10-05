import pytest
from af.domain.ideons.minimal import Decomposition, Target, search_minimal_languages, validate_targets

def test_minimal_search_finds_small_compositional_core() -> None:
    targets=(
      Target("give",(Decomposition(frozenset(("CHANGE","RELATE")),frozenset(("PERSPECTIVE",))),)),
      Target("receive",(Decomposition(frozenset(("CHANGE","RELATE")),frozenset(("PERSPECTIVE",))),)),
      Target.from_parts("not",("NOT",)), Target.from_parts("other",("OTHER",)),
    )
    result=search_minimal_languages(("CHANGE","RELATE","NOT","OTHER","GIVE","RECEIVE"),targets,candidate_operators=("PERSPECTIVE",))
    assert result
    best=result[0]
    assert best.primes==("CHANGE","NOT","OTHER","RELATE")
    assert best.operators==("PERSPECTIVE",)
    assert best.expression_cost==8

def test_target_validation_rejects_hidden_semantics() -> None:
    targets=(Target.from_parts("sky",("SPACE","ABOVE")),)
    with pytest.raises(ValueError,match="undeclared semantics"):
        validate_targets(targets,("SPACE",))
