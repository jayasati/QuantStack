# Volume 4 Postflight — Market Intelligence & Regime Analysis Engine (2026-07-16)

**Scope:** Decides completion for the cumulative system (Volumes 1-4
together, live on quantstack-vm, at production scale). Volume 4 was already
built (all 17 numbered prompts, plus two undocumented gap-fills —
`market_structure`/`momentum`, matching the same retroactive-gap-fill
pattern Volume 3's postflight found for Institutional Flow/Macro) before
this session's process existed. This postflight covers that existing build
plus today's DEBT-1/DEBT-2 work (intraday overlay across all 5 directional
components, `f597610` + `aac4956`).

Read: `prompts/INVARIANTS.md`, `prompts/DEBT.md`, the Volume 4 preflight
(`docs/volumes/preflight-vol4-2026-07-16.md`), and
`prompts/VERIFY-COOKBOOK.md`. Method: repo inspection + live SQL/API checks,
run 2026-07-16 ~22:30-23:10 IST (market closed).

**Verdict: COMPLETE-WITH-DEBT.** The core intelligence pipeline is
comprehensively proven live — 12 components, real historical analogs,
genuinely probabilistic (not hard-switched) regime states, a cumulative
behavior test showing the system respond to a real market move end to end.
One real, previously-undocumented gap found this pass: Explainability's
persisted-history read path (Ch.20's own distinguishing capability beyond
what every component already exposed live) has zero consumers anywhere,
despite the module's own docstring claiming it's exposed via API. Logged as
new debt below, not silently accepted.

---

## Spec coverage (`docs/volumes/volume-4.md`, Ch.1-23)

| Chapter | Status | Evidence |
|---|---|---|
| Ch.1 Why a Market Intelligence Layer | ✅ | Architectural principle; realized via Ch.2's pipeline |
| Ch.2 Architecture | ✅ | `CompositeMarketIntelligenceEngine.assess()` orchestrates all components concurrently (`asyncio.gather`, per-engine exception swallowing) — confirmed live: 12/12 components reporting for HDFCBANK |
| Ch.3 Regime Philosophy | ✅ | Every component's `states` is a probability distribution, never a single label — confirmed live: `probabilities.trend` for HDFCBANK genuinely blended across 4 states (0.32/0.29/0.32/0.08), not one at 100% |
| Ch.4 Regime Taxonomy | ✅ | Named regimes match the spec's vocabulary per dimension (`markup`/`markdown`/`accumulation`/... for structure; `extremely_low`...`extreme` for volatility; etc.) |
| Ch.5 Trend Intelligence (4.1) | ✅ | Live + intraday-overlay-verified this session (see behavior test below) |
| Ch.6 Volatility Intelligence (4.2) | ✅ | Live + intraday-overlay-verified this session |
| Ch.7 Breadth Intelligence (4.3) | ✅ | Live via composite (score 44.74, `available: true`) |
| Ch.8 Sector Intelligence (4.4) | ✅ | Live via composite + `sector_leaders` in state response (real names: IT/PSU Bank/Metal leading) |
| Ch.9 Institutional Flow Intelligence (4.5) | ✅ | Live via composite (score 44.83) + dedicated `/intelligence/institutional-flow` |
| Ch.10 Liquidity Intelligence (4.6) | ✅ | Live via composite + dedicated `/intelligence/liquidity/{symbol}` |
| Ch.11 Event Intelligence (4.7) | ✅ | Live via composite (score 78.01) |
| Ch.12 Correlation Intelligence (4.8) | ✅ | Live via composite (score 92.40) + dedicated `/intelligence/correlation`, `correlation_matrix` metric |
| Ch.13 Relative Strength Intelligence (4.9) | ✅ | Live-verified directly this session + intraday overlay added (`intraday_relative_references: ['nifty','sensex']` for HDFCBANK). Not a `CompositeMarketIntelligenceEngine` input — matches Ch.18's own spec, which never names Relative Strength among Composite's aggregate inputs; not a gap |
| Ch.14 Historical Analog Engine (4.10) | ✅ | Live: 20 real analogs for HDFCBANK, genuine similarity/return/drawdown/runup values (top: similarity 0.9578) |
| Ch.15 Bayesian Regime Detection (4.11) | ✅ | `regime_belief.update`: 8,053+ rows; `/intelligence/regime/trend/HDFCBANK/D` returns genuinely evolving history (3 consecutive snapshots with different probability distributions, not frozen) |
| Ch.16 Regime Transition Engine (4.12) | ✅ (inherited) | Wired into composite/confidence/report per `container.py`; not independently re-exercised this pass beyond confirming its output is consumed elsewhere — no new evidence contradicts the original build |
| Ch.17 Market Confidence Engine (4.13) | ✅ | `market_confidence.observation`: 12,223+ rows, live (grade A, score 81.27, trend stable for HDFCBANK) |
| Ch.18 Composite Market Intelligence Score (4.14) | ✅ | Live: `/intelligence/composite/HDFCBANK` — 12/12 components, 0-100 score (50.23), bullishness/bearishness/stability/opportunity/risk all present |
| Ch.19 Market State Report (4.15) | ✅ | `market_state_report.observation`: 12,195+ rows; persisted via the *scheduled* sweep (confirmed byte-matching a manual API call); every Ch.19-required field present in one live-fetched response |
| Ch.20 Explainability Layer (4.16) | ⚠️ **PARTIAL** | See finding below — write side works, persisted-history read side has zero consumers |
| Ch.21 APIs (4.17) | ⚠️ **PARTIAL** | All named endpoints present and live *except* explainability history (tied to Ch.20's gap — the module docstring's own claim "Prompt 4.17 exposes this over an API" is not true) |
| Ch.22 Dashboard Components | ✅ | `GET /dashboard/intelligence` → 200, 27.5KB, references real working endpoints for 8+ of the 10 named panels (analogs, breadth, composite, confidence, correlation, liquidity, regime/trend, regime/market_structure, regime/institutional_flow, sector) |
| Ch.23 Acceptance Criteria | ✅ 7/8 (1 partial, tied to Ch.20) | See below |

