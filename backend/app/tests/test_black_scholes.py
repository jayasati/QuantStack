"""Black-Scholes Greeks tests (pure math, offline)."""

import pytest

from app.market.black_scholes import black_scholes_greeks


def test_atm_call_delta_near_half() -> None:
    greeks = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="CE",
    )
    assert greeks is not None
    assert 0.45 < greeks.delta < 0.55


def test_put_call_parity_on_delta() -> None:
    """call_delta - put_delta == 1 at the same strike/spot/IV/expiry -- a
    hard mathematical identity, not just a sanity range."""
    call = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="CE",
    )
    put = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="PE",
    )
    assert call is not None and put is not None
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-9)


def test_gamma_and_vega_positive_and_shared_across_legs() -> None:
    call = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="CE",
    )
    put = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="PE",
    )
    assert call is not None and put is not None
    assert call.gamma > 0
    assert call.vega > 0
    # Gamma/vega are identical between legs when computed off the same IV.
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega == pytest.approx(put.vega)


def test_theta_is_negative_for_long_option() -> None:
    """Time decay: a long option loses value each day, all else equal."""
    greeks = black_scholes_greeks(
        spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="CE",
    )
    assert greeks is not None
    assert greeks.theta < 0


def test_deep_itm_call_delta_near_one() -> None:
    greeks = black_scholes_greeks(
        spot=77400, strike=60000, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="CE",
    )
    assert greeks is not None
    assert greeks.delta > 0.95


def test_deep_otm_put_delta_near_zero() -> None:
    greeks = black_scholes_greeks(
        spot=77400, strike=60000, iv_pct=17.0, time_to_expiry_years=3 / 365, option_type="PE",
    )
    assert greeks is not None
    assert greeks.delta > -0.05


@pytest.mark.parametrize(
    "kwargs",
    [
        {"spot": 0, "strike": 77400, "iv_pct": 17.0, "time_to_expiry_years": 0.01},
        {"spot": 77400, "strike": 0, "iv_pct": 17.0, "time_to_expiry_years": 0.01},
        {"spot": 77400, "strike": 77400, "iv_pct": 0, "time_to_expiry_years": 0.01},
        {"spot": 77400, "strike": 77400, "iv_pct": 17.0, "time_to_expiry_years": 0},
        {"spot": 77400, "strike": 77400, "iv_pct": 17.0, "time_to_expiry_years": -1},
    ],
)
def test_degenerate_inputs_return_none_not_nan(kwargs) -> None:
    assert black_scholes_greeks(option_type="CE", **kwargs) is None


def test_unknown_option_type_raises() -> None:
    with pytest.raises(ValueError):
        black_scholes_greeks(
            spot=77400, strike=77400, iv_pct=17.0, time_to_expiry_years=0.01,
            option_type="XX",
        )
