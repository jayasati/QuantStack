# Data Foundation Audit — QuantStack — 2026-07-17

**Requested by:** Principal Quant Engineer / ML Architect mandate — "produce a complete
technical report before modifying anything," focused on whether the data foundation
(collectors → feature store → feature quality → model/dataset registry) is
institutional-grade. **No code was changed to produce this report.**

**Method:** Synthesis of three live audits already on file this week
(`IRR-report-2026-07-11.md`, `collector-audit-2026-07-13.md` +
`collector-audit-vm-2026-07-13.md`, `perf-audit-2026-07-14.md`), the live `DEBT.md` /
`INVARIANTS.md` ledger (current through 2026-07-17), plus fresh direct reads of the
current source (`backend/app/database/tables.py`, `backend/app/features/*`,
`backend/app/collectors/*`, `backend/app/prediction/ensemble.py`,
`backend/pyproject.toml`) to check what has changed since those audits — most recently
the DEBT-13 ensemble-training fix and the switch to 5m/30min-hold, both shipped today.
Every claim below is either sourced to one of those documents (cited inline) or to a
direct grep/read done in this pass. Nothing here is inferred from doc specs alone.

**Framing, per the mandate:** this is not evaluated as a classifier. The bar is whether
the data foundation can support maximizing risk-adjusted return **after transaction
costs**, for intraday F&O trading on a real watchlist, unattended. Accuracy numbers
appear below only where they already exist in the ledger (e.g. DEBT-13's holdout
figures) — they are not the target metric.

---

## 0. Executive Summary

**The architecture is real and mostly well-built. The data foundation underneath it is
not yet institutional-grade, and the gap is specific and enumerable, not vague.**

Three independent live audits this week converge on the same shape of problem, from
three different angles:

- **IRR (2026-07-11, static code read):** 45/100 overall readiness. Architecture is
  clean (DI, event bus, circuit breakers all real), but ~90% of Volumes 5.5–5.999
  (research infra, portfolio intelligence, decision intelligence, simulation, signal
  orchestration, plugin SDK) is unbuilt despite being marked done, and Volume 5's own
  core pipeline (conviction → qualification → priority) had — at that time — never
  been exercised end-to-end.
- **Collector audit (2026-07-13, live, mid-market-hours):** 35/100. Caught the
  real-time price/candle core (`live_market`, `historical_candles`, `vix`) **actively
  down during a live session** on a broker SSL failure the health endpoint couldn't
  see, plus two feature engines (institutional flow, macro) permanently starved
  because they're registered but never scheduled.
- **Perf audit (2026-07-14, live py-spy):** the same request re-computes market-wide
  intelligence up to 66 times when 1 would do, reads ~1.3M feature-store rows to serve
  ~40k needed, and never hits the Redis cache it has (intelligence engines aren't
  wired to it) — a data-serving-layer problem, not a data-collection one.

**Since then (2026-07-15 to today), a self-directed remediation effort has closed a
meaningful slice of this**, tracked live in `prompts/DEBT.md`: the SSL outage's
downstream symptom (D-only directional intelligence) is fixed with an intraday
overlay; feature selection now runs live; and — shipped **today**, 2026-07-17 — the
Ensemble Prediction Engine trained and persisted a real model for the first time ever,
switched same-day to the user's actual 5m/30-minute-hold trading horizon. This is
genuine progress, honestly logged with live evidence at every step, not roadmap
inflation.

**But the specific institutional-grade data-foundation asks in this mandate — pooled
cross-symbol training, symbol-normalized features, a literal per-row feature-quality
stamp, a real event store for news, corporate-actions collectors beyond
earnings/dividends, and a functioning model/dataset registry — are almost entirely
**absent**, not partially built. Section 4 below is precise about which. This report's
job is to make that gap enumerable so the next work is chosen deliberately, not
discovered live in production a third time.**

---

## 1. Architecture Report

**Score (IRR, 2026-07-11): Architecture 48/100, Integration 42/100, Database 30/100.**
Unchanged in kind since — nothing in DEBT.md's 2026-07-15→07-17 work touched these
structural findings; it fixed *specific* live symptoms downstream of them.

### What's genuinely solid
- Clean one-directional dependency graph: collectors → features → intelligence →
  prediction, verified by import-graph grep, never violated.
- A real, minimal DI container (`backend/app/core/container.py`), a proper
  `EventBus` with retry/backoff/DLQ/idempotency, and a `CircuitBreakerRegistry` —
  correctly implemented, verified under live mock fault injection
  (`collector-audit-2026-07-13.md` Phase 14).
- Structured JSON logging everywhere, zero `print()` calls, disciplined exception
  handling in ~40 of ~42 `except Exception` sites.
- Two previously-fixed IRR Critical bugs (lifecycle race, blocking ML training)
  re-verified still correctly scoped.

