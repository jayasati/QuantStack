"""Tests for the Duplicate Signal Engine (Volume 5, Prompt 5.14)."""

from datetime import UTC, datetime, timedelta

from app.prediction.duplicate import (
    CORRELATION_THRESHOLD,
    MAX_SIGNALS_PER_SECTOR,
    DuplicateSignalEngine,
    is_breakout_signal,
    pairwise_correlation,
)
from app.prediction.priority import RankedSignal

BASE_TS = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def make_signal(
    symbol: str, priority_score: float = 50.0, direction: str = "long",
    reason: str = "Long bias on trend.", as_of: datetime = BASE_TS,
) -> RankedSignal:
    return RankedSignal(
        rank=0, symbol=symbol, direction=direction, priority_score=priority_score,
        data_completeness=1.0, conviction_score=70.0, conviction_grade="B",
        as_of=as_of, reason=reason,
    )


# --- pure helpers -------------------------------------------------------


def test_is_breakout_signal_matches_case_insensitively() -> None:
    assert is_breakout_signal("Long bias on significant breakout probability.")
    assert is_breakout_signal("BREAKOUT confirmed.")
    assert not is_breakout_signal("Long bias on institutional accumulation.")


def test_pairwise_correlation_is_one_for_identical_series() -> None:
    series = [0.01, 0.02, -0.01, 0.03, -0.02, 0.01, 0.02]
    assert pairwise_correlation(series, series) == 1.0


def test_pairwise_correlation_is_negative_one_for_inverted_series() -> None:
    series = [0.01, 0.02, -0.01, 0.03, -0.02, 0.01, 0.02]
    inverted = [-x for x in series]
    assert pairwise_correlation(series, inverted) == -1.0


def test_pairwise_correlation_is_none_with_too_few_points() -> None:
    assert pairwise_correlation([0.01], [0.02]) is None


# --- filter_signals: greedy de-duplication -----------------------------


async def test_filter_signals_keeps_a_single_isolated_signal() -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    result = await engine.filter_signals([make_signal("NIFTY")])
    assert [s.symbol for s in result.kept] == ["NIFTY"]
    assert result.suppressed == []


async def test_filter_signals_suppresses_repeated_breakouts_keeping_the_higher_priority_one(
    monkeypatch,
) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    signals = [
        make_signal("A", priority_score=90.0, reason="breakout confirmed"),
        make_signal("B", priority_score=80.0, reason="another breakout setup"),
    ]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)

    result = await engine.filter_signals(signals)
    assert [s.symbol for s in result.kept] == ["A"]
    assert len(result.suppressed) == 1
    assert result.suppressed[0].symbol == "B"
    assert any("Repeated Breakouts" in r for r in result.suppressed[0].reasons)


async def test_filter_signals_does_not_suppress_a_single_breakout_signal(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    signals = [make_signal("A", reason="breakout confirmed")]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)
    result = await engine.filter_signals(signals)
    assert [s.symbol for s in result.kept] == ["A"]


