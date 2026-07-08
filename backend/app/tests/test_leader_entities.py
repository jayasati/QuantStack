"""Tests for named leader/institution entity extraction (Prompt 2.10 extension).

Covers the influential-people list: heads of state, central bankers, India
policymakers, ratings/multilateral institutions, and promoter-to-symbol
aliasing (Ambani/Adani), plus the tokenized entity matching that makes
punctuated names ("Moody's", "S&P") match correctly.
"""

from app.collectors.domains.news import LexiconSentimentProvider, classify, extract_entities


def _article(title: str) -> dict[str, str]:
    return {"title": title, "body": ""}


def test_foreign_leader_classifies_as_global() -> None:
    article = _article("Trump threatens new tariffs on Chinese imports")
    assert classify(article) == "global"
    assert "TRUMP" in extract_entities(article["title"])


def test_central_banker_classifies_as_global() -> None:
    article = _article("Fed Chair Powell signals rate cut path")
    assert classify(article) == "global"
    assert "POWELL" in extract_entities(article["title"])


def test_india_policymaker_classifies_as_policy_not_global() -> None:
    article = _article("PM Modi unveils new infrastructure push")
    assert classify(article) == "policy"
    assert "MODI" in extract_entities(article["title"])


def test_rbi_governor_full_name_classifies_as_policy() -> None:
    article = _article("RBI Governor Sanjay Malhotra signals rate pause")
    assert classify(article) == "policy"
    assert "SANJAY MALHOTRA" in extract_entities(article["title"])


def test_apostrophe_institution_name_matches_via_tokenization() -> None:
    # "Moody's" tokenizes to ["moody", "s"] — the apostrophe must not break
    # matching against the "MOODY" entity.
    article = _article("Moody's downgrades outlook citing fiscal risks")
    assert classify(article) == "global"
    assert "MOODY" in extract_entities(article["title"])


def test_ampersand_institution_name_matches_via_tokenization() -> None:
    article = _article("S&P raises India outlook to positive")
    assert classify(article) == "global"
    assert "S&P" in extract_entities(article["title"])


def test_opec_classifies_as_global() -> None:
    article = _article("OPEC agrees to cut oil output")
    assert classify(article) == "global"
    assert "OPEC" in extract_entities(article["title"])


def test_promoter_name_resolves_to_tradable_symbol() -> None:
    # "Ambani" alone (no "RELIANCE" mention) should still tag the stock,
    # since Reliance is a large enough Nifty weight that promoter-only
    # headlines routinely move the index.
    article = _article("Ambani unveils Jio AI platform at annual meet")
    entities = extract_entities(article["title"])
    assert "RELIANCE" in entities
    assert "AMBANI" not in entities  # resolved, not left as the raw name
    assert classify(article) == "stock"


def test_adani_resolves_to_symbol_even_when_not_on_watchlist() -> None:
    # ADANIENT isn't in this environment's watchlist, so it won't force a
    # "stock" classification, but the alias resolution must still happen —
    # future watchlist changes get this for free with no code change.
    article = _article("Adani group wins new port contract")
    assert "ADANIENT" in extract_entities(article["title"])


def test_bare_xi_is_not_used_avoiding_false_positives() -> None:
    # Only the full "XI JINPING" is tracked — a bare "XI" would collide with
    # ordinary text (roman numerals, clause numbering) the way bare "US" would.
    article = _article("Xi Jinping to attend the summit")
    assert "XI JINPING" in extract_entities(article["title"])


def test_leader_plus_conflict_language_scores_bearish() -> None:
    provider = LexiconSentimentProvider()
    score = provider.score("Trump imposes retaliatory tariffs amid trade war escalation")
    assert score < 0


def test_trade_deal_language_scores_bullish() -> None:
    provider = LexiconSentimentProvider()
    score = provider.score("US and India sign trade deal, tariffs lifted")
    assert score > 0