### What's structurally broken
1. **The EventBus — the documented backbone for all inter-module communication — has
   zero production subscribers, proven twice with live counters** (18,161+ events
   published, 0 delivered, `collector-audit-2026-07-13.md` Phase 8). Every real
   consumer polls its source table directly instead. This isn't a missing feature;
   it's a fully-built mechanism nothing in the system actually uses.
2. **Direct-instantiation bypasses the DI container throughout `intelligence/*.py` and
   `prediction/opportunity.py`** — the container's own docstring says "services never
   instantiate concrete classes directly"; four files do exactly that (IRR Phase 1
   #2).
3. **The Market Intelligence layer (Volume 4) and most of Volume 5's decision chain
   are still not continuously scheduled.** DEBT-13 (open, `prompts/DEBT.md`) found —
   as of 2026-07-16 — 11 of 16 Volume 5 modules had **never executed live, ever**,
   outside manual API calls. As of today, exactly one (Ensemble Prediction, 5.6) has
   been wired into a live scheduled job. Calibration, Model Agreement, Historical
   Similarity, Market Context, Conviction, Qualification, Priority, Duplicate
   Detection, Lifecycle, and Explainability are all still unscheduled. Conviction's
   own live evidence sources currently read 60% default-degraded weight because of
   this (`DEBT-13`, live-verified `GET /prediction/conviction/MARUTI` trace).
4. **Zero foreign keys across 28 tables; 54% of the schema (15 tables, including
   `AuditLog`) is dead** — every real engine instead writes an untyped JSONB blob
   into one shared `market_events` ledger (IRR Phase 6). This is the single largest
   architectural liability for anything calling itself institutional-grade: there is
   currently no relational integrity anywhere in the persistence layer.
5. **No process supervision, no restart policy, no human-notification sink for
   CRITICAL alerts** — `app/telegram/` is a 0-byte stub; the escalation chain
   terminates at a log line and the dead EventBus (IRR Phase 10, Phase 13).
6. **Security is functionally absent**: zero authentication on ~89 endpoints
   including ML-training triggers and lifecycle mutations; broker credentials
   (API key, MPIN, TOTP secret) sit in plaintext `.env`; no TLS; no rate limiting
   (IRR Phase 9, Security 16/100 — the single lowest score in the entire audit).

### Net assessment
The skeleton (DI, events, circuit breakers, layering) would pass an institutional
architecture review. What runs on top of that skeleton mostly doesn't use it as
designed — engines reach around the container, the event bus is decorative, and the
scheduler covers roughly a third of what the volume specs describe as "the decision
core." Fixing the data foundation (this report's actual mandate) will make this worse
before it gets better unless the new feature-store/registry work is wired through the
container and scheduler from day one, not bolted on as another parallel path.

---

## 2. Collector Report

**Live scorecard: 35/100 (2026-07-13, mid-market-hours), later 100% recovered on the
resource-contention finding after a VM resize (2026-07-14); underlying per-collector
data-correctness bugs from the 07-13 audit remain, confirmed unchanged as of
2026-07-13 evening's VM re-check.**

