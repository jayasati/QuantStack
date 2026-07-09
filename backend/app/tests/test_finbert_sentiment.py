"""FinBERT sentiment provider tests (Volume 2, Prompt 2.10 upgrade).

These load the real ~440MB ProsusAI/finbert model on first use (cached
after that) and run real CPU inference — slower than the rest of the unit
suite by design, since the whole point is verifying the actual model
integration works, not a mocked stand-in.
"""

import pytest

from app.collectors.sources.finbert_sentiment import FinBertSentimentProvider

pytestmark = pytest.mark.filterwarnings("ignore")


def test_lazy_construction_does_not_load_model() -> None:
    provider = FinBertSentimentProvider()
    assert provider._pipeline is None  # no network/disk work until first score


def test_score_signs_clearly_positive_and_negative_financial_text() -> None:
    provider = FinBertSentimentProvider()
    positive = provider.score(
        "The company reported record profits and beat analyst estimates by a wide margin."
    )
    negative = provider.score(
        "The company defaulted on its debt and reported a massive fraud investigation."
    )
    assert positive > 0
    assert negative < 0
    assert -1.0 <= positive <= 1.0
    assert -1.0 <= negative <= 1.0


def test_score_empty_text_is_zero_without_touching_model() -> None:
    provider = FinBertSentimentProvider()
    assert provider.score("") == 0.0
    assert provider._pipeline is None


def test_score_batch_matches_individual_scores_and_handles_empty_strings() -> None:
    provider = FinBertSentimentProvider()
    texts = [
        "Record profits beat estimates.",
        "",
        "Massive fraud and default risk rattles investors.",
    ]
    batch_scores = provider.score_batch(texts)
    assert len(batch_scores) == 3
    assert batch_scores[1] == 0.0
    assert batch_scores[0] > 0
    assert batch_scores[2] < 0


def test_score_batch_empty_list_returns_empty_list() -> None:
    provider = FinBertSentimentProvider()
    assert provider.score_batch([]) == []
