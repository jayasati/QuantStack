# Debt Ledger

Every deliberate deferral lives here — not in docstrings. Each entry has an
**expiry condition**: the concrete event that makes the deferral stop being
acceptable. Preflight checks whether the volume being started triggers any
expiry; seam-check and postflight re-verify the whole ledger against current
reality.

Rules:
- A docstring may *mention* a deferral, but this file is the record.
- An expired entry blocks new volume work until resolved or explicitly
  re-accepted by the user (re-acceptance = new expiry condition, noted here).
- Resolved entries move to the Resolved section with the fixing commit —
  they're the system's institutional memory.

---

## Active

### DEBT-1 · Directional intelligence is daily-only (4 of 5 components fixed)
**What:** All directional intelligence (trend, market_structure, volatility,
momentum, relative_strength) reads only `timeframe="D"` features, computed
once at midnight. Intraday moves are invisible to every signal.
**Why deferred:** Volume 4 was specced against daily features; wiring intraday
input is new scope.
**Risk while open:** The system cannot do the one thing it exists for
(Volume 1: intraday F&O same-day trading). HDFCBANK 2026-07-15 is the proof.

**Fixed 2026-07-16 for trend/market_structure/momentum/volatility** (Volume 4
build, DEBT-1/DEBT-2 chunk): a shared intraday-overlay convention
(`app/intelligence/base.py`: `intraday_direction_signal()`,
`intraday_reversal_warning()`, `IntelligenceComponent.intraday_values()`)
blends `IntradayRiskFeatureEngine`'s (Volume 3) 5m session-relative features
into each engine's direction/level/confidence with real weight (0.3 for
direction, dedicated confidence-conflict penalties), not a token gesture.
Verified live on the VM against HDFCBANK's real 2026-07-16 session (-1.06%
from open): D-based evidence was bullish (`ms_trend_direction=1.0`,
`ms_structural_bias=0.71`), but the blended trend read dropped to
`trend_direction=0.19`, confidence `0.49` (was structurally ~0.7+ pre-fix),
with an explicit reasoning line ("Today's intraday move opposes the
underlying read (conflict 48%) -- confidence docked") and dominant state
shifted from a confident bull read to `range_bound` -- this is the actual
HDFCBANK 2026-07-15 fix, live-verified, not just unit-tested. All 12
composite components (including the 4 fixed here) confirmed still reporting
successfully via `/intelligence/composite/HDFCBANK` post-deploy.

**Remaining scope: relative_strength.** Not fixed in this chunk -- Relative
Strength Intelligence is inherently a cross-symbol comparison (rs_* features
are already relative-to-benchmark quantities), and `IntradayRiskFeatureEngine`
has no equivalent relative/benchmark-comparison feature to overlay. Fixing
it would need a new intraday relative-strength computation (symbol's
intraday move vs. each reference's own intraday move), not a reuse of the
existing overlay helpers -- explicitly deferred, not silently skipped.
**Expiry condition:** Relative Strength intraday wiring -- before any signal
is used for a real trading decision, OR when Volume 5.5+ work resumes,
whichever comes first (unchanged for the remaining scope).
**Logged:** 2026-07-15; 4/5 components fixed 2026-07-16
(`f597610`, `docs/volumes/preflight-vol4-2026-07-16.md`).

### DEBT-2 · IntradayRiskFeatureEngine output is unconsumed (3/9 features now fixed; root cause of the stall found: external, not ours)
**What:** Volume 3's `IntradayRiskFeatureEngine` writes real 5m-timeframe
features (`intraday_move_from_open_pct`, `intraday_expected_move_next_30m_pct`,
…) that no Volume 4/5 code reads (violates I-2) — see 2026-07-16 update below,
6 of 9 base features (plus their `_z` companions) are still unconsumed.

The separate stall (last write 10:25 IST → 11:30 IST → confirmed still
stuck across the Volume 2 and 3 preflights) was root-caused 2026-07-15:
**not a bug in this codebase.** `HistoricalCandleCollector.collect()`'s two
early-continue paths had zero logging (fixed, `79a067f` — now warns with
symbol/interval/requested range on an empty broker response). Once
deployed, live investigation isolated the actual cause: `raw_ticks`
(WebSocket live feed) stayed perfectly fresh (~5s old) throughout, while
`ohlcv_candles` (REST `getCandleData`) was ~5.5h behind real time.
Three manual `/collectors/historical_candles/run` triggers ~90s apart each
advanced the 1m candle by exactly ~1 minute — Angel One's own candle-
aggregation backend was actively catching up, just from a badly backlogged
starting point, at roughly real-time pace (i.e., not stuck, but also not
going to fully catch up same-day at that rate). Circuit breaker never
tripped, zero exceptions anywhere in this chain — purely an upstream
broker-side backend degradation, outside this codebase's control.
**Update 2026-07-16:** a real-time tick-aggregation layer now builds and
live-updates 1m/3m/5m/15m/30m/1H candles directly from the same ticks
`live_market` already polls every 15s, instead of waiting on this
collector's 300s external-source sweep to notice new data exists (see the
Resolved entry below). This doesn't resolve DEBT-2 itself (consumer-wiring
gap, DEBT-1, is still open) but substantially reduces exposure to a repeat
of the original stall -- intraday freshness no longer depends solely on
this collector's own cadence, and the NSE/BSE fallback that was DEBT-2's
original workaround is now last-resort rather than primary.

