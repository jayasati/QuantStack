# Implementation Backlog

A running register of deferred work — things designed or stubbed during implementation that need to be completed later. When you finish an item, move it to the Done section with the commit reference. Keep this file honest: if something is mocked, degraded, or waiting on an external decision, it belongs here.

!!! note "How to use"
    Each item lists **where it lives in code**, **what's missing**, and **what unblocks it**. Items are grouped by the volume that specifies them.

## Volume 2 — Data Collection

### Domain collector data sources (high priority)

The seven domain collectors are fully implemented and tested against injectable sources, but their **real feeds are not wired**. Each currently degrades gracefully with `"<domain> source not configured"`. To activate one, implement its `*Source` ABC and register it in the collector's constructor wiring.

| Collector | Source interface (file) | Real feed candidates |
|-----------|------------------------|---------------------|
| Market breadth | `BreadthSource` (`app/collectors/domains/breadth.py`) | Broker quotes for the NIFTY 500 universe (compute EMAs from our own `ohlcv_candles`) |
| Sector rotation | `SectorSource` (`app/collectors/domains/sector.py`) | NSE sectoral index quotes (broker tokens exist in scrip master) |
| Macro intelligence | `MacroSource` (`app/collectors/domains/macro.py`) | Yahoo Finance / FRED / broker MCX+CDS quotes |
| Event calendar | `EventCalendarSource` (`app/collectors/domains/events.py`) | NSE corporate actions + RBI/Fed calendars (scrape or static schedule seed) |
| News intelligence | `NewsSource` (`app/collectors/domains/news.py`) | RSS: Moneycontrol, Economic Times markets, Business Standard, Reuters India |
| Institutional flows | `FlowSource` (`app/collectors/domains/flows.py`) | NSE FII/DII provisional data, NSE block/bulk deal reports |

### Other Volume 2 items

- **Options: Greeks and IV percentile** — NSE's chain carries no Greeks, so gamma/delta exposure only emits if a Greeks-bearing source is added (Angel One's optionGreek endpoint is a candidate). IV percentile needs an ATM-IV history window — derivable from our own stored `atm_iv` market events once enough days accumulate.
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
| Options intelligence real feed (Prompt 2.4) | `app/collectors/sources/nse_options.py` — NSE option-chain v3 API (cookie handshake, expiry from contract-info, 30s fetch throttle); previous-day spot for buildup classification comes from our own daily candles. Verified live: 16 features derived for NIFTY + BANKNIFTY. |
| SmartAPI WebSocket streaming (Prompt 2.2) | Implemented in `app/market/angel_ws.py` — binary LTP/Quote parsing, heartbeat, reconnect with backoff, per-symbol REST fallback in `LiveMarketCollector`. Verified against the live feed (auth + real packets). |
| Instrument lookup (Prompt 2.1) | `app/market/instruments.py` — index token map + scrip master download with daily cache. |
| Live/historical collectors against real broker | Verified: 4,192 candles across 7 timeframes, dedup and backfill-resume confirmed. |
