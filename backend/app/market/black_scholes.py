"""Black-Scholes Greeks — computed from real market IV, not broker-sourced.

Used where neither the exchange nor the broker publishes raw Delta/Theta/
Gamma/Vega — confirmed by direct probe (2026-07-13) that this is the case
for BSE-listed contracts (Sensex, Bankex): BSE's own DerivOptionChain_IV
feed carries OI/LTP/Volume/IV but no Greeks, and Angel One SmartAPI's
Option Greek endpoint has zero data for BSE names (NSE-only coverage).

This is standard practice, not fabrication: Greeks are always IV-derived —
there is no separate "true" Delta/Theta an exchange could publish that
isn't itself computed from IV via a pricing model. Any platform showing
Greeks for an exchange that doesn't publish them is computing them the same
way, from real market IV.

European options, no dividend yield adjustment (index options here; the
retail-standard simplification most comparable platforms also use).
"""

import math
from dataclasses import dataclass

# Short-tenor Indian risk-free proxy (T-bill / repo-adjacent). A simplifying
# constant, not fetched live — same spirit as this codebase's other
# documented v1 heuristic constants (e.g. options.py's DIRECTION_EPSILON).
DEFAULT_RISK_FREE_RATE = 0.065


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 percentage point of IV


def black_scholes_greeks(
    *,
    spot: float,
    strike: float,
    iv_pct: float,
    time_to_expiry_years: float,
    option_type: str,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Greeks | None:
    """None on degenerate inputs (expired/non-positive IV or spot) rather
    than a divide-by-zero NaN masquerading as a real number."""
    if spot <= 0 or strike <= 0 or iv_pct <= 0 or time_to_expiry_years <= 0:
        return None
    sigma = iv_pct / 100
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot / strike) + (risk_free_rate + sigma * sigma / 2) * time_to_expiry_years
    ) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-risk_free_rate * time_to_expiry_years)

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100  # per 1 percentage point of IV

    normalized_type = option_type.upper()
    if normalized_type in ("CE", "CALL", "C"):
        delta = _norm_cdf(d1)
        theta_per_year = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - risk_free_rate * strike * discount * _norm_cdf(d2)
        )
    elif normalized_type in ("PE", "PUT", "P"):
        delta = _norm_cdf(d1) - 1
        theta_per_year = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + risk_free_rate * strike * discount * _norm_cdf(-d2)
        )
    else:
        raise ValueError(f"unknown option_type: {option_type}")

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta_per_year / 365,  # per calendar day
        vega=vega,
    )