**Risk while open:** Any future intraday-intelligence work (DEBT-1) must
treat `ohlcv_candles` freshness as a genuine external dependency that can
silently degrade for hours, not an assumption — the eventual fix should
include a staleness check (e.g., compare latest candle ts to now, downgrade
confidence or flag the signal when the gap exceeds a threshold) rather than
trusting whatever's in the table. The logging fix means a recurrence is now
visible in logs the moment it happens, not discoverable only by manual query.

**Partial mitigation added 2026-07-15:** `HistoricalCandleCollector` now
tries NSE's and BSE's own public quote-page tick feeds *before* the broker,
for today-only intraday windows (the exact window the stall hit), falling
through to the broker then to Yahoo Finance on any exception or empty
result — `app/collectors/sources/{nse_candles,bse_candles,candle_aggregate}.py`,
`yahoo_history.fetch_intraday`, `HistoricalCandleCollector._fetch_with_fallback`.
This reduces exposure to a repeat of this exact stall (an independent source
now stands in when the broker's candle pipeline lags) but does **not**
resolve DEBT-2 itself — the consumer-wiring gap and the staleness-check
recommendation above are both still open. Multi-day backfill is unaffected
(NSE/BSE only ever expose today's session, verified live 2026-07-15 — see
those modules' docstrings for the dead-endpoint findings that motivated the
today-only scoping).
**Consumer-wiring: partially resolved 2026-07-16** (same chunk as DEBT-1).
Of `IntradayRiskFeatureEngine`'s 9 base features, 3 now have real consumers:
`intraday_move_from_open_pct` (trend, market_structure, momentum, all via
`intraday_direction_signal()`), `intraday_current_drawdown_pct` (trend,
market_structure, via `intraday_reversal_warning()`), and
`intraday_realized_vol_pct` (volatility). The remaining 6 --
`intraday_time_elapsed_pct`, `intraday_max_drawdown_pct`,
`intraday_expected_move_next_30m_pct`/`intraday_var95_next_30m_pct`,
`intraday_expected_move_rest_of_session_pct`/`intraday_var95_rest_of_session_pct`
-- (plus all 9 base features' `_z` companions) are still write-only, an I-2
violation. The staleness-check recommendation above (flag/downgrade
confidence when `ohlcv_candles` is stale) also remains unimplemented.
**Expiry condition:** Remaining consumer-wiring resolves with relative_strength's
piece of DEBT-1, or when the still-unconsumed expected-move/VaR features are
next relevant (a natural fit for Volume 5's conviction/qualification engines,
which already reason about position-holding horizons). The staleness-check
recommendation should land as part of whichever fix lands next, not deferred
again a second time.
**Logged:** 2026-07-15 (root-caused same day, `79a067f` +
`docs/volumes/preflight-vol3-2026-07-15.md`; fallback-chain mitigation added
same day; consumer-wiring partially resolved 2026-07-16, `f597610`).

### DEBT-3 · No outcome evaluator / win-rate metric
**What:** Candidates carry `valid_until` but nothing records whether price
moved as predicted once the window closes. No accuracy number exists anywhere.
**Risk while open:** Signal-quality failures (like a permanent long bias) are
only discoverable by a human staring at a chart.
**Expiry condition:** Before any signal is used for a real trading decision.
Recommended as the very next build — it is the acceptance test for every
future intelligence change.
**Logged:** 2026-07-15.

### DEBT-4 · Market-wide events.score triggers candidacy per-symbol
**What:** `events.score > 40` (a market-wide value) fires the
`event_driven_opportunity` trigger for every watchlist symbol at once,
producing candidates with priority_score 0.00 and directions inferred from
unrelated evidence (2026-07-15: NIFTY/BANKNIFTY/SENSEX/RELIANCE all "short",
priority 0.00, identical supporting evidence).
**Expiry condition:** When DEBT-3's evaluator exists and shows these
candidates' win rate is noise, or when candidate quality is next worked on.
**Logged:** 2026-07-15.

### DEBT-7 · Signal generation misses Volume 1's own <2s target
**What:** `/prediction/candidates` measured ~2.2s steady-state live
(2026-07-15, post all perf fixes) vs. Volume 1 §16's <2s target — a ~10%
miss. Not a defect introduced by any single volume; later volumes' scope
grew past what Volume 1 budgeted for.

**Root-caused 2026-07-16 -- the 2.2s baseline was stale, real miss was
6-10x, not 10%:** `OpportunityDetectionEngine.scan()` fans out `detect()`
to every watchlist symbol via a bare `asyncio.gather`, no cap -- each
`detect()` itself fans out ~6 fresh per-symbol intelligence assessments.
The 2.2s figure was measured against the original 3-symbol watchlist
(~18-24 concurrent calls); the watchlist grew to 25 symbols later the same
day (see the Resolved "Watchlist expansion" entry) with nothing here
adjusted for it. Live-measured afterward: 12.2s / 23.2s / 14.2s / 6.8s
across four requests, container CPU pegged 90-95% on 4 vCPUs during a
request (Postgres only 17-40% -- confirms CPU-bound application work, not
DB wait). Exactly the same class of bug `MAX_CONCURRENT_SNAPSHOT_CAPTURES`
was fixed for on 2026-07-14 (`CandidateGenerationEngine.generate()`'s
*second* phase, snapshot capture for the top 20 ranked candidates) -- that
fix never covered `scan()`'s *first* phase (ranking the full watchlist
before truncating to 20), because `scan()` didn't have a scaling problem
until today's watchlist change created one. Fixed the same way:
`MAX_CONCURRENT_SYMBOL_DETECTIONS = 5` semaphore bounds `scan()`'s
per-symbol fan-out, independent of how wide the watchlist grows next.
**Re-measured live post-deploy, same day: improved, not resolved.**
6.1s / 6.6s / 9.2s / 5.9s across four requests -- 2-3x faster and far more
consistent (the 23s outlier is gone), but still 3-4.5x over the <2s
target, not fixed. Expected: 25 symbols / `MAX_CONCURRENT_SYMBOL_DETECTIONS=5`
= 5 sequential waves through Phase 1 -- the semaphore trades system-wide
CPU contention for wall-clock time, it doesn't reduce the total work.
`scan()` still runs the full 6-way intelligence assessment on *every*
watchlist symbol just to rank them, before truncating to the top 20 --
that cost scales directly with watchlist size regardless of concurrency
tuning. Left **Active**, not moved to Resolved.
**Pre-filter added 2026-07-16 -- correctness-preserving, not a heuristic:**
of `detect()`'s "6-way fan-out", only 4 (trend, market_structure,
institutional_flow/relative_strength/volatility, events) actually feed
`evaluate_triggers()` -- confirmed by reading its real inputs.
`market_confidence` and `composite_score`/`composite_confidence` are pure
display metadata attached to `OpportunityCandidate`, never read by trigger
evaluation, but were being computed for every symbol regardless of whether
it triggered. Deferred both until after `if not triggers: return None` --
zero behavior change for symbols that do trigger (identical values,
computed later), and the ~2 reads are skipped entirely for symbols that
don't (the majority in practice). This is a real correctness-preserving
cut, not the "lightweight score gate" heuristic floated below -- that
would trade recall for speed and wasn't implemented; explicitly deferred,
see below.
**Correction, same day -- the first pre-filter version was a live
regression, caught by re-measuring rather than assumed fixed:** deferring
`market_confidence`/`composite_context` until *after* the trigger check
(instead of starting them early like the original code) made
`/prediction/candidates` *slower*, not faster -- 10.7s-14.4s post-deploy,
worse than the 6.1-9.2s pre-filter-less baseline. Root cause: the two
tasks used to start via `asyncio.ensure_future` *before* the main 6-way
`asyncio.gather`, running concurrently with it -- most of their cost was
already hidden behind that gather's own wall-clock time. Deferring them to
start only after the gather completed lost that overlap. Checked live
(`/prediction/opportunities`) rather than assumed: 17 of 25 watchlist
symbols were triggering at the time (68%, not the small minority the fix
assumed), so most requests paid the full serial cost on top of the main
gather instead of getting it for free. Fixed properly: both tasks are
started early again (restoring the original overlap for triggering
symbols), but are now `.cancel()`'d -- not left to run to completion --
the moment a symbol turns out not to trigger, recovering the savings for
non-triggering symbols without re-introducing the regression for
triggering ones. **Re-measured live post-deploy (market closed, 17:35
IST): 6.3s / 5.7s / 6.3s / 6.1s across four requests** -- regression
confirmed gone, back in line with the concurrency-bound-only baseline
(6.1-9.2s), not further improved beyond it in this measurement. Still
3-4x over the <2s target; DEBT-7 stays Active.
**Expiry condition:** Before citing Volume 1's performance target as met
anywhere, or when request latency is next worked. The 4 real per-symbol
engines (trend, market_structure, relative_strength, volatility) still run
for every watchlist symbol regardless -- that's the remaining, larger
cost. **Correction 2026-07-16:** this entry previously pointed at DEBT-6
(thin Redis coverage) as the likely next lever -- DEBT-6 is now Resolved
(97.4% cache hit rate, all 25 symbols have a populated `:D` key), and
DEBT-7 is *still* 3-4x over target, so Redis population was never the
bottleneck here. The remaining cost is genuine per-symbol computation
inside each engine's `assess()` (scoring/aggregation over already-cached
feature values), not cache-miss latency. Closing it for real likely means
either optimizing that computation itself, or accepting a genuine
heuristic pre-filter (with an explicit recall/speed trade-off the user
should sign off on, not one invented silently) rather than assessing
every symbol in full. **Note 2026-07-16:** DEBT-9's resolution hit the
same 4-vCPU contention class from a different angle -- the new
`features.selection_sweep` job measurably degraded this same endpoint's
latency (to 36-46s) for the ~3min it runs, gated to after-hours only since
selection has no reason to run mid-session. Confirms this box's capacity
ceiling is a recurring constraint across unrelated schedulers, not
specific to `scan()`. **New data point, Volume 3 postflight, 20:10 IST:**
an unrelated background-contention spike (43.4s/25.2s, `docker stats`
confirmed 100% CPU; confirmed NOT the selection sweep, which wasn't due to
fire again until the next day) -- worse than any previously recorded
figure here. Isolated (scheduler paused) request-path latency measured
clean at 4.57-4.93s immediately after, confirming the app itself hasn't
regressed; the ceiling is entirely background-job contention, still
unresolved. **Volume 4 DEBT-1/DEBT-2 chunk, 2026-07-16:** isolated latency
after wiring the intraday overlay into trend/market_structure/momentum/
volatility measured 5.5-6.8s (was 4.57-4.93s just before) -- each of the 4
engines now does one additional bounded `FeatureStore.latest()` read per
symbol (Redis-first, same proven-cheap call already used elsewhere). A
real, small addition, within this entry's existing 6.1-9.2s reference
range, not a new regression -- not chased further here since it doesn't
change DEBT-7's own diagnosis (per-symbol `assess()` computation, not
cache-miss latency, is still the dominant remaining cost).
**Logged:** 2026-07-15 (Volume 1 postflight); root-caused and partially
mitigated 2026-07-16 (concurrency bound + cancel-based pre-filter, both
live-verified insufficient alone -- see re-measurement notes above; the
pre-filter's first version was a live regression, corrected same day
before this ledger entry was written).

