import pytest

from app.intelligence.base import slope
from app.intelligence.regime import BayesianRegimeDetector
from app.intelligence.transitions import RegimeTransitionEngine, assess_regime_transition


class StubAlertService:
    def __init__(self) -> None:
        self.fired: list[dict] = []

    async def fire(self, source, severity, message, **context):
        self.fired.append({"source": source, "severity": severity, "message": message, **context})


def test_slope_increasing_series_is_positive() -> None:
    assert slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


def test_slope_decreasing_series_is_negative() -> None:
    assert slope([4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_slope_flat_series_is_zero() -> None:
    assert slope([2.0, 2.0, 2.0]) == pytest.approx(0.0)


def test_slope_single_value_is_zero() -> None:
    assert slope([5.0]) == 0.0


def test_stable_regime_reads_stable_with_no_alert() -> None:
    stable = [{"bull": 0.8, "bear": 0.2}] * 10
    result = assess_regime_transition(stable)
    assert result.metrics["transition_probability"] == pytest.approx(0.24)
    assert result.metrics["transition_speed"] == pytest.approx(0.0)
    assert result.metrics["confidence_loss"] == pytest.approx(0.0)
    assert max(result.states, key=lambda s: result.states[s]) == "stable"
    assert result.metrics["alert"] is False
    assert result.metrics["alert_message"] is None


def test_active_transition_reads_transitioning_with_alert() -> None:
    transitioning = [{"bull": 0.9 - 0.03 * i, "bear": 0.1 + 0.03 * i} for i in range(10)]
    result = assess_regime_transition(transitioning)
    assert result.metrics["transition_probability"] == pytest.approx(0.684)
    assert result.metrics["transition_speed"] == pytest.approx(-0.03)
    assert result.metrics["confidence_loss"] == pytest.approx(0.27)
    assert max(result.states, key=lambda s: result.states[s]) == "transitioning"
    assert result.metrics["alert"] is True
    assert "bull -> bear" in result.metrics["alert_message"]


def test_alert_threshold_is_configurable() -> None:
    near_tie = [{"bull": 0.52, "bear": 0.48}] * 5
    default = assess_regime_transition(near_tie)
    assert default.metrics["alert"] is False  # 0.576 < default 0.6
    lower_threshold = assess_regime_transition(near_tie, alert_threshold=0.5)
    assert lower_threshold.metrics["alert"] is True  # 0.576 >= 0.5


def test_near_tie_alone_drives_transition_probability_without_momentum() -> None:
    near_tie = [{"bull": 0.52, "bear": 0.48}] * 5
    result = assess_regime_transition(near_tie)
    assert result.metrics["transition_speed"] == pytest.approx(0.0)
    assert result.metrics["transition_probability"] > 0.5


def test_states_sum_to_one() -> None:
    transitioning = [{"bull": 0.9 - 0.03 * i, "bear": 0.1 + 0.03 * i} for i in range(10)]
    result = assess_regime_transition(transitioning)
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_insufficient_history_returns_graceful_stable_default() -> None:
    result = assess_regime_transition([{"bull": 0.9, "bear": 0.1}])
    assert result.score == 0.0
    assert result.confidence < 0.2
    assert max(result.states, key=lambda s: result.states[s]) == "stable"
    assert result.metrics["transition_probability"] is None
    assert result.metrics["alert"] is False


def test_empty_history_returns_graceful_default() -> None:
    result = assess_regime_transition([])
    assert result.score == 0.0
    assert result.metrics["alert"] is False


def test_empty_latest_snapshot_returns_graceful_default() -> None:
    result = assess_regime_transition([{"bull": 0.9, "bear": 0.1}, {}])
    assert result.score == 0.0
    assert result.metrics["current_state"] is None


def test_data_sufficiency_scales_confidence() -> None:
    short = assess_regime_transition([{"bull": 0.8, "bear": 0.2}] * 3)
    long = assess_regime_transition([{"bull": 0.8, "bear": 0.2}] * 10)
    assert long.confidence > short.confidence


async def test_engine_wires_target_component_symbol_timeframe(monkeypatch) -> None:
    async def fake_history(self, component, symbol, timeframe, limit=20):
        assert component == "trend"
        assert symbol == "NIFTY"
        assert timeframe == "D"
        return [{"bull": 0.9 - 0.03 * i, "bear": 0.1 + 0.03 * i} for i in range(10)]

    monkeypatch.setattr(BayesianRegimeDetector, "history", fake_history)

    engine = RegimeTransitionEngine()
    result = await engine.assess(component="trend", symbol="NIFTY", timeframe="D")

    assert result.metrics["target_component"] == "trend"
    assert result.metrics["symbol"] == "NIFTY"
    assert result.metrics["timeframe"] == "D"
    assert result.metrics["alert"] is True


async def test_engine_routes_alert_through_alert_service(monkeypatch) -> None:
    """Previously an alert only set a metric flag consumed internally by
    opportunity.py and published to an EventBus topic with zero
    subscribers -- it never reached AlertService, so it never reached a
    human. This confirms the wiring actually calls .fire()."""

    async def fake_history(self, component, symbol, timeframe, limit=20):
        return [{"bull": 0.9 - 0.03 * i, "bear": 0.1 + 0.03 * i} for i in range(10)]

    monkeypatch.setattr(BayesianRegimeDetector, "history", fake_history)

    alerts = StubAlertService()
    engine = RegimeTransitionEngine(alerts=alerts)
    result = await engine.assess(component="trend", symbol="NIFTY", timeframe="D")

    assert result.metrics["alert"] is True
    assert len(alerts.fired) == 1
    assert alerts.fired[0]["source"] == "regime_transition_engine"
    assert alerts.fired[0]["symbol"] == "NIFTY"
    assert alerts.fired[0]["component"] == "trend"


async def test_engine_without_alert_service_does_not_raise(monkeypatch) -> None:
    """alerts=None (the default) must still work -- same graceful-degrade
    convention as every other optional dependency in this codebase."""

    async def fake_history(self, component, symbol, timeframe, limit=20):
        return [{"bull": 0.9 - 0.03 * i, "bear": 0.1 + 0.03 * i} for i in range(10)]

    monkeypatch.setattr(BayesianRegimeDetector, "history", fake_history)

    engine = RegimeTransitionEngine()  # alerts defaults to None
    result = await engine.assess(component="trend", symbol="NIFTY", timeframe="D")
    assert result.metrics["alert"] is True  # must not raise despite no alert sink


async def test_engine_defaults_to_trend_and_benchmark_symbol(monkeypatch) -> None:
    seen = {}

    async def fake_history(self, component, symbol, timeframe, limit=20):
        seen["component"] = component
        seen["symbol"] = symbol
        seen["timeframe"] = timeframe
        return []

    monkeypatch.setattr(BayesianRegimeDetector, "history", fake_history)

    engine = RegimeTransitionEngine()
    await engine.assess()

    assert seen["component"] == "trend"
    assert seen["timeframe"] == "D"
    assert seen["symbol"]  # resolved to the configured benchmark symbol
