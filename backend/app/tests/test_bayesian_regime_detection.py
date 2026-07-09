import pytest

from app.intelligence.base import IntelligenceResult
from app.intelligence.regime import BayesianRegimeDetector, RegimeBelief, bayesian_update


def test_no_prior_returns_normalized_likelihood() -> None:
    posterior = bayesian_update(None, {"bull": 3.0, "bear": 1.0}, evidence_confidence=0.9)
    assert posterior == {"bull": 0.75, "bear": 0.25}


def test_high_confidence_shifts_strongly_toward_likelihood() -> None:
    posterior = bayesian_update(
        {"bull": 0.5, "bear": 0.5}, {"bull": 1.0, "bear": 0.0}, evidence_confidence=0.8
    )
    # weight = clamp(0.8, 0.1, 0.9) = 0.8
    assert posterior["bull"] == pytest.approx(0.2 * 0.5 + 0.8 * 1.0)
    assert posterior["bear"] == pytest.approx(0.2 * 0.5 + 0.8 * 0.0)


def test_max_confidence_still_blends_not_overwrites() -> None:
    posterior = bayesian_update(
        {"bull": 0.5, "bear": 0.5}, {"bull": 1.0, "bear": 0.0}, evidence_confidence=1.0
    )
    # weight capped at MAX_WEIGHT=0.9, not 1.0 — never a hard switch.
    assert posterior["bull"] == pytest.approx(0.1 * 0.5 + 0.9 * 1.0)
    assert posterior["bull"] < 1.0


def test_zero_confidence_still_nudges_the_prior() -> None:
    posterior = bayesian_update(
        {"bull": 0.5, "bear": 0.5}, {"bull": 1.0, "bear": 0.0}, evidence_confidence=0.0
    )
    # weight floored at MIN_WEIGHT=0.1 — never frozen forever either.
    assert posterior["bull"] == pytest.approx(0.9 * 0.5 + 0.1 * 1.0)
    assert posterior["bull"] > 0.5


def test_mismatched_state_keys_are_unioned_with_zero_fill() -> None:
    posterior = bayesian_update(
        {"bull": 1.0}, {"bull": 0.5, "transition": 0.5}, evidence_confidence=0.5
    )
    assert set(posterior) == {"bull", "transition"}
    assert abs(sum(posterior.values()) - 1.0) < 1e-9


async def test_engine_update_with_no_prior_uses_likelihood_directly(monkeypatch) -> None:
    async def fake_load(self, component, symbol, timeframe):
        return None

    stored = []

    async def fake_store(self, component, symbol, timeframe, states, observation_count):
        stored.append((component, symbol, timeframe, dict(states), observation_count))

    monkeypatch.setattr(BayesianRegimeDetector, "_load_belief", fake_load)
    monkeypatch.setattr(BayesianRegimeDetector, "_store_belief", fake_store)

    detector = BayesianRegimeDetector()
    result = await detector.update("trend", "NIFTY", "D", {"bull": 0.7, "bear": 0.3}, 0.9)

    assert result.states == {"bull": 0.7, "bear": 0.3}
    assert result.metrics["observation_count"] == 1
    assert result.metrics["prior"] is None
    assert stored == [("trend", "NIFTY", "D", {"bull": 0.7, "bear": 0.3}, 1)]


async def test_engine_update_blends_stored_prior_with_new_likelihood(monkeypatch) -> None:
    async def fake_load(self, component, symbol, timeframe):
        return RegimeBelief(states={"bull": 0.6, "bear": 0.4}, observation_count=5)

    stored = []

    async def fake_store(self, component, symbol, timeframe, states, observation_count):
        stored.append(observation_count)

    monkeypatch.setattr(BayesianRegimeDetector, "_load_belief", fake_load)
    monkeypatch.setattr(BayesianRegimeDetector, "_store_belief", fake_store)

    detector = BayesianRegimeDetector()
    result = await detector.update("trend", "NIFTY", "D", {"bull": 0.9, "bear": 0.1}, 0.8)

    # weight = 0.8: bull = 0.2*0.6 + 0.8*0.9 = 0.84
    assert result.states["bull"] == pytest.approx(0.84)
    assert result.metrics["observation_count"] == 6
    assert stored == [6]


async def test_update_from_result_extracts_states_and_confidence(monkeypatch) -> None:
    async def fake_load(self, component, symbol, timeframe):
        return None

    async def fake_store(self, component, symbol, timeframe, states, observation_count):
        pass

    monkeypatch.setattr(BayesianRegimeDetector, "_load_belief", fake_load)
    monkeypatch.setattr(BayesianRegimeDetector, "_store_belief", fake_store)

    detector = BayesianRegimeDetector()
    fake_result = IntelligenceResult(
        component="trend", score=80.0, confidence=0.6, states={"bull": 0.8, "bear": 0.2}
    )
    result = await detector.update_from_result("trend", "NIFTY", "D", fake_result)

    assert result.states == {"bull": 0.8, "bear": 0.2}
    assert result.metrics["target_component"] == "trend"


async def test_load_belief_returns_none_without_a_session() -> None:
    detector = BayesianRegimeDetector()  # no session_factory -> self._sessions is None
    belief = await detector._load_belief("trend", "NIFTY", "D")
    assert belief is None


async def test_store_belief_is_a_noop_without_a_session() -> None:
    detector = BayesianRegimeDetector()
    await detector._store_belief("trend", "NIFTY", "D", {"bull": 1.0}, 1)  # must not raise


async def test_history_returns_empty_list_without_a_session() -> None:
    detector = BayesianRegimeDetector()
    history = await detector.history("trend", "NIFTY", "D")
    assert history == []


async def test_maturity_increases_confidence_over_repeated_observations(monkeypatch) -> None:
    counts = iter([0, 19])

    async def fake_load(self, component, symbol, timeframe):
        count = next(counts)
        if not count:
            return None
        return RegimeBelief(states={"bull": 0.5, "bear": 0.5}, observation_count=count)

    async def fake_store(self, component, symbol, timeframe, states, observation_count):
        pass

    monkeypatch.setattr(BayesianRegimeDetector, "_load_belief", fake_load)
    monkeypatch.setattr(BayesianRegimeDetector, "_store_belief", fake_store)

    detector = BayesianRegimeDetector()
    young = await detector.update("trend", "NIFTY", "D", {"bull": 0.6, "bear": 0.4}, 0.5)
    mature = await detector.update("trend", "NIFTY", "D", {"bull": 0.6, "bear": 0.4}, 0.5)
    assert mature.confidence > young.confidence
