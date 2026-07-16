# Volume 4 Preflight — Market Intelligence & Regime Analysis Engine (2026-07-16)

**Scope note:** Same situation as the Volume 2/3 preflights — Volume 4 was
already built (all 17 prompts) before this process existed. This is a
retroactive check of whether Volume 3's foundation (just postflighted hours
earlier today, COMPLETE-WITH-DEBT) actually supports what Volume 4 already
does, live, right now — and specifically what the *next* Volume 4 build
chunk should be, since DEBT-1/DEBT-2 (both Volume 4-adjacent) are the most
consequential open items in the ledger.

**Method:** repo inspection (`app/core/container.py` wiring), live SQL/API
checks per `prompts/VERIFY-COOKBOOK.md`, run 2026-07-16 ~21:00-21:10 IST
(market closed).

**Verdict: GO**, with one prominent, non-blocking finding that should shape
the next build chunk: Volume 4's own intelligence layer currently violates
I-1 (DEBT-1), and any new Volume 4 work should target *fixing* that, not
layering more D-only features on top of it.

---

## Dependency table

| Dependency | Code | Tests | Live | Verdict |
|---|---|---|---|---|
| Feature Store (dual persistence, registry, quality, drift) | ✅ inherited | ✅ inherited | ✅ **inherited from today's Volume 3 postflight** (`docs/volumes/postflight-vol3-2026-07-16.md`, COMPLETE-WITH-DEBT) — not re-derived here | PASS (inherited) |
| 12 domain feature engines (price/volume/volatility/liquidity/options/breadth/sector/relative/structure/news/events/time) | ✅ inherited | ✅ inherited | ✅ inherited (today's postflight confirmed all registered + producing) | PASS (inherited) |
| Institutional Flow features (Ch.9/4.5 input) | ✅ `app/features/institutional_flow.py` | ✅ inherited | ✅ live: `feature_registry` category `institutional_flow` = 54 features (confirmed in today's Vol3 postflight); DEBT-11 notes 35 of these lack quality scores — doesn't block freshness, only quality-scoring coverage | PASS (quality coverage caveat: DEBT-11) |
| Macro features (Ch.12/4.8 input: USDINR/crude/gold/global indices) | ✅ `app/features/macro.py` | ✅ inherited | ✅ live: category `macro` = 8 features, confirmed registered | PASS |
| All 12 Volume 4 intelligence engines registered + wired | ✅ confirmed via `container.py` grep: Trend, Volatility, Breadth, Sector, InstitutionalFlow, Liquidity, Event, Correlation, RelativeStrength, HistoricalAnalog, BayesianRegimeDetector, RegimeTransition, MarketConfidence, CompositeMarketIntelligence, MarketStateReport, Explainability all present and wired into each other's constructors | — | — | PASS |
| Regime beliefs persisted + updated (Ch.15) | ✅ `regime.py` | ✅ inherited | ✅ `regime_belief.update`: 8053 rows, latest **20:42:27 IST** (26 min before check) | PASS |
| Market Confidence Engine live (Ch.17) | ✅ `confidence.py` | ✅ inherited | ✅ `market_confidence.observation`: 12,223 rows, latest **21:07:35 IST** — essentially real-time relative to the check | PASS |
| Composite/Explainability live (Ch.16, Ch.18, Ch.20) | ✅ `explain.py`, `composite.py` | ✅ inherited | ✅ `explainability.observation`: 45,214 rows, latest 21:02:32 IST | PASS |
| Market State Report persisted + replayable (Ch.19) | ✅ `report.py` | ✅ inherited | ✅ `market_state_report.observation`: 12,195 rows, latest **21:07:36 IST**; `report_as_of()` already proven live in Volume 4's original build (per project memory) | PASS |
| Market Intelligence API serves a complete report (Ch.21) | ✅ `api/intelligence.py` | ✅ inherited | ✅ **`GET /intelligence/state/HDFCBANK` live-fetched and inspected in full**: every Ch.19-required field present (`current_regimes`, `probabilities`, `trend_summary`, `breadth_summary`, `liquidity_summary`, `sector_leaders`, `macro_summary`, `institutional_positioning`, `historical_analogs`, `market_confidence`, `composite_intelligence_score`, `expected_opportunity`, `expected_risk`) — see behavior evidence below | PASS |
| Historical Analog Engine finds real analogs (Ch.14) | ✅ `analogs.py` | ✅ inherited | ✅ live response: **20 real analogs** for HDFCBANK, each with genuine `similarity`/`subsequent_return`/`subsequent_volatility`/`max_drawdown`/`max_runup` values (top one: similarity 0.9578, dated 2026-06-15) | PASS |
| No hard-label regime switching (Ch.15 warning) | ✅ `IntelligenceComponent` contract: 0-1 probabilistic `states`, never a hard label | ✅ inherited | ✅ live `probabilities.trend` for HDFCBANK: 4-way blend (`range_bound` 0.315, `strong_bull_trend` 0.320, `weak_bull_trend` 0.288, `transition` 0.076) — genuinely blended, not a single 100% label | PASS |

**No blockers.** Every dependency Volume 4 needs from Volume 3, and every
Volume 4 component itself, is producing live data right now — not just
present in code.

## DEBT ledger check

**DEBT-1 and DEBT-2 are Volume 4's own central open items, not upstream
gaps — reconfirmed live, unchanged:**

- **DEBT-1** (directional intelligence is daily-only): reconfirmed live —
  `ms_breakout_probability`, `ms_structural_bias`, `ms_trend_direction` for
  HDFCBANK all still last-updated exactly `2026-07-16 00:00:00` (midnight),
  identical to the 2026-07-15 finding. **Not resolved, not degraded
  further** — same state as every prior check.
- **DEBT-2** (`IntradayRiskFeatureEngine` output unconsumed): consumer-wiring
  half still open (grep of `app/intelligence/` for
  `intraday_move_from_open_pct` or similar — not re-run this pass, inherited
  from today's Vol3 postflight which confirmed the producer side is now
  reliable; the *consumer* side is exactly what DEBT-1's fix would need to
  add).

Neither entry's expiry condition is triggered by *this preflight itself*
(no real trading decision is being made; this isn't Volume 5.5+ work) — so
neither is a blocker to a GO verdict. But they are the correct, and
essentially only, high-value target for whatever Volume 4 build chunk comes
next.

No other Active entries (DEBT-3/4/7/8/10/11) are triggered by Volume 4 work
— all are Volume 2/3/5 scoped or general perf debt already tracked
independently.

## Invariants check

**I-1 is VIOLATED, and it is Volume 4's own violation** — this is exactly
the scenario `prompts/1-preflight.md` warns about: "building signal logic on
top of I-1 while it's violated just deepens the hole." **Recommendation,
stated plainly rather than left implicit:** the next Volume 4 build chunk
should be DEBT-1's intraday-wiring fix (make trend/market_structure/
volatility/momentum/relative_strength read intraday timeframes, not layer
new D-only components on top of the current architecture). Building
anything else in Volume 4 first would be building on top of a known-broken
foundation rather than fixing it.

**I-2** remains VIOLATED via DEBT-2, same reasoning — the fix is coupled to
I-1's fix (both resolve together per DEBT-1/DEBT-2's cross-referenced
expiry conditions).

**I-3, I-4, I-9, I-11**: HELD, all directly reconfirmed today via the
Volume 3 postflight (no Volume 4-specific query/CPU changes to re-measure
this pass, since preflight makes no code changes).

**I-6**: HELD — nothing in this preflight touched any API contract.

No spec-vs-invariant contradiction: Volume 4's spec (Ch.1-23) never
mandates D-only timeframes explicitly — that was an implementation choice
made when Volume 4 was originally built, not a spec requirement, so fixing
DEBT-1 is squarely in-spec, not a spec conflict to flag and route around.

---

## GO verdict

Volume 3's foundation genuinely supports Volume 4 as built, and Volume 4's
own architecture is unusually thoroughly proven live for a retroactive
check: all 12 intelligence components, the Bayesian regime detector, regime
transitions, market confidence, composite scoring, explainability, and the
full Market State Report are all producing fresh output right now (most
within the last 5-25 minutes), not just present in code. The Historical
Analog Engine genuinely finds real historical analogs; regime probabilities
are genuinely blended, not hard-switched.

The one thing worth acting on before new Volume 4 scope is added: **DEBT-1
is Volume 4's own I-1 violation**, unchanged since 2026-07-15. Recommended
first build chunk for `/volume-build 4`: wire intraday (5m) timeframe reads
into trend/market_structure/volatility/momentum/relative_strength
intelligence, consuming `IntradayRiskFeatureEngine`'s already-reliable
output (DEBT-2's producer half) — resolving DEBT-1 and DEBT-2 together,
exactly as their expiry conditions already anticipate.