### DEBT-8 · news_intelligence / global_shock_news chronically slow, low quality
**What:** Live `/collectors` check: `avg_latency_ms` 33,696 (news_intelligence,
120s interval) and 36,444 (global_shock_news, 30s interval) — both take
longer than their own scheduled interval to complete. `collector_health`
quality scores sit at ~22-23/100 (occasional spikes to ~62), consistent since
at least 05:30 IST — predates and is unrelated to this session's FinBERT
cross-collector lock (checked and ruled out). Likely CPU-bound FinBERT
inference cost on a shared 4-vCPU box; root cause not yet investigated
further.
**Risk while open:** A 30s-interval collector that takes 36s can never catch
up to its own schedule — news-driven signals (including the
`event_driven_opportunity` trigger, DEBT-4) run on data that's structurally
always behind.

**Root-caused 2026-07-16:** confirmed live (`docker stats`: container
pegged at ~100% CPU on 4 vCPUs during a run; exactly 2 "loading finbert
sentiment model" log lines total since container start, so NOT repeated
per-cycle reloading). Three contributing factors:
1. **Genuine capacity ceiling, not fixable by code alone:** `news_intelligence`
   fetches up to ~200 articles/cycle (4 RSS feeds × `MAX_ARTICLES_PER_FEED=50`),
   `global_shock_news` up to ~120+ (2 fixed feeds + 8 `GOOGLE_NEWS_QUERIES` ×
   `MAX_ARTICLES_PER_QUERY=15`). FinBERT (BERT-base) CPU inference, capped
   to 3 threads (`cpu_count - 1`, deliberately, to leave a core free — this
   part is correct), genuinely costs 30-45s at that volume. A 40s job can
   never fit a 30s clock regardless of tuning.
