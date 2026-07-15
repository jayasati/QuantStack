# System Invariants

Rules the whole system must satisfy, regardless of which volume is being
built. Every build/seam-check/postflight prompt checks its work against this
list. Each invariant records its **current live status** — an invariant this
file claims is HELD must have been verified live (see VERIFY-COOKBOOK.md), not
assumed. Marking one HELD without evidence is itself a violation of I-7.

Status legend: **HELD** / **VIOLATED** (with known cause) / **UNVERIFIED**.

---

## I-1 · Signal freshness (the HDFCBANK invariant)
During market hours (09:15–15:30 IST), any directional signal presented for a
trading decision must derive from market data no older than 15 minutes.
Directional intelligence must not depend solely on "D"-timeframe features
(which update once, at midnight).

**Status: VIOLATED** (2026-07-15) — trend / market_structure / volatility /
momentum / relative_strength intelligence all read only `timeframe="D"`
features. Verified live: `ms_breakout_probability`, `ms_trend_direction`,
`ms_structural_bias` for HDFCBANK last updated 00:00 IST; the system held a
"long" bias through a real 1.1% collapse at 12:45 IST. See DEBT-1.

## I-2 · Every producer has a consumer
Every feature, table column, engine output, or event written by the system
must have at least one downstream consumer, or a DEBT.md entry naming when a
consumer will exist. Write-only outputs are dead code wearing a disguise.

**Status: VIOLATED** — `IntradayRiskFeatureEngine` writes 5m-timeframe
features nothing in Volume 4/5 reads (DEBT-2). Historical precedent:
`CompositeMarketIntelligenceEngine` sat unwired for weeks (since fixed).

## I-3 · No unbounded reads at production scale
Any query against feature_store / market_events / ohlcv_candles must be
bounded by a LIMIT or a time window, and any new/changed query on these
tables must be shown efficient at live scale (EXPLAIN ANALYZE on the VM or a
production-scale fixture). feature_store holds 170k–324k rows per
symbol/timeframe; Postgres has no index skip-scan, so "the index will handle
it" is not an argument — measure it.

**Status: HELD** (2026-07-15) — after the DISTINCT ON regression was bounded
to a 14-day window (verified 374ms → 8.8ms via live EXPLAIN ANALYZE).

## I-4 · The event loop is for I/O
No synchronous CPU work beyond trivial cost runs directly on the asyncio
event loop. Pure-compute passes go through `asyncio.to_thread` (the
convention `BaseFeatureEngine.run()` established).

**Status: HELD** (2026-07-15) — trend/volatility/correlation/analogs offloaded
this session; py-spy previously caught all four blocking the loop mid-request.

## I-5 · Signals are outcome-accountable
Every emitted trade signal must be evaluable after the fact: it carries
`as_of` and `valid_until`, and the system records whether the predicted move
actually happened. A signal generator whose accuracy nobody measures will
happily say "long" forever.

**Status: VIOLATED (partially)** — `valid_until` exists (2026-07-15); the
outcome evaluator and win-rate metric do not (DEBT-3).

## I-6 · API compatibility is additive
Fields in persisted payloads and API responses are added, never renamed or
removed, without an explicit migration note in the commit and a DEBT.md entry
for the old field's removal. (Precedent: `as_of_ist` / `valid_until` /
`signal_since` were all added alongside existing fields, never replacing.)

**Status: HELD.**

## I-7 · Status claims match measured reality
roadmap.md, volume specs, and completion claims must agree with the latest
audit/postflight evidence. A volume is "complete" only when its postflight
report says so, with a link. (The 2026-07-11 IRR found roadmap claiming
completeness at a measured 45/100 readiness.)

**Status: VIOLATED** — roadmap.md still overstates Vol 5.5+ status per the
IRR; not yet corrected.

## I-8 · Graceful degradation everywhere
Every engine constructed with `session_factory=None` (and/or missing cache,
bus, broker) degrades to honest empty output — never raises, never fabricates.
This is the codebase-wide convention and every new engine follows it.

**Status: HELD** — enforced by the existing test suite's no-DB tests.

## I-9 · Deploys are verified, not assumed
A deploy is: push → pull on quantstack-vm → rebuild → container healthy →
logs clean → live behavior measured (latency and/or the changed behavior
observed with real data). Restarts during market hours need explicit user
approval — the container recreate drops live collection for ~10-15s.

**Status: HELD** — this was the working rhythm of 2026-07-14/15.

## I-10 · Market-wide signals are labeled as such
A market-wide input (events, breadth, macro, sector, correlation,
institutional_flow) must never masquerade as symbol-specific evidence. When a
market-wide trigger fires for every symbol simultaneously (e.g. events.score
put all 6 watchlist symbols into candidates with priority 0.00), that is one
signal, not six.

**Status: UNVERIFIED as a rule** — the events.score behavior is real and
currently by-design; whether it should gate candidacy at all is DEBT-4.

## I-11 · CI is green before it's trusted
"CI exists" and "CI works" are different claims — never treat the presence of
a workflow file as evidence pushes are checked. Before relying on CI for any
decision (starting a new volume, calling a volume complete), confirm via the
GitHub Actions API or UI that recent runs actually executed steps and
passed — not just that the workflow file is well-formed.

**Status: VIOLATED** — `.github/workflows/backend-tests.yml` exists and is
well-formed, but 0 of the last 5 runs on `main` executed a single step
(each completed in <1s, no runner assigned) — verified via the GitHub API,
2026-07-15. Every commit this session shipped with zero CI signal. See
DEBT-5, `docs/volumes/postflight-vol1-2026-07-15.md`.