**Ch.23 criteria, individually:**
- Every feature contributes to a market intelligence component — ✅
- Regime classification supports probabilities, not hard labels — ✅
- Trend/volatility/breadth/liquidity/macro/sector/institutional-flow/correlation operational — ✅
- Historical analog search works on any snapshot — ✅
- Composite score generated every evaluation cycle — ✅ (confirmed via scheduled sweep)
- Market State Reports persisted and replayable — ✅
- **All intelligence outputs are explainable and available through APIs — ⚠️ PARTIAL.** Every component's *immediate* contributions/reasoning is available live through its own API response (genuinely satisfies "no black box" for a current read). The *persisted history* explainability — Ch.20's own distinguishing addition, including the confidence interval — is computed and stored (50,115+ rows and growing) but has zero API exposure.
- Downstream systems consume the Market State Report, not raw features — ✅ (`CandidateGenerationEngine` reads `report_as_of()`, confirmed from this session's context)

## New finding: Explainability's persisted history is write-only

`app/intelligence/explain.py`'s own docstring states: *"ExplainabilityStore
persists the full record... so a dashboard can query exactly how any past
score was constructed... Prompt 4.17 exposes this over an API."* Checked
directly: `ExplainabilityStore` is constructed and `.record()`-called from
exactly two places (`composite.py`, `prediction/opportunity.py`) — both
writers. Grepped the entire `app/api/` tree for any call to `.history()` or
`.latest()` (the only two read methods on the class): **zero matches.** The
docstring's own claim is false as of this check — not a stale comment about
something that was later removed, but a capability that appears to have
never been wired to begin with.

This is architecturally distinct from Ch.2's baseline explainability (every
`IntelligenceResult.contributions`/`.reasoning`, exposed live through every
component's own API response since Prompt 4.1) — that part is genuinely
fine and was re-confirmed this session on every single component checked.
What's missing is specifically the *historical* view and the *confidence
interval* Ch.20 added — "how did this score's explanation look an hour ago,
and how wide was the uncertainty band" — which is exactly the kind of
question a dashboard timeline or an incident review would need, and exactly
what 50,000+ rows of `explainability.observation` events currently can't
answer for anyone without a direct SQL query.

Logged as new debt below.

## Cumulative behavior test (Volumes 1-4, real data)

Reused this session's own live evidence from the DEBT-1/DEBT-2 build
(not re-derived, per the postflight's own instruction not to re-discover
established facts) plus one fresh check:

1. **Through Vol 1-3 (inherited):** raw ticks and 5m features continuous
   through today's full session, zero gaps — established in today's
   earlier Volume 3 postflight, not re-run here.
2. **Through Vol 4 — intelligence responds to a real market move, not
   frozen (the actual HDFCBANK 2026-07-15 regression test).** HDFCBANK
   declined a real -1.06% today. D-based trend evidence was bullish
   (`ms_trend_direction=1.0`, `ms_structural_bias=0.71`), but the live,
   *persisted* (scheduled-sweep, not manual) trend assessment shows
   `trend_direction=0.19`, confidence `0.49`, dominant state `range_bound`,
   with an explicit reasoning line naming the conflict. This is Volume 4
   correctly changing its read in response to real, current market
   behavior — the direct opposite of the failure this postflight process
   was built to catch.
3. **Regime belief history genuinely evolves.** `/intelligence/regime/trend/HDFCBANK/D`
   returned 3 consecutive persisted snapshots with visibly different
   probability distributions (`transition` 0.049 → 0.044 → 0.042,
   `range_bound` 0.559 → 0.601 → ...) — the Bayesian belief is actually
   updating over time, not static.

## Full regression + performance

- **Suite:** last full run (this session, no code changes since):
  **1265 passed**, 5 pre-existing failures in `test_market_scenarios.py`
  (Volume 5 work-in-progress, confirmed failing on clean HEAD, unrelated).
- **ruff / mypy:** clean on every file touched this session.
- **Deploy:** `aac4956` live, container healthy 15+ minutes, logs clean.
- **Latency — isolated (scheduler paused, CPU confirmed idle first):**
  5.3-5.5s, consistent with the post-DEBT-1-chunk baseline (5.5-6.8s). One
  intermediate reading (21-27s) was investigated and confirmed to be a
  post-restart artifact (a background job still finishing when the pause
  was issued) — re-measured clean after confirming `docker stats` showed
  genuinely idle CPU first. Not a regression from this session's work.

## Invariants reconciliation

| Invariant | Status | Note |
|---|---|---|
| I-1 (signal freshness) | VIOLATED, substantially improved | All 5 DEBT-1 components fixed and live-verified; still VIOLATED pending a genuine live-market-hours check (both chunks were built/verified after close, against the day's final snapshot only) |
| I-2 (producer→consumer) | VIOLATED, new instance found | DEBT-2's remaining 6 intraday features unchanged; **new**: Explainability's persisted history is also write-only (see finding above) |
| I-3, I-4, I-9, I-11 | HELD, reconfirmed | No new query patterns this session beyond what DEBT-1's build already measured; both deploys followed the full verified sequence |
| I-6 | HELD | All Volume 4 changes this session were additive (new optional params, new metrics fields) |
| I-7 | Unaffected | Volume 4's own roadmap claim is now backed by this postflight for the first time |

## Debt reconciliation

- **DEBT-1 → Resolved** (already moved, `2d23c63`).
- **DEBT-2** unchanged, still Active (6/9 intraday features unconsumed).
- **New: DEBT-12** — Explainability persisted-history read path
  (`ExplainabilityStore.history()`/`.latest()`) has zero callers anywhere;
  the module's own docstring incorrectly claims API exposure exists.
- DEBT-3/4/7/8/10/11 unchanged, outside Volume 4's own scope this pass.

---

## Verdict: COMPLETE-WITH-DEBT

Volume 4's intelligence pipeline is comprehensively proven live: 12
concurrently-orchestrated components, genuine probabilistic regime states,
real historical analog search, a full persisted-and-replayable Market State
Report, and — freshly built and verified this session — all 5 directional
components now responding to real intraday price action instead of sitting
frozen until the next midnight D bar. The one gap found by this postflight's
own scrutiny (DEBT-12, explainability history's missing read path) is a
real, previously-undocumented completeness issue, not a breakage of
anything currently load-bearing — logged, not fixed here per this phase's
rule against new feature work. Nothing found here blocks Volume 5 work from
proceeding.
