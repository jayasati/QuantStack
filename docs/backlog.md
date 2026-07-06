# Implementation Backlog

A running register of deferred work — things designed or stubbed during implementation that need to be completed later. When you finish an item, move it to the Done section with the commit reference. Keep this file honest: if something is mocked, degraded, or waiting on an external decision, it belongs here.

!!! note "How to use"
    Each item lists **where it lives in code**, **what's missing**, and **what unblocks it**. Items are grouped by the volume that specifies them.

## Volume 2 — Data Collection

### Domain collector data sources

**All nine collectors now run on real feeds** — see the Done table below. The injectable `*Source` interfaces remain for tests and future feed swaps.

### Other Volume 2 items

- **Breadth universe size** — the breadth source tracks NIFTY 50 constituents (configurable `index` parameter). Expanding to NIFTY 500 needs ~500 daily-candle fetches for the EMA cache (~3 minutes once a day at broker rate limits) — decide whether the extra coverage is worth it.
- **DLQ persistence** — dead letters live in a bounded in-memory deque (1000) with inspection + replay APIs; they are lost on restart. Persist to a table if replay-after-crash ever matters.
- **Always-on hosting** — the stack collects only while the host machine runs Docker; today's session lost ~3.5 market hours to host sleep (observations stopped 12:08 IST). For unattended collection, deploy to an always-on box (small VPS or home server) — full production deployment is Volume 10, but a minimal `docker compose up -d` on a VPS works today.
- **Macro event schedule maintenance** — `configs/event_calendar.yaml` holds officially scheduled macro events (RBI MPC, FOMC, ECB, BoJ, US CPI, MSCI/FTSE rebalances). It ships with recurring rules for India CPI (12th monthly) and the Union Budget (Feb 1); exact central-bank dates must be added by hand from the official calendars listed in the file header when published.
- **INDIA10Y factor** — no public Yahoo ticker for the India 10-year yield; the factor is omitted (weights renormalize). Candidates: RBI/CCIL data or investing.com. Also: CRYPTO_MCAP uses BTC-USD as a documented proxy.
- **Insider/promoter values (PIT)** — NSE's corporates-pit API currently returns empty data regardless of parameters, so promoter buy/sell values and insider net stay at 0 with `insider_data_available: false`. The parser is ready; re-check the endpoint periodically or find an alternate disclosure feed.
- **IV percentile ramp-up** — implemented; starts emitting automatically once ≥100 ATM-IV observations accumulate (~10:55 IST on the first full trading day).
- **FinBERT sentiment** — `SentimentProvider` in `app/collectors/domains/news.py` currently uses a lexicon. Swap in FinBERT (or another finance model) behind the same interface. Unblocked by: deciding on the inference dependency (transformers/onnx) and its container size cost.
- **Market depth over WebSocket (20-level)** — the feed parses LTP and Quote modes (51/123 bytes); Snap Quote mode (379 bytes, includes 5-level depth) is not parsed yet. `app/market/angel_ws.py`.
- **Raw tick retention policy** — `raw_ticks` grows unbounded at one row per symbol per 15s. Needs a retention/aggregation job (e.g., keep 7 days raw, downsample the rest).
- **TimescaleDB / partitioning** — `ohlcv_candles` is a plain table; the Volume 1 stack notes TimescaleDB as optional later. Revisit when candle volume grows past ~10M rows.
- **Options/order-book methods on BrokerInterface** — Prompt 2.1 lists options chain and order book; the interface currently exposes quotes + candles only. Extend when the options collector's real source lands.

## Volume 1 — Foundation

- **CI / automated checks** — GitHub Actions workflows were removed by choice (account billing lock; owner opted for plain git version control). The last version of the CI workflow (ruff, mypy, migration up/down/up, pytest against live Postgres/Redis, Docker build) and the Pages deploy workflow live in git history at commit `02275cc` — restore them from there if CI is ever wanted again. Until then: run `pytest`, `ruff check app`, and `mypy app` locally before pushing, and publish docs with `mkdocs gh-deploy --force --no-history`.
- **`develop` branch discipline** — the branch exists; day-to-day work is still landing on `main`. Adopt feature branches once more than one person works on the repo.

## Volume 3+ — Not started

Volumes 3 (Feature Store), 4 (Market Intelligence), 5 (Prediction & Conviction) and beyond are specified in [the volume docs](volumes/volume-3.md) but not yet implemented. The `market_events` stream and `ohlcv_candles` produced by Volume 2 are their inputs.

## Done

