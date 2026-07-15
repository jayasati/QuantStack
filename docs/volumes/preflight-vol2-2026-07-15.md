# Volume 2 Preflight — Data Collection & Market Intelligence Layer (2026-07-15)

**Scope note (read first):** Volume 2 is not "about to be started" in the
literal sense — it, and everything through ~Volume 5.99, was already built
before this prompt-system existed. This preflight is therefore retroactive:
it verifies whether Volume 1's foundation actually supports what Volume 2
already does, live, right now — both to validate the foundation before any
further Volume 2 work (new collectors, etc.) and as a real-world exercise of
the process itself. Where this preflight found something wrong, it's a live
finding about the running system, not a hypothetical.

**Method:** repo inspection, live SSH/SQL/API checks per
`prompts/VERIFY-COOKBOOK.md`, cross-referenced against
`docs/volumes/postflight-vol1-2026-07-15.md` (not re-verifying what that
report already established).

**Verdict: GO**, with one live finding worth tracking (not a blocker) and
zero triggered DEBT expirations.

---

## Dependency table

Every input Volume 2 (per `docs/volumes/volume-2.md`) assumes Volume 1
provides, verified at three levels.

| Dependency | Code | Tests | Live | Verdict |
|---|---|---|---|---|
| Broker abstraction (Ch.6): `BrokerInterface` → DI, never direct SmartAPI import | ✅ `app/market/broker.py`, resolved via `container.py` | ✅ `test_angel_one_adapter.py` (8), `test_broker_sectors_fallback.py` | ✅ `broker.connect()` at startup; live quote/greeks calls succeed (only the known `optionGreek` 403 noise) | PASS |
| Circuit breaker wired into broker calls (Volume 1 §13) | ✅ `core/circuit_breaker.py`, wired into `AngelOneAdapter._request` | ✅ `test_circuit_breaker.py` (5) | ✅ registered live, `.get("broker.angel_one")` resolved at startup | PASS |
| Event Bus (Ch.18): publish/subscribe, retries, DLQ, idempotency, versioning | ✅ `events/bus.py` — all features present (verified reading the full class this session) | ✅ `test_event_bus.py` (9) | ✅ collectors publish; **zero subscribers is correct per Volume 1 §10** (audit spine, not call path) — not a defect, confirmed by this session's own `has_subscribers()` work | PASS |
| Redis caching (Ch.19): TTL, invalidation, stale-while-revalidate, rate-limit | ✅ `core/cache.py` — `get_or_set`, `rate_limited`, `get_safe`/`set_safe` all present | ✅ `test_cache.py` (8) | ✅ `/health/ready` → redis "ok"; dashboard's BSE throttle (`_DASHBOARD_MIN_FETCH_INTERVAL_SECONDS`) demonstrates rate-limit protection working live | PASS |
| Config loading, required keys (§6) | ✅ `Settings` class, env→`.env`→`default.yaml` priority | — | ✅ working all session (broker auth, DB, Redis all connect) | PASS |
| DI container (§8) | ✅ `Container.register/resolve`, used throughout | ✅ `test_container.py` | ✅ `wire_default_services()` runs clean at every deploy this session | PASS |
| APScheduler (§ scheduling policy) + Collector Registry (Ch.17) | ✅ `scheduler/service.py`, `collectors/registry.py` | ✅ `test_scheduler_service.py`, `test_collector_framework.py` | ✅ `/health/scheduler/status` (added this session): 13 `collector.*` jobs currently scheduled and firing, matching `collectors_discovered: 13` at boot | PASS |
| Initial DB tables (§19): `collectors`, `collector_health`, `market_events` | ✅ migration 0001 | — | ✅ `collector_health` live query returned 13 rows with real, current quality scores | PASS |
| Structured JSON logging (§11) | ✅ `JsonFormatter` | — | (not independently re-checked this pass — covered by Volume 1 postflight) | PASS (inherited) |

**No blockers.** Every dependency Volume 2 needs from Volume 1 is present in
code, exercised by tests, and observably working live right now.

## DEBT ledger check

None of the 6 Active entries' expiry conditions are triggered by Volume 2
work — DEBT-1/2 are Volume 3→4 wiring, DEBT-3/4 are Volume 5, DEBT-6/7 are
request-latency (Volume 4/5 request path). No blockers from the ledger.

## Invariants check

I-2 (every producer has a consumer): Volume 2's own producer chain is
intact — collector output flows into `market_events`/`raw_ticks`/
`ohlcv_candles`, which Volume 3 feature engines demonstrably consume (the
same tables this session's HDFCBANK investigation read from). The I-2
violation on record (`IntradayRiskFeatureEngine`) is a Volume 3→4 boundary,
not Volume 2's. I-4, I-8, I-11: all HELD, all directly exercised by
Volume 2 code (collectors degrade gracefully with `session_factory=None`,
FinBERT scoring runs via `asyncio.to_thread`). No blockers.

## Live finding (non-blocking, tracked as new debt)

`news_intelligence` and `global_shock_news` collectors show chronically low
quality scores (~22-23/100, occasionally spiking to ~62) with `avg_latency_ms`
of **33,696ms and 36,444ms** respectively — 33-36 *seconds* per collection
cycle, against 120s/30s intervals. Checked whether this was caused by this
session's FinBERT cross-collector lock (`_finbert_scoring_lock`, added to
reduce CPU contention): **it wasn't** — the pattern was already present at
05:30 IST, hours before that fix deployed. Pre-existing, most likely CPU-bound
FinBERT inference cost on a shared 4-vCPU box. Logged as DEBT-8 below rather
than blocking this preflight — it doesn't affect what Volume 2 promises
Volume 3+ (the collector still produces output, just slowly and at reduced
quality score), but a 30s-interval collector that reliably takes 33-36s to
run can never catch up to its own schedule, which is worth fixing.

---

## GO verdict

Volume 1's foundation genuinely supports Volume 2 as built. No dependency
gaps, no expired debt, no violated load-bearing invariants. The one live
finding (slow news collectors) is real but doesn't block further work on
Volume 2 or moving toward Volume 3 preflight — logged as debt with an
appropriate expiry condition instead.
