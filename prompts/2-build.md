# Phase 2 · Build — the template every volume-N build prompt follows

Precondition: a GO preflight report for volume N exists in `docs/volumes/`
(dated within the current work stretch). If it doesn't, stop and run
`/volume-preflight N` first.

Work through the volume spec chunk by chunk (one chapter/prompt per chunk,
matching the existing per-prompt commit rhythm). For **every chunk**, fill in
and satisfy all five sections below before calling it done. If a section is
genuinely not applicable, say so explicitly and why — silence is not N/A.

## The five sections

### 1 · Contract
Before writing code, state:
- **Consumes:** each input with its source, timeframe, and the freshness this
  chunk requires of it. If preflight didn't verify that input live, verify it
  now (cookbook §2).
- **Produces:** each output, and — by name — what consumes it. If the honest
  answer is "nothing yet," either add the consumer in this same chunk or add
  a DEBT entry with an expiry condition before writing the producer (I-2).
  No write-only outputs.

### 2 · Backward compatibility
List what existing behavior this chunk touches: API response shapes
(additive-only, I-6), persisted payload formats, engine constructor
signatures (new params optional-with-None-default — the codebase convention),
scheduled job ids, event types. Existing tests must keep passing unmodified;
if a test must change, justify it in the commit message.

### 3 · Implementation
Follow the codebase's established conventions: graceful degradation with
`session_factory=None` (I-8), `asyncio.to_thread` for CPU passes (I-4),
bounded queries (I-3), DI registration in `container.py`, market-wide vs
symbol-specific signals kept distinct (I-10). Match surrounding comment
density and style. Deferrals go to `prompts/DEBT.md` with expiry conditions,
not docstrings.

### 4 · Tests — including at scale
- Unit tests for the pure logic; a no-DB degradation test.
- **At least one production-scale check** for any new/changed query on
  feature_store / market_events / ohlcv_candles: EXPLAIN ANALYZE on the VM
  (cookbook §4) or a fixture at realistic row counts. Record the measured
  numbers in the commit message. A 10-row fixture proving a 170k-row query
  is a lie with a green checkmark.
- Full suite before every deploy:
  `cd backend && python -m pytest app/tests -q` (~5 min, must be green).

### 5 · Live verification
Deploy per cookbook §10 (market-hours restarts need explicit user approval)
and observe the changed behavior itself with real data — not just "container
healthy." Latency check (§5) if the request path was touched. State plainly
in your report what you observed vs. what you expected; if they differ,
that's a finding, not a footnote.

## Commit rhythm
One commit per chunk, sole author, no co-author trailers. Message explains
why, references the audit/debt item if the chunk resolves one, and includes
measured evidence for perf-relevant changes.

## Cadence rule
After every 2-3 chunks — or immediately after any chunk that adds a new
producer — run `/volume-seam-check N` before continuing.
