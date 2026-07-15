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
stalled live: last write 10:25 IST on 2026-07-15, >5h stale mid-session —
root cause not yet investigated.
**Expiry condition:** Same as DEBT-1 (they resolve together: the natural fix
for DEBT-1 consumes this engine's output — which first requires it to run
reliably).
**Logged:** 2026-07-15.

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

### DEBT-5 · CI exists but is completely non-functional — REVISED, now top priority
**What:** ~~No CI/CD~~ — corrected by the 2026-07-15 Volume 1 postflight:
`.github/workflows/backend-tests.yml` exists and is well-formed (added in
`edcfabf`, addressing the original 2026-07-11 IRR finding), but 0 of the last
5 runs succeeded. Each failing run's own timestamps show `created_at` ==
`started_at`, completing within 1 second with zero steps recorded — GitHub
never assigned a runner. Root cause is a repo/org GitHub Actions permission
setting (not diagnosable or fixable from code) — check
github.com/jayasati/QuantStack → Settings → Actions → General. Every commit
pushed on 2026-07-15, including all of this session's perf fixes, shipped
with zero CI signal.
**Risk while open:** False sense of safety net — the team believes pushes are
checked; none have been.
**Expiry condition:** Before the next volume's build phase begins. This entry
now blocks ahead of everything else in this ledger.
**Logged:** 2026-07-15 (revised from the 2026-07-09 audit's "no CI/CD" note;
see `docs/volumes/postflight-vol1-2026-07-15.md`).

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

---

## Resolved

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