2. **Fixed, real waste:** scoring ran on the full raw article batch
   *before* the near-duplicate filter discarded matches — `global_shock_news`'s
   8 topically-overlapping queries ("Iran Israel war" / "US Iran conflict" /
   "Russia Ukraine war" / ...) routinely return the same breaking story more
   than once, so CPU was being spent scoring articles thrown away a moment
   later. `NewsIntelligenceCollector.collect()` now dedups first, scores
   only the survivors.
3. **Fixed, real waste:** `news_intelligence` and `global_shock_news` each
   called `_default_sentiment_provider()` independently, so each loaded its
   own separate ~440MB FinBERT copy — despite every scoring call from
   either collector already serializing through the shared
   `_finbert_scoring_lock`, so there was never a concurrent-access reason to
   keep them apart. `_default_sentiment_provider()` is now a module-level
   singleton, one model shared by both.

Factors 2/3 reduce wasted CPU and memory but do not resolve factor 1 —
`global_shock_news`'s 30s interval remains structurally too tight for its
own article volume. Cutting `MAX_ARTICLES_PER_QUERY`/query count, or
accepting a longer interval, would be the next lever if this still isn't
fast enough after the above.

**Re-checked live 2026-07-16 evening (post-market-close, light news
flow):** `news_intelligence` avg_latency_ms down to ~3,524 (from ~42,000),
`global_shock_news` down to ~2,933 (from ~47,000) — both comfortably
within their own interval budgets, latency quality scores 98.85/99.55.
**Not enough to call this resolved:** both runs found zero new articles
(`last_run_collected: 0`), so this confirms factors 2/3's fixes are
working but does NOT confirm factor 1 (the genuine BERT-inference cost at
a large article batch) survives a real high-volume moment. Left
**Active** deliberately — plan is to re-check live during market hours
tomorrow (2026-07-17), when a heavier/breaking news cycle is more likely
to actually exercise a large batch, before considering this resolved.

