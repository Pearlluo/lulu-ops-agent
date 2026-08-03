"""Entity resolution smoke tests. The resolver builds its name dictionary
from Gold parquet, so these only run where the lake exists."""
from tests.conftest import requires_gold


@requires_gold
def test_fuzzy_client_match():
    from entity_resolver import resolve

    r = resolve("roy hil")
    assert r["status"] in ("fuzzy", "exact")
    assert r["match"]["value"] == "Ironbridge"


@requires_gold
def test_exact_match_scores_high():
    from entity_resolver import resolve

    r = resolve("Ironbridge")
    assert r["status"] in ("fuzzy", "exact")
    assert r["match"]["score"] >= 95
