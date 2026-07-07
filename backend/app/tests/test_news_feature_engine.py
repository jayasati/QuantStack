from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.features.news import (
    Article,
    NewsFeatureEngine,
    compute_news_features,
)

BASE_TS = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def val(x: float | None) -> float:
    assert x is not None
    return x


def article(minutes: int, sentiment: float = 0.5, novelty: float = 0.8,
            urgency: str = "high", category: str = "corporate",
            title: str = "Company beats estimates on strong quarter",
            entities: tuple[str, ...] = ()) -> Article:
    return Article(ts=BASE_TS + timedelta(minutes=minutes), sentiment=sentiment,
                   novelty=novelty, urgency=urgency, category=category,
                   title=title, entities=entities)


def test_hourly_aggregates() -> None:
    articles = [
        article(5, sentiment=1.0, novelty=0.9, urgency="high"),
        article(20, sentiment=0.0, novelty=0.5, urgency="low"),
        article(70, sentiment=-1.0, urgency="medium"),  # next hour
    ]
    timestamps, series = compute_news_features(articles)
    assert len(timestamps) == 2
    assert series["news_sentiment"][0] == pytest.approx(0.5)
    assert series["news_novelty"][0] == pytest.approx(0.7)
    assert series["news_urgency"][0] == pytest.approx(0.5)  # (1 + 0) / 2
    assert series["news_sentiment"][1] == pytest.approx(-1.0)
    assert series["news_urgency"][1] == pytest.approx(0.5)


def test_momentum_reflects_flow_acceleration() -> None:
    articles = [article(60 * h) for h in range(6)]  # one per hour baseline
    articles += [article(60 * 6 + m) for m in range(0, 50, 10)]  # burst of 5
    timestamps, series = compute_news_features(articles)
    assert val(series["news_momentum"][-1]) > 2.0
    assert series["news_momentum"][0] == pytest.approx(1.0)


def test_empty_hours_keep_timeline_but_no_content() -> None:
    articles = [article(0), article(60 * 3)]  # gap of two hours
    timestamps, series = compute_news_features(articles)
    assert len(timestamps) == 4
    assert series["news_sentiment"][1] is None
    assert series["news_momentum"][1] == pytest.approx(0.0)  # zero flow hour


def test_headline_similarity_and_topic_entropy() -> None:
    same_story = [
        article(0, title="RBI holds repo rate steady in policy review"),
        article(5, title="RBI holds repo rate steady, policy review says"),
    ]
    _, series = compute_news_features(same_story)
    assert val(series["news_headline_similarity"][0]) > 0.5
    assert series["news_topic_entropy"][0] == 0.0  # single category

    mixed = [
        article(0, category="macro", title="Inflation cools sharply this month"),
        article(5, category="corporate", title="Automaker announces record exports"),
    ]
    _, mixed_series = compute_news_features(mixed)
    assert mixed_series["news_topic_entropy"][0] == pytest.approx(1.0)
    assert val(mixed_series["news_headline_similarity"][0]) < 0.2


def test_sector_and_stock_impact_shares() -> None:
    articles = [
        article(0, category="corporate", entities=("RELIANCE",)),
        article(5, category="macro"),
    ]
    _, series = compute_news_features(articles)
    assert series["news_sector_impact"][0] == pytest.approx(0.5)
    assert series["news_stock_impact"][0] == pytest.approx(0.5)
    assert series["news_entity_frequency"][0] == pytest.approx(0.5)


def test_impact_probability_bounded_and_monotone_in_urgency() -> None:
    hot = [article(0, sentiment=1.0, novelty=1.0, urgency="high")]
    cold = [article(0, sentiment=0.0, novelty=0.1, urgency="low")]
    _, hot_series = compute_news_features(hot)
    _, cold_series = compute_news_features(cold)
    assert 0.05 <= val(cold_series["news_impact_probability"][0]) < val(
        hot_series["news_impact_probability"][0]
    ) <= 0.95


def test_registration_and_z_companions() -> None:
    articles = [article(10 * i, sentiment=0.1 * (i % 5)) for i in range(60)]
    _, series = compute_news_features(articles, normalization_window=20)
    raw = [name for name in series if not name.endswith("_z")]
    assert len(raw) == 10
    for name in raw:
        assert f"{name}_z" in series

    engine = NewsFeatureEngine(settings=Settings())
    definitions = engine.registry.list_definitions(category="news")
    assert len(definitions) == 10 * 2
    assert all(d.version == "v1" for d in definitions)
