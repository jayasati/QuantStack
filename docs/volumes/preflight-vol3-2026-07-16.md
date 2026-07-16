# Volume 3 Preflight — Feature Store & Market Intelligence Platform (2026-07-16)

**Scope note:** Re-run of the 2026-07-15 Volume 3 preflight
(`preflight-vol3-2026-07-15.md`, verdict GO), requested one day later after a
day of substantial change: tick-aggregation layer added (`TickCandleAggregator`),
watchlist expanded 3 → 25 symbols, DEBT-6 resolved, DEBT-7/8 partially fixed,
multiple redeploys. Same retroactive framing as before — Volume 3 is already
built; this verifies its foundation and its own live behavior *today*. Facts
yesterday's report established from code/tests (module presence, test
coverage, the Ch.1 no-bypass grep) are inherited, not re-derived; everything
live was re-measured fresh.

**Method:** live SSH/SQL/API checks per `prompts/VERIFY-COOKBOOK.md`
(all run 18:39–18:50 IST, 2026-07-16, market closed), repo inspection for
endpoint signatures.

**Verdict: GO.** Stronger than yesterday's: the one live degradation
yesterday's report flagged (IntradayRiskFeatureEngine stalled 6h) is
verifiably gone, and the one level-3 gap it left open (replay never
exercised live) is now closed with a ground-truth cross-check. DEBT-9
(feature selection never run live) remains the single unmet Volume 3
acceptance criterion and is the natural first build chunk.

---

## Dependency table (live evidence re-measured today)

| Dependency | Code | Tests | Live (2026-07-16) | Verdict |
|---|---|---|---|---|
| Stack up and clean (§1) | — | — | ✅ all 3 containers healthy (backend up ~1h since the evening DEBT-7 deploy, postgres 45h, redis 2d); backend logs last 15m: zero error/exception lines after the known Angel One 403 filter | PASS |
| Raw events feeding feature engines (Vol 2 output) | ✅ inherited | ✅ inherited | ✅ `raw_ticks` latest 15:35:55 IST (seconds after close — `live_market` is `market_hours_only`, correct); `ohlcv_candles` HDFCBANK: 1m/5m through 15:35, 3m 15:33, 15m/1H 15:30, 30m 15:15, all consistent with a 15:30 close — the new tick-aggregation layer is live and current | PASS |
| Feature Registry + metadata tables (Ch.5, Ch.8) | ✅ inherited | ✅ inherited | ✅ `feature_registry` 1,075 / `feature_versions` 1,075 / `feature_dependencies` 800 (static, unchanged — expected) | PASS |
| Feature Quality Engine (Ch.22) | ✅ | ✅ | ✅ `feature_quality` **169,742 rows** (58,777 yesterday — +111k/day, actively scoring the 25-symbol watchlist) | PASS |
| Feature Statistics | ✅ | ✅ | ✅ `feature_statistics` **161,096** (was 56,847) | PASS |
| Feature Drift Engine (Ch.23) | ✅ | ✅ | ✅ `feature_drift` **50,734** (was 11,185) | PASS |
| Online store — Redis (Ch.4) | ✅ | ✅ | ✅ DBSIZE 180; **26 `qs:features:*:D` keys** = 25 watchlist symbols + MARKET (DEBT-6 resolution holds) | PASS |
| Offline store — Parquet (Ch.4) | ✅ | ✅ | ✅ **16,010 `.parquet` files** (5,134 yesterday — tripled with the watchlist expansion) | PASS |
| Feature API (Ch.26) | ✅ | ✅ | ✅ `GET /features/latest/HDFCBANK?timeframe=5m` returns fresh intraday features (ts 15:35 IST) | PASS |
| **Historical Replay Engine (Ch.25)** — yesterday's one unexercised level 3 | ✅ | ✅ | ✅ **now verified live**: `GET /features/replay/HDFCBANK?as_of=2026-07-16T12:00+05:30&timeframe=5m` returned `intraday_move_from_open_pct = -0.6977171185507114` at ts exactly 12:00 IST — byte-identical to the direct SQL ground-truth query, and different from the 15:35 end-of-session value (-1.0588), i.e. no look-ahead | PASS |
| Ch.1 no-bypass rule (nothing reads collectors/raw tables directly) | ✅ inherited (2026-07-15 grep; no intelligence-layer changes since touch raw tables) | — | — | PASS (inherited) |
| Feature Selection Engine (Ch.24) | ✅ | ✅ unit-level | ❌ `feature_usage` **still 0 rows** — unchanged, DEBT-9 | PASS in code, **UNVERIFIED live** (DEBT-9) |
| **IntradayRiskFeatureEngine production** (yesterday: stalled 11:30→17:09) | ✅ | ✅ | ✅ **fully recovered**: HDFCBANK 5m features span **77 distinct buckets, 09:15 → 15:35 IST, largest gap exactly 00:05:00** (zero missed buckets, full session), across **all 25 watchlist symbols**. Also new `quote` (15:30) and `chain` (15:35) timeframe features, fresh through close | PASS |

