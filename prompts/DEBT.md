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

### DEBT-2 · IntradayRiskFeatureEngine output is unconsumed — and it stalls
**What:** Volume 3's `IntradayRiskFeatureEngine` writes real 5m-timeframe
features (`intraday_move_from_open_pct`, `intraday_expected_move_next_30m_pct`,
…) that no Volume 4/5 code reads (violates I-2). Separately, it was observed
stalled live: last write 10:25 IST on 2026-07-15 (checked at Volume 2
preflight), still stalled at last write 11:30 IST when re-checked at Volume 3
preflight (VM time 17:09 IST — ~6h stale, spanning the rest of the trading
session and market close). Confirmed NOT self-resolving. Root cause not yet
investigated.
**Expiry condition:** Same as DEBT-1 (they resolve together: the natural fix
for DEBT-1 consumes this engine's output — which first requires it to run
reliably). Root-cause investigation recommended before Volume 4 work resumes,
independent of DEBT-1's wiring fix.
**Logged:** 2026-07-15 (revised at Volume 3 preflight,
`docs/volumes/preflight-vol3-2026-07-15.md`).

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
**Expiry condition:** Before citing Volume 1's performance target as met
anywhere, or when request latency is next worked (pairs naturally with
DEBT-6 — populating Redis is the likely next win).
**Logged:** 2026-07-15 (Volume 1 postflight).

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
**Expiry condition:** When Volume 2 collector work or news/event-driven
signal quality is next worked on.
**Logged:** 2026-07-15 (Volume 2 preflight,
`docs/volumes/preflight-vol2-2026-07-15.md`).

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
