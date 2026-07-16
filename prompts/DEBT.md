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

### DEBT-1 · Directional intelligence is daily-only
**What:** All directional intelligence (trend, market_structure, volatility,
momentum, relative_strength) reads only `timeframe="D"` features, computed
once at midnight. Intraday moves are invisible to every signal.
**Why deferred:** Volume 4 was specced against daily features; wiring intraday
input is new scope.
**Risk while open:** The system cannot do the one thing it exists for
(Volume 1: intraday F&O same-day trading). HDFCBANK 2026-07-15 is the proof.
**Expiry condition:** Before any signal is used for a real trading decision,
OR when Volume 5.5+ work resumes — whichever comes first.
**Logged:** 2026-07-15.

### DEBT-2 · IntradayRiskFeatureEngine output is unconsumed (root cause of the stall found: external, not ours)
**What:** Volume 3's `IntradayRiskFeatureEngine` writes real 5m-timeframe
features (`intraday_move_from_open_pct`, `intraday_expected_move_next_30m_pct`,
…) that no Volume 4/5 code reads (violates I-2) — this half is still open.

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
**Expiry condition:** Consumer-wiring half resolves with DEBT-1. The
staleness-check recommendation above should land as part of that same fix,
not deferred again.
**Logged:** 2026-07-15 (root-caused same day, `79a067f` +
`docs/volumes/preflight-vol3-2026-07-15.md`; fallback-chain mitigation added
same day).

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

### DEBT-6 · Redis online-store coverage is partial
**What:** TTL refresh on unchanged runs was fixed (94d8eb5 era), but coverage
is still thin — during the 2026-07-15 checks Redis held ~21 keys with zero
`:D`-timeframe keys, so the intelligence read path effectively always falls
through to Postgres. Cache wiring is correct; population is the gap.
**Expiry condition:** When request latency next needs improvement (it's the
biggest remaining lever from the 2026-07-14 perf audit, items verified but
under-delivering for exactly this reason).
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
anywhere, or when request latency is next worked (pairs naturally with
DEBT-6 — populating Redis is the likely next win). The 4 real per-symbol
engines (trend, market_structure, relative_strength, volatility) still run
for every watchlist symbol regardless -- that's the remaining, larger
cost, and closing it for real likely means either populating Redis
properly (DEBT-6) so each of those 4 reads is fast regardless of watchlist
size, or accepting a genuine heuristic pre-filter (with an explicit
recall/speed trade-off the user should sign off on, not one invented
silently) rather than assessing every symbol in full.
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
**Expiry condition:** When Volume 2 collector work or news/event-driven
signal quality is next worked on.
**Logged:** 2026-07-15 (Volume 2 preflight,
`docs/volumes/preflight-vol2-2026-07-15.md`); root-caused and partially
fixed 2026-07-16.

### DEBT-9 · Feature Selection Engine has never run live
**What:** `feature_usage` (Ch.8 of Volume 3: "which models/modules consume
which features") is empty — 0 rows live, vs. every other feature-metadata
table (registry/versions/dependencies/quality/statistics/drift) genuinely
populated. Not a code gap: `FeatureSelectionEngine.persist()` correctly
writes to it. It's reachable only via `POST /features/selection/run`
(`api/features.py:200`) — never scheduled in `main.py`, so there is zero
live evidence it has ever executed.
**Risk while open:** Volume 3's own acceptance criterion "feature selection
identifies the strongest predictors" can't be called operational. Low
urgency — nothing downstream currently depends on its output.
**Expiry condition:** When feature/model selection quality is next worked
on. Resolve either by scheduling it periodically or by explicitly deciding
it's on-demand-only (and updating this entry to reflect that as accepted,
not deferred).
**Logged:** 2026-07-15 (Volume 3 preflight,
`docs/volumes/preflight-vol3-2026-07-15.md`).

---

## Resolved

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