## DEBT ledger check (step 3)

- **DEBT-2** — the producer half is demonstrably healthy today (the 77-bucket,
  zero-gap evidence above); the original stall's root cause was external
  (broker candle backend) and the tick-aggregation layer now removes the
  single-point dependency on it. The **consumer-wiring half is still open**
  (nothing reads these 5m features — that's DEBT-1/Volume 4 scope, expiry
  unchanged, not triggered by Volume 3 work).
- **DEBT-9** — the only entry whose expiry condition Volume 3 work plausibly
  triggers: "when feature/model selection quality is next worked on."
  Ch.24 *is* Volume 3 scope, and "feature selection identifies the strongest
  predictors" is Volume 3's own acceptance criterion. If `/volume-build 3`
  proceeds, DEBT-9 must be resolved in that build (schedule the engine, or
  explicitly decide on-demand-only and re-word the ledger entry as accepted) —
  it cannot be deferred through a Volume 3 build phase.
- DEBT-1 (Vol 4/5 signal wiring), DEBT-3 (outcome evaluator, Vol 5),
  DEBT-4 (events.score candidacy, Vol 5), DEBT-7 (request latency, Vol 5
  request path), DEBT-8 (news collectors, Vol 2 — market-hours re-check
  planned 2026-07-17): none triggered by Volume 3 work. No expired entries.

## Invariants check (step 4)

- **I-2 (VIOLATED)** — the violation is Volume 3's *output* side
  (unconsumed intraday features), not something Volume 3 builds on top of.
  Materially improved today: the producer is reliable again, so fixing the
  consumer (Volume 4) is no longer "building on sand" as yesterday's report
  put it. Not a Volume 3 blocker.
- **I-1, I-5 (VIOLATED)** — signal-layer invariants (Vol 4/5 scope); Volume 3
  neither depends on nor deepens them.
- **I-7 (VIOLATED)** — roadmap overstatement concerns Vol 5.5+, not Vol 3.
- **I-3, I-4, I-8** — HELD, all directly exercised by Volume 3 code (the
  bounded `DISTINCT ON`, `asyncio.to_thread` offloading, no-DB degradation
  tests).
- No spec-vs-invariant contradiction: Volume 3's spec is timeframe-agnostic
  and the store now demonstrably carries fresh intraday (5m/quote/chain)
  features alongside D.

## Acceptance criteria snapshot (Vol 3 Ch.27, as of today)

8 of 9 hold with live evidence (raw→features, online/offline sync, metadata/
versions, quality scores, drift, replay, APIs, no-bypass). The 9th —
"feature selection identifies the strongest predictors" — has never executed
live (DEBT-9).

---

## GO verdict

Volume 3's foundation and its own live behavior are in the best measured
state they've ever been: every dependency passes level 3 today, yesterday's
two soft spots (intraday stall, replay unexercised) are both closed with
fresh evidence, and the metadata pipeline is visibly digesting the 25-symbol
watchlist at scale. The single remaining gap is DEBT-9, which is Volume 3's
own scope and therefore the recommended **first build chunk** for
`/volume-build 3`: run `FeatureSelectionEngine` live (via
`POST /features/selection/{symbol}`), verify `feature_usage` populates, then
either schedule it in `main.py` or record an explicit on-demand-only
decision in DEBT.md.