**Re-checked live again 2026-07-16, ~20:08 IST (incidental, surfaced during
the Volume 3 postflight, not the planned market-hours check):** regressed
back to `news_intelligence` quality 34.14 / avg_latency_ms 13,288 and
`global_shock_news` quality 33.73 / avg_latency_ms 5,771 — worse than the
98.85/99.55 figures recorded a few hours earlier the same evening. Confirms
this genuinely fluctuates with article volume/content even outside market
hours (not just a market-hours-only problem), consistent with factor 1
(BERT-inference cost scales with batch size) rather than a regression in
factors 2/3's fixes. Not investigated further this pass — out of scope for
the Volume 3 postflight that surfaced it; folded into the existing planned
market-hours re-check rather than treated as a new finding.
**Expiry condition:** When Volume 2 collector work or news/event-driven
signal quality is next worked on.
**Logged:** 2026-07-15 (Volume 2 preflight,
`docs/volumes/preflight-vol2-2026-07-15.md`); root-caused and partially
fixed 2026-07-16; re-checked evening (good) and again ~20:08 IST
(regressed) same day; market-hours re-check still planned for 2026-07-17.

### DEBT-10 · Feature Versioning has never been exercised beyond v1
**What:** `feature_versions` and the versioning mechanism (Ch.6: "Never
overwrite a feature -- publish successive versions, e.g. VWAP_v1 -> v2 ->
v3") are real and correctly wired, but `SELECT count(DISTINCT version)
FROM feature_versions` = **1** across all 1075 registered features --
every one has only ever been `"v1"`. No feature's calculation has ever
changed in a way that triggered a version bump, so the "models pin to a
specific version" guarantee (Ch.6) has zero live evidence of working
beyond the trivial single-version case.
**Risk while open:** Low -- nothing downstream currently needs multi-
version pinning. But it's an explicit, named spec capability with no
proof it functions when actually exercised (same shape of gap as DEBT-9
before this session, just lower stakes).
**Expiry condition:** When any feature's calculation logic changes (a
version bump becomes real), or when model/feature version pinning is
next worked on.
**Logged:** 2026-07-16 (Volume 3 postflight,
`docs/volumes/postflight-vol3-2026-07-16.md`).

### DEBT-11 · 85 registered features have never received a quality score
**What:** `feature_quality` has 169,742 live rows, but only 990 of 1075
registered features (92%) have ever appeared in it. The 85 never-scored
features concentrate in `institutional_flow` (35), `liquidity` (28),
`breadth` (12), `structure` (9), `events` (1).
**Risk while open:** Ch.27's own acceptance criterion ("every feature has
a quality score") isn't fully met. Plausibly explained by index symbols
(NIFTY/BANKNIFTY/SENSEX) legitimately lacking the underlying volume/
liquidity/institutional-flow data by design (documented pattern
elsewhere in this project) -- but this was **not independently confirmed**
this pass, so treat the explanation as a hypothesis, not a verified cause.
**Expiry condition:** When feature quality coverage is next investigated,
or before claiming 100% quality-score coverage anywhere.
**Logged:** 2026-07-16 (Volume 3 postflight,
`docs/volumes/postflight-vol3-2026-07-16.md`).

---

## Resolved

### ~~DEBT-9: Feature Selection Engine has never run live~~ — resolved 2026-07-16
`feature_usage` was empty (0 rows) despite `FeatureSelectionEngine.persist()`
being correct code -- reachable only via `POST /features/selection/{symbol}`,
never scheduled in `main.py`. Scheduling it live (`features.selection_sweep`,
`main.py`) surfaced two real bugs neither on-demand single-symbol testing nor
the small-column unit tests ever would have:

1. **O(features^2) redundancy scan.** `select_features()`'s pairwise-
   correlation pass compared every stored feature against every other
   before ever truncating to the `MODEL_CANDIDATES` (20) actually used
   downstream. Measured live against real HDFCBANK/D data (781 stored
   features): **12.5s of CPU per symbol.** Fixed with an early exit: once
   `MODEL_CANDIDATES` MI-ranked survivors are found, nothing later in the
   list can ever enter `candidates` regardless of its own redundancy status,
   so the scan stops. Verified correctness-preserving against the exact
   live matrix pulled from the VM (`recommended`/`ranking` byte-identical
   before/after; only `redundant`/`correlated_pairs` shrink, from an
   already-partial 175 pairs to 61 -- expected, matches their existing
   bounded nature). 2.7s -> 0.7s on that fixture locally. Wrapped the
   remaining cost in `asyncio.to_thread` (I-4) -- not trivial even after
   the fix, same convention as trend/volatility/correlation/analogs.
2. **feature_usage's unique constraint was `(feature_name, consumer)` only**
   -- symbol/timeframe lived inside JSONB `data`, not real columns.
   Reproduced live: selection across 5 watchlist symbols x 10 features each
   produced only 48 rows, not 50 (HDFCBANK's `volume_mfi_50`/`price_alpha_50`
   silently overwritten by ICICIBANK's/TCS's). Migration 0006 adds real
   `symbol`/`timeframe` columns and widens the edge to
   `(feature_name, consumer, symbol, timeframe)`. Live-verified at full
   scale post-fix: 25 symbols x 10 features = exactly 250 rows, 250
   distinct edges, zero collisions.
3. **Scheduling contention with the request path (cross-reference DEBT-7).**
   The first live run (even post-fix) still took ~3min of real CPU across
   the full 25-symbol watchlist and measurably degraded
   `/prediction/candidates` from its ~4.7s steady state to 36s/10.7s/14.6s
   while running -- the same CPU-contention class DEBT-7 documents on this
   4-vCPU box. `feature_selection_interval` (21600s) divides 24h exactly,
   so an unguarded interval trigger would hit the same time-of-day daily,
   inside market hours, forever. Fixed by skipping the sweep entirely via
   `is_nse_market_open()` (the same check collectors' `after_hours_only`
   gate uses) -- there's no upside to running mid-session anyway, since
   selection reads `timeframe="D"`, which only updates once/day at
   midnight (DEBT-1). Re-verified live after this fix: the after-hours run
   still costs the same ~3min/CPU-contention hit (46s/14.8s measured
   mid-sweep) but resolves on its own once the sweep completes (steady
   state ~4.5-5.0s confirmed after) -- an acceptable, bounded, low-frequency
   (3 of 4 daily ticks) cost for a non-trading-hours window, not something
   this chunk needed to eliminate entirely.

New consumer added per I-2: `GET /features/usage/{symbol}` (feature_usage's
first real read path, matching quality/drift's existing history-endpoint
pattern) -- feature_usage is no longer a write-only table.
**Logged:** 2026-07-15 (Volume 3 preflight); resolved 2026-07-16
(`c56140f`, `a882d14`, migration 0006,
`docs/volumes/preflight-vol3-2026-07-16.md`).

### ~~DEBT-6: Redis online-store coverage is partial~~ — resolved by usage, 2026-07-16
Not fixed by any code change -- the caching wiring was already correct
when this was logged (2026-07-15), confirmed by reading
`FeatureStore._write_online`/`.latest()`/`.refresh_online_ttl()` in
`app/features/store.py`: Redis-first read with Postgres fallback,
merge-not-overwrite on write (several engines share one key per
symbol/timeframe), and a prior fix (`94d8eb5` era) that re-extends a key's
TTL on a no-op run so a feature slower than `online_ttl_seconds` doesn't
silently fall out of the cache. What was actually thin was *population* --
measured 2026-07-15 against a 3-symbol watchlist with comparatively little
elapsed runtime, so there simply hadn't been enough write activity yet to
fill the cache, not because anything was broken.

Re-checked live 2026-07-16, after the watchlist expansion (3 -> 25
symbols) and several more hours of normal operation: **97.4% cache hit
rate** (9,756 hits / 261 misses via `/collectors/cache/metrics`), **all 25
watchlist symbols plus MARKET have a populated `:D` key** (the exact gap
originally called out), 181 total Redis keys (up from ~21), healthy TTLs
(~3400s of the 3600s window, recently refreshed). The "intelligence read
path effectively always falls through to Postgres" claim in the original
entry no longer holds.

Cross-referenced from DEBT-7 (still Active) as "populating Redis is the
likely next win" -- corrected there too: DEBT-7 is still 3-4x over its
<2s target even with DEBT-6 resolved, so Redis population was never
DEBT-7's actual bottleneck. Its remaining cost is genuine per-symbol
computation inside each engine's `assess()`, not cache-miss latency.
**Logged:** 2026-07-15; re-measured and resolved 2026-07-16 (no code
change -- cold-cache artifact of an early measurement, not a defect).

### ~~DEBT-5: CI is broken~~ — resolved by decision, 2026-07-15
Not fixed — **removed on purpose, a second time.** Found non-functional
(0/5 runs execute, no runner ever assigned) via the Volume 1 postflight;
investigation traced it to a billing lock that had already caused CI to be
deliberately removed once before (`fee0703`), then silently re-added four
days earlier (`edcfabf`) to address an audit finding without checking that
prior decision. Re-removed (`.github/workflows/backend-tests.yml` deleted)
and made permanent, documented policy: solo contributor, no CI billing,
full local test suite (+ ruff + mypy) before every push is the actual and
sufficient safety net — see `docs/volumes/volume-1.md` §18 and
`docs/backlog.md`'s Volume 1 section, which now explicitly warns against a
third re-add without checking there first. I-11 revised to match (see
INVARIANTS.md). Canonical example of why decisions need a durable,
loudly-flagged record — a comment in one file wasn't enough to survive one
audit prompt not reading it.

### ~~Several NSE-session-bound collectors ran 24/7 with no market-hours gate~~ — resolved 2026-07-15
`BaseCollector` already had a proven `market_hours_only` gate (`is_nse_market_open()`,
short-circuits `run_once()` before any DB/network call) used correctly by
`market_breadth`/`options_intelligence`/`sector_rotation`, but it was never
applied consistently. Audit found: `live_market` (15s interval, quotes/LTP
that freeze the instant the market closes — ~4,200 pointless broker
round-trips/night), plus `historical_candles`/`vix`/`reference_indices`
(same family just fixed the same day — less wasteful thanks to
`_backfill_start`'s resume-tracking, but still 175 DB round-trips every 5
min all night for zero new data). Separately, `nse_delivery` and
`institutional_flow` poll hourly for data (bhavcopy, FII/DII reports) that
only ever publishes end-of-day — the *opposite* problem, most wasted runs
were the daytime ones checking for a file that provably wasn't published
yet. Fixed: added `market_hours_only = True` to the first group, added a
new symmetric `after_hours_only` gate (`app/collectors/base.py`) to the
second group, and closed a related gap found along the way —
`is_nse_market_open()` only checked weekday + time-of-day, so a weekday
exchange holiday (e.g. 2026-03-04) still counted as "open"; it now also
checks `feature_market_holidays`.
**Logged:** 2026-07-15.

### ~~Candle freshness capped at HistoricalCandleCollector's 300s sweep~~ — resolved 2026-07-16
New capability, not a bug fix: `app/collectors/tick_aggregator.py`'s
`TickCandleAggregator` builds and live-updates 1m candles directly from
the ticks `live_market` already polls every 15s (or streams via
WebSocket), instead of relying solely on `historical_candles`'s 300s
external-source sweep to notice new data. 3m/5m/15m/30m/1H are re-derived
by folding the stored 1m bars on every ingest cycle, not tracked as
separate state -- always fresh, self-healing if a tick was missed. D and
anything older than 1m's own 2-day retention still comes from
`historical_candles` (broker/Yahoo deep backfill) -- ticks alone can't
produce 2 years of daily history.

Two layers, deliberately: this layer uses `ON CONFLICT DO UPDATE` (the
forming candle should visibly live-update, not just appear once its
minute closes -- 2026-07-16 decision); `historical_candles` keeps
`DO NOTHING` so it can never clobber this layer's more current data, and
remains the gap-filler for restarts/WebSocket drops.

**Correction, same day:** "NSE/BSE demoted to last-resort" was the stated
intent but the source-list reorder was never actually implemented in the
first version -- `_fetch_with_fallback` still tried NSE/BSE FIRST for
today-only windows, unchanged. Caught live during market hours: NIFTY and
BANKNIFTY (the two symbols using NSE's *index* chart endpoint,
`getGraphChart` -- not the equity endpoint) showed candles timestamped
hours in the future (e.g. 14:45 IST bars while real time was 10:26 IST).
Root cause: that endpoint forward-pads its response with placeholder bars
for the rest of the session instead of only what's actually traded so
far -- SENSEX (BSE-routed) and every equity (NSE's *different*,
non-padding `getSymbolChartData` endpoint) were unaffected. Fixed properly
this time: `_fetch_with_fallback`'s source order is now broker → Yahoo →
NSE/BSE (genuinely last-resort), plus a new `_drop_future_candles` guard
that filters any bar timestamped after the actual fetch time regardless
of source -- defense in depth, since a source lying about "now" is a bug
class that could recur elsewhere. 66 already-stored bad rows deleted
(`ohlcv_candles WHERE symbol IN ('NIFTY','BANKNIFTY') AND ts > now()`).

First version measured 111ms/symbol against a real Postgres in
`test_load_and_performance.py` (one upsert + 5 SELECT/upsert pairs *per
symbol* -- Volume 1 §16 targets <100ms) -- rewritten to batch across all
symbols in a cycle into a fixed ~11 SQL statements total regardless of
symbol count, not 11 × N.

Paired with a new `CandleRetentionCollector`
(`app/collectors/domains/retention.py`, `after_hours_only`, hourly):
nothing had ever pruned `ohlcv_candles` before this -- it now deletes rows
per interval older than that interval's own `HistoricalCandleCollector
.default_lookback` window (the same table already used for fetch sizing,
not a second copy of the same numbers).
**Logged:** 2026-07-16.

### ~~Watchlist expansion silently no-op'd on the VM~~ — resolved 2026-07-15
Expanded `Settings.watchlist` from 3 indices to a 25-symbol basket in
`app/core/config.py` (`fcd3da4`), deployed it, and confirmed live that only
6 symbols were actually being backfilled — including one (ICICIBANK) that
sat between two symbols that WERE working, ruling out "just slow." Traced
via a direct in-container probe (`HistoricalCandleCollector().initialize()`
→ `len(tokens) == 6`, `"ICICIBANK" in tokens == False`) to `configs/
default.yaml`, a git-tracked file `Settings.model_config` loads as a config
source that still hardcoded the old 6-symbol watchlist and 3-entry
`feature_stock_sectors` — I edited `config.py`'s Python-level defaults
without checking whether a second, higher-priority config source existed
and needed the same edit. Not deployment drift — the file was tracked and
in-repo the whole time, I simply didn't look for it. Fixed by syncing
`configs/default.yaml` to the same 25 symbols / 19 sector mappings.
**Lesson for future config changes:** `Settings` in this codebase has THREE
layered sources (code defaults, `configs/default.yaml`, `.env`/environment)
per `app/core/config.py`'s docstring/comments — a change to one is invisible
in practice unless checked against the others. Grep `configs/*.yaml` for
the field name before trusting a `config.py` default-value edit is live.
**Logged:** 2026-07-15.

### ~~report.py "accepted v1 redundancy"~~ — resolved 2026-07-15
Market Confidence's internal re-runs and per-symbol market-wide recomputation,
deferred in a docstring that went stale when snapshot capture entered the
request path. Fixed by threading precomputed results through
(52d9d95, 78cbd3c). Kept here as the canonical example of why deferrals need
expiry conditions.

### ~~Unbounded DISTINCT ON in FeatureStore.latest()~~ — resolved 2026-07-15
Introduced in fa1bc2b (passed all tests), regressed production 6.5s → 11-42s,
fixed with a 14-day window bound in 94d8eb5 after live EXPLAIN ANALYZE.
Canonical example of why I-3 requires measurement at live scale.
