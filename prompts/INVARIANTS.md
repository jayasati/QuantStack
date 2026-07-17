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

**Substantial improvement 2026-07-16:** all 5 named components (trend,
market_structure, momentum, volatility, relative_strength) now blend a 5m
intraday overlay (DEBT-1, resolved) with real weight, and the blended read
is live-verified to respond to same-day price action, not just tomorrow's
D bar (HDFCBANK: bullish D-evidence, but a real -1.06% session move
dropped trend confidence to 0.49 and shifted the dominant state to
`range_bound`, persisted via the scheduled `market_intelligence_sweep`,
not just a manual check; relative_strength separately confirmed live
folding in both Nifty and Sensex). Still recorded **VIOLATED**, not HELD,
for two reasons that are about rigor of verification, not missing scope:
(1) the underlying D-based sub-features (`ms_trend_direction`,
`price_momentum_*`, `rs_*_strength_*`, etc.) still only update at midnight
— only the *blended* output now refreshes faster, the D-based half of the
blend is unchanged; (2) both build chunks were built and deployed after
market close, so "updates in near-real-time during a live session" is
verified against the day's *final* snapshot only — a genuine live-market-
hours check (watching the blended read visibly change across consecutive
5-minute ticks during an actual session) hasn't been done yet. Upgrade to
HELD only after that market-hours check.

**More raw data, same gap, 2026-07-17:** `feature_timeframes` now includes
"5m" (DEBT-15), so price/volume/volatility/liquidity/structure/risk/
relative_strength genuinely compute at 5m resolution, not just the narrow
9-feature `IntradayRiskFeatureEngine` overlay. This does NOT move I-1
toward HELD — every intelligence-layer read is still hardcoded to
`timeframe="D"` (DEBT-15), so the new intraday data exists and is stored
but nothing in the decision-making layer reads it. Recording this
precisely so "we compute 5m features now" is never mistaken for "I-1 is
closer to resolved" — the D-only *read* pattern is I-1's actual substance,
unchanged by adding more unread intraday *writes*.

## I-2 · Every producer has a consumer
Every feature, table column, engine output, or event written by the system
must have at least one downstream consumer, or a DEBT.md entry naming when a
consumer will exist. Write-only outputs are dead code wearing a disguise.

**Status: VIOLATED** — `IntradayRiskFeatureEngine` writes 5m-timeframe
features nothing in Volume 4/5 reads (DEBT-2). Historical precedent:
`CompositeMarketIntelligenceEngine` sat unwired for weeks (since fixed).
**New instance, 2026-07-17:** 8 more engines' 5m output joins the same
unconsumed pile (DEBT-15) — deliberately logged with an expiry condition
at write time, not discovered later the way DEBT-2 was.

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
outcome evaluator and win-rate metric do not (DEBT-3). **Sharpened
2026-07-16 (Volume 5 preflight):** the gap is more fundamental than "we
don't measure outcomes" — DEBT-13 found the full conviction/qualification
pipeline has never run live, so there is currently no genuine signal for
I-5's evaluator to even measure yet, beyond raw candidate detection.

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
IRR; not yet corrected. **New instance, 2026-07-16 (Volume 5 preflight):**
roadmap.md's Volume 5 row is "✅" with no linked postflight, and DEBT-13
found 12 of its 16 modules have literally never executed live — the
same shape of overstatement the IRR found for 5.5+, now confirmed to
extend to Volume 5 itself. Not corrected here (roadmap only updates after
a passing postflight, and Volume 5 hasn't had one yet) but named
explicitly rather than left to compound further.

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

## I-11 · No push to main without a green local suite
This project deliberately has no CI (solo contributor, no Actions billing —
tried twice, see `docs/backlog.md`; do not re-add without reading that note
first). The full local suite substitutes for it and is not optional: before
any push to `main`, `cd backend && python -m pytest app/tests -q` must be
green, run in the same session as the push, not assumed from a prior run.
`ruff check app` / `mypy app` before push where the change touches typed
code. This is the actual gate — treat it with the seriousness a red CI
check would get, not more casually just because nothing external enforces
it.

**Status: HELD** — this has been the working rhythm of every deploy since
2026-07-14 (full suite run and reported before each push, cookbook §10).
Never again treat a workflow file's mere existence as evidence of anything —
that specific mistake (`edcfabf` re-adding CI without checking why it was
removed) cost 4 days of silent zero-signal pushes before the 2026-07-15
Volume 1 postflight caught it. See DEBT.md's Resolved section.
