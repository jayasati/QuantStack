# Volume 3 Preflight — Feature Store & Market Intelligence Platform (2026-07-15)

**Scope note:** Same as the Volume 2 preflight — Volume 3 was already built
before this process existed. This is a retroactive check of whether Volumes
1+2's foundation actually supports what Volume 3 already does, live, right
now. Where Volume 2's preflight already established a fact (e.g. collector
freshness, raw tick data), it is not re-derived here.

**Method:** repo inspection, live SSH/SQL checks per
`prompts/VERIFY-COOKBOOK.md`, grep-based architecture verification.

**Verdict: GO**, with one directly-relevant open issue (DEBT-2, confirmed
still live) that should be fixed before Volume 4 intraday work resumes, and
one new minor finding (DEBT-9).

---

## Dependency table

| Dependency | Code | Tests | Live | Verdict |
|---|---|---|---|---|
| Raw events in standardized schema (Ch.5 of Vol 2) feeding feature engines | ✅ `BaseCollector` output schema | ✅ collector test suite | ✅ established in Volume 2 preflight (raw_ticks 15s cadence, real prices) — not re-verified here | PASS (inherited) |
| Feature Registry + metadata tables (Ch.5, Ch.8) | ✅ `FeatureRegistry`, synced via `sync_to_db()` | ✅ `test_feature_selection.py` etc. | ✅ live: `feature_registry` 1075 rows, `feature_versions` 1075, `feature_dependencies` 800 — matches boot log `"features_registered": 1075` | PASS |
| Feature Quality Engine (Ch.22) | ✅ `features/quality.py` | ✅ `test_quality_engine.py` | ✅ `feature_quality` table: 58,777 live rows | PASS |
| Feature Drift Engine (Ch.23) | ✅ `features/drift.py` | ✅ `test_feature_drift_engine.py` | ✅ `feature_drift` table: 11,185 live rows | PASS |
| Feature Statistics | ✅ (part of quality pipeline) | ✅ | ✅ `feature_statistics`: 56,847 live rows | PASS |
| Online store (Redis) + Offline store (Postgres + Parquet), Ch.4 | ✅ `FeatureStore.write()` dual-writes all three | ✅ `test_feature_store_parquet.py` etc. | ✅ Postgres: proven extensively this session (170k+ rows/symbol). **Parquet: 5,134 real `.parquet` files live**, correctly Hive-partitioned by `symbol=/timeframe=`, checked directly on the VM. Redis: thin coverage, already tracked as **DEBT-6** — cross-referenced, not re-logged here | PASS (Redis half tracked as known debt) |
| Historical Replay Engine (Ch.25) | ✅ `features/replay.py` — origin of the `DISTINCT ON` pattern reused in `store.latest()` this session | ✅ `test_replay_engine.py` | (not independently re-checked; code+tests sufficient given this session's direct familiarity with the module) | PASS |
| Feature API (Ch.26) | ✅ `api/features.py` | ✅ `test_features_api.py` | ✅ live all session (history/versions/quality endpoints exercised repeatedly today) | PASS |
| **Chapter 1's core rule: no downstream module reads collectors/raw tables directly** | ✅ verified by grep: zero hits for `OhlcvCandle`/`RawTick` in `app/intelligence/`; every intelligence engine's base class (`IntelligenceComponent.__init__`) wires `self.store = FeatureStore(...)` unconditionally | — | — | **PASS** — the one non-`app/intelligence` hit, `app/prediction/labeling.py`, is `TripleBarrierLabelingEngine` reading raw OHLC to compute training *labels* (did price hit a profit/stop barrier) — a legitimate, spec-appropriate exception, not a features/signals bypass |
| Feature Selection Engine (Ch.24) | ✅ `features/selection.py`, correctly writes to `feature_usage` per its own code | ✅ `test_feature_selection.py` (unit-level) | ❌ **`feature_usage` table: 0 rows, live** — see finding below | PASS in code, **UNVERIFIED live** |

## DEBT ledger check

**DEBT-2 is directly Volume 3's own scope, not just a downstream wiring gap,
and is confirmed still live right now:** `IntradayRiskFeatureEngine`
(`intraday_move_from_open_pct` for HDFCBANK) last wrote at **11:30 IST**;
current VM time is **17:09 IST** — nearly 6 hours stale, spanning the rest
of today's session including market close (15:30 IST). This was flagged in
the Volume 2 preflight as "not yet investigated further"; this preflight
confirms it hasn't self-resolved. Recommend investigating before Volume 4
work reopens (DEBT-1's fix depends on this engine running reliably).

No other Active DEBT entries' expiry conditions are triggered by Volume 3
work specifically (DEBT-1/3/4/6/7/8 all belong to Volume 4/5/2 scope).

## Invariants check

**I-2 (every producer has a consumer)** remains VIOLATED, and this
preflight's own evidence sharpens it: it's not just that
`IntradayRiskFeatureEngine`'s output is unconsumed — the engine has also
stopped producing at all for most of the day. Fixing the consumer wiring
(Volume 4) without first fixing why the producer stalls would be building on
sand.

**I-3, I-4, I-8** all directly exercised and HELD by Volume 3 code this
session (the `DISTINCT ON` bound fix, `asyncio.to_thread` offloading in
`trend.py`/`volatility.py`/`correlation.py`/`analogs.py`, graceful
degradation in every feature engine's `session_factory=None` path).

## New finding: Feature Selection Engine has never run live (DEBT-9)

`feature_usage` (Ch.8's "which models/modules consume which features"
table) is empty. Not a code gap — `FeatureSelectionEngine.persist()`
correctly writes to it — but the engine is reachable **only** via
`POST /features/selection/run` (`api/features.py:200`), never scheduled in
`main.py`. Volume 3's own acceptance criterion "Feature selection identifies
the strongest predictors" cannot be called operational when there is zero
live evidence it has ever executed. Logged as DEBT-9 below — low urgency
(nothing downstream currently depends on its output), but worth a decision:
either schedule it periodically or explicitly accept it as an on-demand-only
tool.

---

## GO verdict

Volumes 1+2's foundation genuinely supports Volume 3 as built — the Feature
Store's core promises (registry, quality, drift, dual-store persistence,
API, and critically the "nothing bypasses the store" architectural rule) are
all proven with live data, not just present in code. No new blockers from
this preflight. The one thing worth acting on before Volume 4 work resumes
isn't new: it's confirming DEBT-2's `IntradayRiskFeatureEngine` stall is
still exactly where the Volume 2 preflight left it, unresolved, 6+ hours
later.