async def test_filter_signals_suppresses_correlated_stocks(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    signals = [
        make_signal("A", priority_score=90.0),
        make_signal("B", priority_score=80.0),
    ]

    async def fake_correlation(symbol_a, symbol_b):
        return CORRELATION_THRESHOLD + 0.05

    monkeypatch.setattr(engine, "_pairwise_correlation", fake_correlation)

    result = await engine.filter_signals(signals)
    assert [s.symbol for s in result.kept] == ["A"]
    assert result.suppressed[0].symbol == "B"
    assert any("Correlated Stocks" in r for r in result.suppressed[0].reasons)


async def test_filter_signals_keeps_uncorrelated_stocks(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    signals = [make_signal("A", priority_score=90.0), make_signal("B", priority_score=80.0)]

    async def low_correlation(symbol_a, symbol_b):
        return 0.1

    monkeypatch.setattr(engine, "_pairwise_correlation", low_correlation)
    result = await engine.filter_signals(signals)
    assert {s.symbol for s in result.kept} == {"A", "B"}


async def test_filter_signals_suppresses_sector_duplication(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    engine._settings.feature_stock_sectors = {"RELIANCE": "Energy", "ONGC": "Energy"}
    signals = [
        make_signal("RELIANCE", priority_score=90.0),
        make_signal("ONGC", priority_score=80.0),
    ]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)
    result = await engine.filter_signals(signals)
    assert [s.symbol for s in result.kept] == ["RELIANCE"]
    assert any("Sector Duplication" in r for r in result.suppressed[0].reasons)


async def test_filter_signals_never_suppresses_on_an_unknown_sector(monkeypatch) -> None:
    """Missing sector coverage reads as "unknown", never fabricated --
    two symbols with no configured sector are never suppressed as
    sector-duplicates of each other."""
    engine = DuplicateSignalEngine(session_factory=None)
    engine._settings.feature_stock_sectors = {}
    signals = [
        make_signal("UNMAPPED_A", priority_score=90.0),
        make_signal("UNMAPPED_B", priority_score=80.0),
    ]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)
    result = await engine.filter_signals(signals)
    assert {s.symbol for s in result.kept} == {"UNMAPPED_A", "UNMAPPED_B"}


async def test_filter_signals_allows_multiple_per_sector_up_to_the_max(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    engine._settings.feature_stock_sectors = {"A": "IT"}
    assert MAX_SIGNALS_PER_SECTOR == 1  # documents the current v1 default this test relies on

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)
    result = await engine.filter_signals([make_signal("A")])
    assert result.suppressed == []


async def test_filter_signals_suppresses_repeated_opportunities(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)

    async def fake_recent(symbol=None, limit=50):
        return [{
            "symbol": "NIFTY", "direction": "long",
            "as_of": (BASE_TS - timedelta(minutes=10)).isoformat(),
        }]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine._priority, "recent", fake_recent)
    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)

    result = await engine.filter_signals([make_signal("NIFTY", as_of=BASE_TS)])
    assert result.kept == []
    assert any("Repeated Opportunity" in r for r in result.suppressed[0].reasons)


async def test_filter_signals_does_not_suppress_outside_the_cooldown_window(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)

    async def fake_recent(symbol=None, limit=50):
        return [{
            "symbol": "NIFTY", "direction": "long",
            "as_of": (BASE_TS - timedelta(hours=5)).isoformat(),
        }]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine._priority, "recent", fake_recent)
    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)

    result = await engine.filter_signals([make_signal("NIFTY", as_of=BASE_TS)])
    assert [s.symbol for s in result.kept] == ["NIFTY"]


async def test_filter_signals_does_not_suppress_a_different_direction_repeat(monkeypatch) -> None:
    engine = DuplicateSignalEngine(session_factory=None)

    async def fake_recent(symbol=None, limit=50):
        return [{
            "symbol": "NIFTY", "direction": "short",
            "as_of": (BASE_TS - timedelta(minutes=10)).isoformat(),
        }]

    async def no_correlation(symbol_a, symbol_b):
        return None

    monkeypatch.setattr(engine._priority, "recent", fake_recent)
    monkeypatch.setattr(engine, "_pairwise_correlation", no_correlation)

    result = await engine.filter_signals([make_signal("NIFTY", direction="long", as_of=BASE_TS)])
    assert [s.symbol for s in result.kept] == ["NIFTY"]


# --- engine, no DB: honest degradation ---------------------------------------


async def test_filter_signals_on_empty_input_is_empty() -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    result = await engine.filter_signals([])
    assert result.kept == []
    assert result.suppressed == []


async def test_rank_and_filter_without_a_db_returns_nothing() -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    result = await engine.rank_and_filter()
    assert result.kept == []
    assert result.suppressed == []


async def test_recent_returns_empty_list_without_a_session_factory() -> None:
    engine = DuplicateSignalEngine(session_factory=None)
    assert await engine.recent() == []
