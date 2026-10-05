from af.domain.ideons.minimal import Target, search_minimal_languages

def test_minimal_search_finds_small_compositional_core() -> None:
    targets = (
        Target.from_parts("give", ("CHANGE", "RELATE")),
        Target.from_parts("receive", ("CHANGE", "RELATE")),
        Target.from_parts("not", ("NOT",)),
        Target.from_parts("other", ("OTHER",)),
    )
    result = search_minimal_languages(
        ("CHANGE", "RELATE", "NOT", "OTHER", "GIVE", "RECEIVE"),
        targets,
    )
    assert result
    best = result[0]
    assert best.primes == ("CHANGE", "NOT", "OTHER", "RELATE")
    assert best.expression_cost == 6