13 production collectors, all subclassing `BaseCollector`, auto-discovered and
APScheduler-driven. None use a message queue; all share one flat single-retry policy
(0.2s, no exponential backoff, deliberate per the base class's own docstring) plus a
per-collector circuit breaker (3-failure threshold, 60s recovery).

### The finding that matters most: an active outage the health check couldn't see
At audit time, `live_market`/`historical_candles`/`vix` — the entire real-time
price/candle core the user's stated intraday-F&O use case depends on — were **down
live, during market hours**, on a broker SSL trust-store failure. `GET /health/ready`
reported `200 OK` throughout, because it checks only Postgres/Redis, never broker or
circuit-breaker state. This single finding is why the collector score (35) came in
*below* the whole-system IRR score (45) two days earlier — a static code review
cannot see a live outage; a live audit can. Root cause (Fortinet SSL inspection on the
old laptop deployment) was resolved by the GCP VM migration; a second, different
capacity-driven outage recurred under full live-market-hours load on 2026-07-14 and
was resolved by resizing e2-medium → e2-standard-4 (`collector-audit-vm-2026-07-13.md`
"Update — 2026-07-14"). **Health monitoring itself has since been proven to lie under
real failure conditions twice** — this is the collector layer's single highest-value
open gap, not any individual data bug.

### Per-collector state (condensed from `collector-audit-2026-07-13.md`, cross-checked live 07-13/07-14)

| Collector | Cadence | Verdict | Material defect |
|---|---|---|---|
| `live_market` | 15s | 🔴→🟡 | WS packets carry literal `0.0` OHLC for NIFTY/BANKNIFTY/INDIAVIX (not null — a data-correctness bug, not missing data); 72.8% null bid/ask/depth (WS Quote-mode has no depth fields) |
| `historical_candles` | 300s | 🔴→🟡 | 88% duplicate `market_events` re-emission even on 0-new-bars runs; "misleading-healthy" stalls (reports success, underlying series frozen for hours) seen on both the laptop and the VM |
| `vix` | 300s | 🔴→🟡 | Same misleading-healthy pattern; root cause (403 bursts during cold-start backfill) not fully closed |
| `reference_indices` | 3600s | 🟡 | Only 1 of 2 scheduled runs ever succeeded at audit time; a `market_events` copy nobody reads |
| `options_intelligence` | 60s | 🟡 (data), ❌ (usage) | `exchange` field never overridden from schema default `"NSE"` — 400/400 sampled SENSEX rows mislabeled; **computed correctly, quality-scored, stored, and then never consumed anywhere in `ensemble.py`/conviction/regime** — the highest-value wire-up gap for the stated F&O use case |
| `market_breadth` | 60s | 🟡 | Redis EMA cache confirmed writing zero keys — every restart pays a ~55s cold-start penalty; was root cause of a 7h9m full outage on 2026-07-08 |
| `sector_rotation` | 60s | 🟡 | `volume_ratio` has never left exactly 1.0 in 8+ days (structurally unreachable threshold); `index_volume` permanently null for 2 of 12 sectors (NSE index-name mismatch, fails silently) |
| `institutional_flow` | 3600s | 🟡 | Up to 66x duplicate emission per deal (no ingest-time dedup key); `promoter_net`/`insider_net` always exactly 0.0 (NSE feed returns empty); 86-hour weekend scheduler stall produced zero alerts |
| `macro_intelligence` | 300s | 🟡 | `MacroFeatureEngine` registered, never scheduled — feature_store has 0 rows for it, unchanged across all three audits and confirmed still true post-DEBT-13; INDIA10Y permanently absent (no public ticker) |
| `event_calendar` | 1800s | 🟡 | Up to 137x duplicate emission for one event (no upsert key); only 4 of 18 spec'd event kinds ever populate, **including both RBI and FED — the two "critical/trading-freeze" kinds** (`configs/event_calendar.yaml` is effectively empty) |
| `news_intelligence` | 120s | 🟡 | FinBERT inference genuinely costs 30-45s per cycle at real article volume — structurally can't fit a sub-cycle budget by code changes alone; latency fluctuates 3.5s↔42s depending on article volume even outside market hours (DEBT-8, still open) |
| `global_shock_news` | 30s | 🟡→🔴 (VM, under load) | 74.9% duplicate rate (233 distinct URLs / 930 rows); zero downstream consumers found anywhere in the pipeline; was the dominant cause of the 2026-07-14 VM capacity incident |
| `nse_delivery` | 3600s | 🟡 | 19.2% duplicate rate; unexplained 6-day dead gap in production history, cause never found |

### Systemic, cross-collector root causes (fix once, resolves many rows above)
1. **No business-key constraint on `market_events`** — every duplication bug above is
   the same root cause (surrogate `id` only, no `(source, symbol, kind, as_of)`-style
   unique key) appearing 6 separate times.
2. **`freshness_seconds` is 100% null for 7 of 13 collectors** — the exact field the
   schema defines to answer "how stale is this," never populated, so
   `DataQualityEngine`'s own freshness score was blind to `vix` serving 3-day-stale
   data while self-reporting 96/100 freshness.
3. **Per-item/per-feed failures inside one `collect()` call are swallowed to a
   WARNING log and never surface to collector health** — a partial outage (one of 4
   RSS feeds down, one Yahoo ticker failing) is invisible at the health endpoint.

### What is genuinely solid here too
Market-hours/holiday gating (fixed 2026-07-15, `is_nse_market_open()` now checks the
holiday table too), the `market_hours_only`/`after_hours_only` collector gates, real
tick-to-candle aggregation for sub-minute freshness independent of the 300s external
sweep (added 2026-07-16), and the NSE/BSE fallback chain with future-timestamp
guarding — all live-verified working, not just unit-tested.

---

## 3. Feature Report

**Volume 3 (Feature Store) compliance per IRR: mostly Implemented, with two real,
confirmed gaps** — `MacroFeatureEngine`/`InstitutionalFlowFeatureEngine` never
scheduled (Ch2), and `features/price.py` never calling `normalize.py` (Ch21) — plus
what this pass adds: the literal feature-row schema does not match the metadata
contract this mandate specifies.

### Current architecture (real and reasonably sophisticated)
16 feature engines exist (`backend/app/features/`: price, volume, volatility,
liquidity, options, breadth, sector, relative, structure, timefeat, events, macro,
institutional_flow, intraday_risk, news, plus normalize/quality/drift/selection/
snapshots/replay/store/registry/stats as infrastructure). A dependency graph with
Kahn's-algorithm topological sort and cycle detection is real (Ch7, Implemented). A
Feature Registry and 7-table feature-metadata schema exist (Ch5/Ch8, Implemented).
Feature Quality (`features/quality.py`) computes freshness/completeness/PSI-based
stability/variance/correlation-stability/noise/predictive-power into a weighted 0-100
score, scheduled into `feature_health_sweep`. Feature Selection
(`features/selection.py`) does real MI ranking, permutation importance, exact linear
SHAP, and RFE — genuinely sophisticated, no external ML-explainability library
required. A Historical Replay Engine (`features/replay.py`) exists.

### The literal feature-row schema (verified by direct read, `database/tables.py:58-74`)
```
FeatureStoreRow: feature_name, feature_version, symbol, timeframe, ts, value, window_size
```
Against this mandate's explicit requirement ("Every feature must contain timestamp,
symbol, feature_version, collector_version, last_updated, feature_quality_score — no
feature should exist without metadata"):

| Required field | Status |
|---|---|
| `timestamp` | ✅ (`ts`) |
| `symbol` | ✅ |
| `feature_version` | ✅ (defaults `"v1"`, real column) |
| `collector_version` | ❌ **absent** |
| `last_updated` | ❌ **absent** (only `ts`, the observation time, not a write/refresh timestamp) |
| `feature_quality_score` | ❌ **absent from the row itself** — quality is computed by a separate engine into a separate `feature_quality` table, not stamped onto each observation |

This is the single most literal, checklist-verifiable gap in this entire audit: the
mandate's exact metadata contract is roughly half-satisfied by the current schema, and
the other half exists as parallel infrastructure that was never merged onto the
feature row itself.

### Feature versioning: real infrastructure, zero live exercise
`feature_versions` and the versioning mechanism are correctly built (Ch6). But
`SELECT count(DISTINCT version) FROM feature_versions` = **1** across all 1,075
registered features — every one has only ever been `"v1"` (DEBT-10, open). The
"models pin to a specific feature version" guarantee has never been tested against a
real version bump.

### Feature quality: real, but with a coverage hole
`feature_quality` has 169,742 live rows, but only 990 of 1,075 registered features
(92%) have ever received a score — 85 never-scored, concentrated in
`institutional_flow` (35), `liquidity` (28), `breadth` (12) (DEBT-11, open,
hypothesis-but-not-confirmed: index symbols legitimately lack underlying
volume/liquidity data by design).

### Normalization: the mandate's own stated concern, and it's real
`features/price.py` never imports `normalize.py` (IRR Ch21, confirmed via grep) —
price features (returns, ATR, momentum, beta) are stored and consumed by the ML
pipeline **entirely unnormalized**, directly contradicting that chapter's own "never
feed raw values into ML models" warning. This is distinct from, and in addition to,
this mandate's cross-symbol normalization ask (Section 4 below) — even *within-symbol*
normalization is incomplete.

### What actually trains the live model today
`ENSEMBLE_FEATURE_SPECS` (`prediction/ensemble.py`) mixes long-history D-timeframe
features with several feature categories that only started being collected 1-2 days
before DEBT-13's fix — the coverage gate had to be rewritten around a
`CORE_FEATURE_NAMES` subset just to get training to produce a single sample
(`b41f3dd`, today). Options-intelligence features (the signal most relevant to F&O)
are confirmed absent from the ensemble spec entirely (`collector-audit-2026-07-13.md`
Phase 15/16). News features are correctly *not* wired into the model, per this
mandate's own instruction — confirmed by grep, zero `news_*` hits in
`ENSEMBLE_FEATURE_SPECS`.

---

## 4. Missing Components — mapped directly against this mandate's checklist

Each item below is stated EXISTS / PARTIAL / MISSING against the mandate's literal
ask, with the evidence.

### Data collection improvements
| Ask | Status | Evidence |
|---|---|---|
| ≥1 year historical data | **PARTIAL** | Daily bars: `default_lookback["D"] = timedelta(days=365*2)` — 2 years, real. Intraday bars (the timeframe the live model now actually trains on, per today's 5m switch): capped 2-60 days depending on interval (5m = 10 days). The live model is training on ~7-8 trading days of 5m history, not close to a year. |
| Pooled cross-symbol training | **MISSING** | `EnsemblePredictionEngine.train(self, symbol: str, ...)` trains one model per symbol; no pooled/watchlist-wide fit found anywhere in `ensemble.py`. |
| Symbol-normalized features | **MISSING** | `features/normalize.py` normalizes each feature against its own rolling history (within-symbol, over time) — not cross-symbol (e.g. no z-score/rank of a feature *across the watchlist* at a given timestamp). |
| Point-in-time feature storage | **EXISTS** | `FeatureStoreRow` is append-only, uniquely keyed on `(feature_name, feature_version, symbol, timeframe, ts)` — genuinely point-in-time by construction, this is a real strength. |
| Historical feature regeneration | **EXISTS (mechanism), not independently verified as a backfill job** | `features/replay.py` (Historical Replay Engine, Ch25) is implemented; whether it's wired into an actual re-derive-for-a-past-range CLI/job wasn't independently exercised in this pass. |
| Data versioning | **MISSING** | No `data_version`/`dataset_version` field or dataset hash anywhere in the schema or training code. |
| Feature versioning | **EXISTS (infrastructure), unexercised** | Real `feature_version` column + `feature_versions` table — but every one of 1,075 features has only ever been `"v1"` (DEBT-10). |
| True Feature Store | **PARTIAL** | Point-in-time storage, registry, dependency graph, quality/drift/selection engines all real. Missing: cross-symbol pooling, per-row metadata completeness (below), an online/offline sync story beyond Postgres+Redis (no Parquet consumption path despite `pyarrow` now in `pyproject.toml` as a write-only archival sink). |

### Feature Store metadata (the mandate's literal 6-field contract)
Already detailed in Section 3 — **3 of 6 fields present on the row itself**
(`timestamp`, `symbol`, `feature_version`); **`collector_version`, `last_updated`,
`feature_quality_score` are absent from the feature row** and would need a schema
migration to add, even though quality scoring itself exists as separate
infrastructure.

### Collector improvements
| Ask | Status | Evidence |
|---|---|---|
| Missing data / duplicate rows / timezone / holiday / symbol mapping / late-arriving data audits | **DONE — twice, live** | `collector-audit-2026-07-13.md` + VM delta report are exactly this audit, already executed with live evidence, per-collector, this week. |
| Automatic health monitoring | **EXISTS, materially real** | `CollectorHealthStatus` dataclass: `last_run, last_success, next_run, run_count, failure_count, consecutive_failures, retry_count, avg_latency_ms, last_quality_score, last_error, in_flight, queue_length` — genuinely close to the mandate's ask (heartbeat, freshness, latency, failure count, last success all present), exposed via API. |
| ...but proven to lie under real failure | **CONFIRMED GAP** | `/health/ready` reported 200 OK during a live 23+-minute broker-price outage (twice, on two different root causes, two days apart) because it only checks Postgres/Redis, never broker/circuit-breaker state. The per-collector health objects are real; the aggregate readiness signal built from them is not trustworthy. |

### News architecture
| Ask | Status | Evidence |
|---|---|---|
| Event Store replacing point sentiment | **PARTIAL** | `collectors/domains/news.py` does real cross-run Jaccard dedup and novelty scoring; `features/news.py` builds hourly sentiment/novelty/urgency/momentum/impact rolling z-scores — genuinely beyond a single scalar. |
| Required fields: timestamp/symbol/source/urgency/sentiment/novelty/embedding hash | **PARTIAL** | timestamp/symbol/source/sentiment/urgency/novelty all present in some form; **no embedding hash field exists anywhere.** |
| Deduplication | **PARTIAL, and empirically leaky** | Mechanism exists (in-process dedup deque/Jaccard window) but measured live at 74.9% duplicate rate for `global_shock_news` and up to 11x for `news_intelligence` — the window is too small for real article churn, and it's in-process (resets on restart), not durable. |
| Exponential decay | **NOT FOUND** as an explicit function. |
| 30-min / session / overnight / daily sentiment tiers | **MISSING as specified** — aggregation granularity found is hourly buckets, not this four-tier structure. |
| Not connected to ML model yet | **CORRECTLY TRUE TODAY** — confirmed by grep, zero `news_*` features in `ENSEMBLE_FEATURE_SPECS`. This is the one news-architecture item that is already exactly where the mandate wants it. |

### Corporate announcements
| Ask | Status | Evidence |
|---|---|---|
| NSE/BSE earnings, board meetings, dividends, splits | **EXISTS, live, wired** | `collectors/sources/nse_events.py` pulls live NSE endpoints for DIVIDEND/BONUS/SPLIT ex-dates, RESULTS (earnings/board meetings), IPOs, F&O expiries — genuinely real and running (`event_calendar` collector, 1800s cadence). |
| Buybacks | **MISSING** — no dedicated collector found. |
| Credit rating changes | **MISSING** — no dedicated collector found. |
| Large orders (bulk/block deals) | **MISSING as a distinct collector** — `institutional_flow`/`nse_flows.py` cover aggregate FII/DII flow, not per-stock bulk/block deal disclosures. (Note: `collector-audit-2026-07-13.md` Phase 1 lists `institutional_flow`'s source as including "block/bulk deals" in its table header, but Phase 5's field-level inspection found `promoter_net`/`insider_net` always exactly 0.0 — the NSE `corporates-pit` feed this depends on returns empty. Effectively non-functional even where notionally in scope.) |
| Store in event store | **N/A given above** — 4 of the mandate's 8 named categories exist and write into the shared `market_events` table (not a dedicated event store schema); 3 categories (buybacks, credit ratings, large orders) have no collector to store from. |

### Data quality
| Ask | Status | Evidence |
|---|---|---|
| Schema validation | **WEAK** — `CollectorOutput` structural validation exists and every sampled record type-checks, but `validate()` is a no-op default per collector — real data-correctness bugs (SENSEX mislabeled `"NSE"`, WS OHLC literal `0.0`) sit inside structurally-valid records, invisible to this layer entirely. |
| Missing-value reports | **PARTIAL** — `freshness_seconds` null-rate alone would have caught most of this week's staleness findings; it's null for 7 of 13 collectors, so the report a `DataQualityEngine` run produces is itself incomplete. |
| Feature drift reports | **EXISTS** — `features/drift.py` (Ch23, Implemented per IRR). |
| Duplicate detection | **EXISTS but structurally blind to the actual bug class found** — `DataQualityEngine`'s duplicate fingerprint includes each record's own creation timestamp, making it mathematically incapable of catching the cross-run duplication this week's audit found manually in 6 collectors (up to 137x). |
| Distribution reports | **EXISTS** — PSI-based distribution stability is part of `features/quality.py`'s scoring. |
| Outlier detection | **NOT INDEPENDENTLY CONFIRMED** this pass — not read directly; flagged as a follow-up check, not asserted either way. |
| Data completeness scoring | **EXISTS, at the feature layer** — composite 0-100 score in `features/quality.py`, scheduled. **Does not exist at the raw-collector layer** in a form that would have caught this week's live outage before an audit found it manually. |

### Model registry
| Ask | Status | Evidence |
|---|---|---|
| Model registry | **MISSING (dead scaffolding)** — `ModelVersion` table exists with only `model_name`/`version`/`status` columns; never written to by any code (`ensemble.py:713` generates a version string inline, `f"ensemble_v1-{trained_at}-n{len(rows)}"`, but never persists it to the table). Confirmed still true after today's DEBT-13 commits, which touched training/scheduling only. |
| Dataset registry | **MISSING** — no equivalent table or mechanism found anywhere. |
| Experiment tracking | **MISSING** — no experiment-run table, no MLflow/W&B-equivalent, nothing found. |
| Training metadata | **PARTIAL** — training run produces real numbers (sample count, holdout accuracy per model, per DEBT-13's live log) but these are only ever logged to the ledger by hand, never persisted to `RetrainingRun` (also dead scaffolding, same pattern as `ModelVersion`). |
| Git commit tracking tied to a model | **MISSING** — no code found linking a trained model artifact to the commit hash that produced it. |
| Data hash / feature hash | **MISSING** — no hash of the training dataset or feature set is computed or stored anywhere. |

---

## 5. Technical Debt List

Two live, currently-tracked ledgers already exist and are the authoritative source —
this section indexes them rather than re-deriving them.

### Active in `prompts/DEBT.md` (2026-07-17)
- **DEBT-2** — `IntradayRiskFeatureEngine`: 6 of 9 base features (plus all `_z`
  companions) still write-only, no consumer.
- **DEBT-3** — No outcome evaluator / win-rate metric exists anywhere. Flagged by the
  user as the recommended next build, before any signal is used for a real decision.
- **DEBT-4** — Market-wide `events.score` fires candidacy for every watchlist symbol
  simultaneously, producing priority-0.00 candidates.
- **DEBT-7** — `/prediction/candidates` still 3-4.5x over Volume 1's <2s target after
  multiple rounds of live-measured fixes; remaining cost is genuine per-symbol
  `assess()` computation, not cache-miss latency.
- **DEBT-8** — News collector latency genuinely fluctuates with article volume
  (structural FinBERT-CPU-cost ceiling), not fully resolved; market-hours re-check
  still pending as of the last log entry.
- **DEBT-10** — Feature versioning has never been exercised beyond v1 (1,075/1,075
  features).
- **DEBT-11** — 85 of 1,075 features have never received a quality score.
- **DEBT-12** — `ExplainabilityStore`'s persisted history has zero API consumers
  (write-only, 50,000+ rows).
- **DEBT-13** — 10 of Volume 5's 16 modules (Calibration through Explainability
  Report) have still never run live, one link fixed today (Ensemble Prediction).

### Invariants currently VIOLATED (`prompts/INVARIANTS.md`, 2026-07-17)
- **I-1** — Signal freshness: directional intelligence's D-based sub-features still
  only update at midnight even after the intraday-overlay fix; a genuine
  market-hours-live check (not just a post-close snapshot) is still owed.
- **I-2** — Every producer has a consumer: DEBT-2's remaining unconsumed features.
- **I-5** — Signals are outcome-accountable: no outcome evaluator exists (DEBT-3),
  and DEBT-13 found there is currently no genuine live signal for one to measure
  beyond raw candidate detection.
- **I-7** — Status claims match measured reality: `roadmap.md` still overstates
  Volume 5.5+ completeness, and — new as of the 2026-07-16 Volume 5 preflight —
  Volume 5's own roadmap row is marked "✅" despite 12 of 16 modules never having
  executed live at the time.
- **I-10** — Market-wide signals labeled as such: unresolved by design decision
  (tied to DEBT-4).

### From IRR (2026-07-11) not superseded by anything in DEBT.md
These are architectural/security/testing findings the 2026-07-15→17 remediation
sprint did not touch (it was scoped to live-behavior/scheduling fixes, not
architecture):
- Zero API authentication/authorization (Security 16/100) — **unaddressed**.
- Zero foreign keys, 54% dead schema tables, unindexed `market_events` — **unaddressed**
  (though `feature_store`/`market_events` did get targeted composite indexes per the
  2026-07-14 perf audit's fix list, item 3 of 8).
- No process supervision / restart policy on `docker-compose.yml` — **unaddressed at
  the compose level** (the VM itself was resized, which is a different fix).
- No human-notification alert sink (Telegram/webhook) — **unaddressed**, `app/telegram/`
  still a stub.
- No test coverage tooling, though `pytest-cov` **is now a listed dev dependency**
  in `backend/pyproject.toml` (a change since the IRR audit — whether a coverage
  threshold is actually enforced anywhere was not verified this pass).
- Model/Calibration caches have the same unprotected check-then-train race
  `lifecycle.py` was fixed for — **not verified whether DEBT-13's ensemble-scheduling
  work incidentally addressed this; treat as still open until confirmed.**

### New in this pass, not previously logged anywhere
- **Feature-row metadata gap** (Section 3/4): `collector_version`, `last_updated`,
  `feature_quality_score` are absent from `FeatureStoreRow` itself — this is a schema
  gap, not a behavior bug, so it wouldn't have surfaced in any of the three live-audit
  passes (none of which were scoped to schema-vs-spec comparison at the column level).
- **`ModelVersion`/`RetrainingRun` remain fully dead after DEBT-13** — worth a DEBT.md
  entry of its own now that a real trained model exists for the first time and has
  nowhere authoritative to be recorded.

---

## 6. Implementation Summary

**What is genuinely done, verified live, no caveats:**
- Collector layer core mechanics: retry, circuit breaking, structured health status,
  market-hours/holiday gating, tick-to-candle real-time aggregation.
- Point-in-time feature storage schema and dependency-graph-driven registry.
- Feature quality scoring, drift detection, and MI/SHAP-based selection — all real,
  all scheduled.
- Intraday directional intelligence (trend/structure/momentum/volatility/relative-
  strength) now blends a same-day 5m overlay, live-verified against a real
  intraday reversal.
- Ensemble Prediction Engine trains and persists real models, as of today, on the
  user's actual 5m/30-min-hold horizon — the first Volume 5 link to run live at all.

**What is partially done — real infrastructure, missing the specific piece that makes
it "institutional-grade":**
- Feature Store: point-in-time storage real, cross-symbol pooling and full per-row
  metadata absent.
- Feature versioning: mechanism real, never exercised past v1.
- News pipeline: dedup/novelty/aggregation real, but leaky (up to 75% duplicate rate
  live) and missing embedding hashes and the specified decay-tier structure.
- Data quality: strong at the feature layer, structurally blind to the exact
  duplicate/staleness bug classes found live this week.
- Corporate announcements: earnings/dividends/splits/board-meetings real and live;
  buybacks, credit ratings, and large-order collectors don't exist.
- Health monitoring: rich per-collector objects exist; the aggregate readiness signal
  built from them has been proven wrong twice this week under real failure.

**What is essentially not started:**
- Pooled/cross-symbol training and symbol normalization.
- Data/dataset versioning and any git-commit/data-hash linkage to a trained model.
- Model registry and experiment tracking (tables exist, are dead).
- Embedding-hash news dedup and the 30-min/session/overnight/daily sentiment tiers.
- Buyback/credit-rating/bulk-deal collectors.
- API authentication, human alerting, process supervision — not this mandate's direct
  scope, but load-bearing for calling *anything* here "institutional-grade" once it's
  running unattended.

**Sequencing note, given what's already in motion:** DEBT-13 (Volume 5's live
decision-pipeline wiring) and this mandate's data-foundation work will collide if run
independently — DEBT-13's next step (Calibration) needs real prediction history to fit
against, and this mandate's model-registry ask needs exactly that same
prediction-history to be worth registering. Recommend treating "record every ensemble
training run in a real registry, keyed by data hash + commit hash" as the connective
piece between the two efforts rather than building the registry in isolation, and note
that the project's own standing decision (2026-07-15, `[[quantstack-process-redesign]]`)
was to route **all** new work through preflight → build → seam-check → postflight —
this report is a preflight-equivalent input for that lifecycle, not a substitute for
running it.

---

## 7. Benchmark Against Institutional Quant Systems

This section compares QuantStack's current data foundation against what a
production quant desk (equities/derivatives, mid-frequency-to-intraday) would
consider baseline, not aspirational.

| Capability | Institutional baseline | QuantStack today | Gap |
|---|---|---|---|
| Point-in-time correctness | Every feature/label join is provably as-of a timestamp, no lookahead | Real — `FeatureStoreRow` uniquely keyed on `(feature, version, symbol, timeframe, ts)`, append-only | **At parity.** This is a genuine strength — many retail-grade systems get this wrong and QuantStack doesn't. |
| Cross-sectional (pooled) modeling | Standard: one model trained across the tradable universe, symbol as a feature/fixed effect, features normalized within cross-section (rank/z-score across the universe at each timestamp) | One model per symbol; no cross-sectional normalization | **Large gap.** This is arguably the single highest-leverage fix available — pooled training multiplies effective sample size by watchlist width (currently 25 symbols) and is standard practice specifically because per-symbol models overfit on thin history, which DEBT-13 just hit directly (474 samples for one symbol at 5m/6-bar). |
| Feature/dataset lineage | Every trained model traceable to exact feature versions + code commit + data snapshot hash, for audit and bug-bisection | Feature versioning exists unexercised (all v1); no data hash; no commit linkage; `ModelVersion` table dead | **Large gap**, and specifically an audit/compliance liability, not just an ML-quality one. |
| Cost-aware evaluation | Backtests and live evaluation net of slippage/impact/fees, since the stated objective is post-cost risk-adjusted return | No outcome evaluator exists at all yet (DEBT-3) — pre-cost accuracy isn't even measured, let alone post-cost | **Foundational gap.** Every institutional shop's first question about a signal is "what's it worth after costs" — this system cannot currently answer "what happened after the signal fired" at all. |
| Label construction | Purged/embargoed cross-validation, de-overlapped or sample-weighted labels for overlapping holding windows (standard since López de Prado) | Triple-barrier labeling is real and correctly implemented (Volume 5 Ch5) — but DEBT-13 found today's 5m/6-bar labels overlap by up to 25 of 30 minutes, un-deoverlapped and unweighted; the reported accuracy lift over the D-bar model is explicitly flagged as possibly an autocorrelation artifact, not verified edge | **Known, self-flagged gap.** Rare for a project this size to catch this itself — worth crediting — but it means today's headline accuracy numbers aren't yet trustworthy for capital allocation. |
| Data-quality gating before training | Automated completeness/freshness/drift gates block a training run on bad input | `features/quality.py` scores exist but aren't wired as a training precondition; DEBT-13's own coverage-gate bug (0 usable samples until the gate logic was fixed) shows the gate that does exist wasn't calibrated against real collection history | **Partial gap** — the scoring exists, the enforcement doesn't consistently reach the training path. |
| Health/readiness signal integrity | A "ready" signal that is provably tied to the actual serving path (broker connectivity, data freshness), tested under fault injection | Proven wrong twice live this week (`/health/ready` = 200 OK during a real 23-minute price-feed outage, twice, two different root causes) | **Large gap**, actively dangerous for unattended operation specifically. |
| News/alt-data as auxiliary, not primary, signal | Common institutional pattern: alt-data feeds inform risk/sizing/timing, rarely gate direction directly, given noise | Correctly implemented today — news explicitly excluded from `ENSEMBLE_FEATURE_SPECS`, confirmed by grep | **At parity**, and worth explicitly not disturbing when news architecture work resumes. |
| Corporate actions coverage | Earnings, dividends, splits, buybacks, rating actions, bulk/block deals all feed either a risk overlay or a trading-halt/adjustment rule | 4 of ~8 standard categories live (earnings/dividends/splits/board meetings); buybacks and credit ratings entirely absent; bulk/block deals nominally in scope but empirically always-zero (dead upstream feed) | **Moderate gap** — the highest-frequency, highest-impact categories (earnings, corporate actions around ex-dates) are covered; the lower-frequency ones aren't. |
| Model governance / registry | Every production model has a registry entry: version, training window, metrics, approver, promotion status | `ModelVersion`/`RetrainingRun` tables exist and are 100% dead — the exact institutional-governance surface named in this mandate | **Large gap**, and now specifically live-relevant since a real model started training today with nowhere to be registered. |

**Overall positioning:** QuantStack's data foundation is closer to a well-engineered
**research prototype with unusually good point-in-time hygiene** than to an
institutional production system. The parts that are hard to bolt on later
(point-in-time correctness, feature versioning infrastructure, triple-barrier
labeling, correct news/model separation) are already right. The parts that are
comparatively cheap to add but currently missing (pooled training, a model registry,
an outcome evaluator, corporate-action collector coverage) are exactly the ones an
institutional desk would flag first — and, not coincidentally, are close to what this
mandate itself asks for.

---

## Sources

- `docs/volumes/IRR-report-2026-07-11.md` (18-phase static audit, Volumes 1-5.999)
- `docs/volumes/collector-audit-2026-07-13.md` (live, 27 agent-runs, mid-market-hours)
- `docs/volumes/collector-audit-vm-2026-07-13.md` (VM migration delta + 2026-07-14
  market-hours re-audit and capacity resolution)
- `docs/volumes/perf-audit-2026-07-14.md` (live py-spy investigation of
  `/prediction/candidates`)
- `prompts/DEBT.md` / `prompts/INVARIANTS.md` (live ledger, current through
  2026-07-17)
- Direct reads this pass: `backend/app/database/tables.py`, `backend/app/features/*`,
  `backend/app/collectors/*`, `backend/app/prediction/ensemble.py`,
  `backend/pyproject.toml`, `configs/default.yaml`, recent git log