| Item | Resolution |
|------|-----------|
| Redis caching wired into production (Prompt 2.14) | The CacheService existed but had zero call sites — every source used process-local caches that died on restart. Breadth EMA snapshots (50 broker calls) and sector window returns (13 calls) are now Redis-backed with graceful degradation when Redis is down, plus a `GET /collectors/cache/metrics` endpoint. Verified: post-restart breadth run dropped from 29.8s to 1.4s with a 100% cache hit rate. |
| Event bus DLQ inspection + replay (Prompt 2.13) | `GET /collectors/events/dead-letters` lists failed events (error, handler, attempts, trace id); `POST /collectors/events/dead-letters/{id}/replay` re-publishes with a fresh event id (bypasses idempotency) while preserving the trace chain. |
| Collector Registry full spec (Prompt 2.12) | Dependency resolution added: collectors declare `depends_on`, the registry validates unknown dependencies, topologically orders scheduling (cycle detection), and `disable` reports still-enabled dependents. Scheduling is now configurable via `collector_intervals` in settings (env `COLLECTOR_INTERVALS='{"news_intelligence": 300}'`), with default and effective intervals both exposed in the list API. |
| Data Quality Engine full spec (Prompt 2.11) | All eight dimensions now real: schema validity measures actual validate()-stage drop rates, missing values scans metadata nulls (distinct from completeness), and historical reliability averages the last 50 persisted quality scores across restarts (15-min cached). New monitoring endpoint `GET /collectors/{name}/quality` serves persisted history + current components. |
| News intelligence real feed (Prompt 2.10) | `app/collectors/sources/rss_news.py` — RSS 2.0 from Economic Times Markets, Moneycontrol (markets + economy), and LiveMint Markets; HTML/entity cleanup, RFC822->ISO timestamps, per-feed failure tolerance, 90s cache. Business Standard dropped (403s non-browser clients). Verified live: 92 unique articles classified across all six categories. |
| Event calendar real feed (Prompt 2.9) | `app/collectors/sources/nse_events.py` — dividends/bonus/splits from NSE corporateActions ex-dates, results from the NSE event-calendar, IPOs from ipo-current-issue, F&O expiries from option-chain contract-info, plus the maintained `configs/event_calendar.yaml` for scheduled macro events. Verified live: 49 events in the 7-day window (27 dividends, 17 results, IPO, expiry, India CPI). |
| Macro intelligence real feed (Prompt 2.8) | `app/collectors/sources/yahoo_macro.py` — Yahoo chart API for 13 factors (USDINR, DXY, US10Y, crude, gold, silver, natgas, SPX/NDX/Nikkei/HangSeng/DAX, BTC proxy); z-scores and 1-day changes computed from 3 months of daily closes. Verified live: 14 records incl. Macro Pressure Score, quality 99.15. |
| Institutional flows real feed (Prompt 2.7) | `app/collectors/sources/nse_flows.py` — FII/DII from NSE fiidiiTradeReact, block/bulk deals from the large-deal snapshot (value = qty x price), SAST filing counts from corporate-sast-reg29. 20-day flow averages come from our own stored history (same-day gross/4 as bootstrap scale). Verified live: 134 records, FII +1355cr / DII -1954cr. |
| Sector relative volume (Prompt 2.6) | Today's per-index volume from NSE `equity-stock-indices` (10-min cache), ratioed against our own stored end-of-day volume history; neutral 1.0 until ≥3 days accumulate (starts activating ~2026-07-09). Benchmark raw entry now persists in the summary record for history queries. |
| Options Greeks live activation | Confirmed live on 2026-07-06: gamma_exposure and delta_exposure emitting with real Angel One Greeks during market hours. |
| Prompt 2.4 completion: OI/volume distribution, IV percentile, Greeks, market-hours gating | `oi_distribution` (put-wall vs call-wall positioning) and `volume_distribution` (concentration, volume PCR, volume-weighted strike) verified live. IV percentile computes from our own stored ATM-IV history (min 100 observations). Greeks enrichment via Angel One `optionGreek` merges delta/gamma into chain legs. Market-hours-only collectors skip scheduled runs outside NSE hours (manual `/run` bypasses). |
| Market breadth real feed (Prompt 2.5) | `app/collectors/sources/nse_breadth.py` — NIFTY 50 constituents + live quotes from NSE `equity-stock-indices`; EMAs (20/50/100/200) computed from broker daily candles with a daily cache. Verified live: 16 breadth metrics from 50 real constituents. |
| Sector rotation real feed (Prompt 2.6) | `app/collectors/sources/broker_sectors.py` — twelve NSE sectoral indices via broker daily candles (4h cache). Sector list adjusted to indices that actually exist (PSU Bank / Private Bank replace Capital Goods / Defence, which have no NSE index in the broker universe). Verified live: 13 records. |
| Options intelligence real feed (Prompt 2.4) | `app/collectors/sources/nse_options.py` — NSE option-chain v3 API (cookie handshake, expiry from contract-info, 30s fetch throttle); previous-day spot for buildup classification comes from our own daily candles. Verified live: 16 features derived for NIFTY + BANKNIFTY. |
| SmartAPI WebSocket streaming (Prompt 2.2) | Implemented in `app/market/angel_ws.py` — binary LTP/Quote parsing, heartbeat, reconnect with backoff, per-symbol REST fallback in `LiveMarketCollector`. Verified against the live feed (auth + real packets). |
| Instrument lookup (Prompt 2.1) | `app/market/instruments.py` — index token map + scrip master download with daily cache. |
| Live/historical collectors against real broker | Verified: 4,192 candles across 7 timeframes, dedup and backfill-resume confirmed. |
