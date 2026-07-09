from app.intelligence.sector import assess_sectors


def sample_sectors(**overrides) -> dict[str, dict[str, float]]:
    base = {
        "Banking": {
            "sector_heat_score": 82.0, "sector_leadership": 1.8,
            "sector_relative_strength": 4.5, "sector_momentum": 2.0,
            "sector_capital_rotation": 60.0,
        },
        "IT": {
            "sector_heat_score": 75.0, "sector_leadership": 1.2,
            "sector_relative_strength": 3.0, "sector_momentum": 1.5,
            "sector_capital_rotation": 40.0,
        },
        "Auto": {
            "sector_heat_score": 60.0, "sector_leadership": 0.3,
            "sector_relative_strength": 0.5, "sector_momentum": 0.2,
            "sector_capital_rotation": 10.0,
        },
        "Energy": {
            "sector_heat_score": 45.0, "sector_leadership": -0.2,
            "sector_relative_strength": -0.5, "sector_momentum": -0.3,
            "sector_capital_rotation": -5.0,
        },
        "Pharma": {
            "sector_heat_score": 30.0, "sector_leadership": -1.0,
            "sector_relative_strength": -2.0, "sector_momentum": -1.0,
            "sector_capital_rotation": -30.0,
        },
        "Metal": {
            "sector_heat_score": 20.0, "sector_leadership": -1.8,
            "sector_relative_strength": -4.0, "sector_momentum": -2.5,
            "sector_capital_rotation": -55.0,
        },
    }
    for name, updates in overrides.items():
        base.setdefault(name, {}).update(updates)
    return base


def sample_market(**overrides) -> dict[str, float]:
    base = {"sector_rotation_index": 8.0, "sector_participation_pct": 65.0}
    base.update(overrides)
    return base


def test_leading_and_lagging_sectors_ranked_by_heat() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    assert result.metrics["leading_sectors"] == ["Banking", "IT", "Auto"]
    assert result.metrics["lagging_sectors"] == ["Energy", "Pharma", "Metal"]


def test_broad_bullish_universe_scores_above_50() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    assert result.score > 50


def test_capital_rotation_intensity_is_mean_absolute_value() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    expected = sum([60, 40, 10, 5, 30, 55]) / 6
    assert result.metrics["capital_rotation_intensity"] == round(expected, 4)


def test_sector_heat_score_is_mean_across_universe() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    expected = sum([82, 75, 60, 45, 30, 20]) / 6
    assert result.metrics["sector_heat_score"] == round(expected, 4)


def test_relative_momentum_passthrough_per_sector() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    assert result.metrics["relative_momentum"]["Banking"] == 2.0
    assert result.metrics["relative_momentum"]["Metal"] == -2.5


def test_leadership_change_detected_on_sign_flip() -> None:
    # Energy flips from leading (+0.5 previously) to lagging (-0.2 now).
    previous = {
        "Banking": 1.7, "IT": 1.1, "Auto": 0.25,
        "Energy": 0.5, "Pharma": -1.1, "Metal": -1.7,
    }
    result = assess_sectors(sample_sectors(), sample_market(), previous_leadership=previous)
    assert "Energy" in result.metrics["leadership_changes"]
    # Banking/IT/Auto/Pharma/Metal moved less than the threshold and didn't flip sign.
    assert "Banking" not in result.metrics["leadership_changes"]


def test_leadership_change_detected_on_large_move_without_flip() -> None:
    # Banking stays positive but jumps by 2.0 z-score points (past threshold).
    previous = {"Banking": -0.2}
    result = assess_sectors(sample_sectors(), sample_market(), previous_leadership=previous)
    assert "Banking" in result.metrics["leadership_changes"]


def test_no_previous_leadership_means_no_changes_detected() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    assert result.metrics["leadership_changes"] == []


def test_states_sum_to_one() -> None:
    result = assess_sectors(sample_sectors(), sample_market())
    assert abs(sum(result.states.values()) - 1.0) < 1e-9


def test_missing_market_features_degrades_gracefully() -> None:
    result = assess_sectors(sample_sectors(), {})
    assert result.metrics["leading_sectors"] == ["Banking", "IT", "Auto"]
    assert 0.0 <= result.confidence <= 1.0


def test_no_data_defaults_to_neutral_with_low_confidence() -> None:
    result = assess_sectors({}, {})
    assert result.score == 50.0
    assert result.confidence < 0.4
    assert result.metrics["leading_sectors"] == []
    assert result.metrics["sector_heat_score"] is None


def test_more_complete_data_increases_confidence() -> None:
    sparse = assess_sectors({"Banking": {"sector_heat_score": 82.0}}, {})
    rich = assess_sectors(sample_sectors(), sample_market())
    assert rich.confidence > sparse.confidence


def test_high_rotation_and_broad_participation_favors_broad_rotation_state() -> None:
    result = assess_sectors(
        sample_sectors(), sample_market(sector_rotation_index=15.0, sector_participation_pct=80.0)
    )
    assert result.states["broad_rotation"] > result.states["narrow_leadership"]
