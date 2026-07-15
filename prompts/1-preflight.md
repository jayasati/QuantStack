# Phase 1 · Preflight — run BEFORE starting volume N

You are about to start building volume **N** (passed as the argument). Your
job in this phase is to establish ground truth about what volumes 1..N-1
*actually* deliver — in code, in tests, and live on the VM — and decide
GO / NO-GO. You write no feature code in this phase. Fixing a blocker found
here is allowed (as its own commits), but building volume N is not.

Read first: `prompts/INVARIANTS.md`, `prompts/DEBT.md`,
`prompts/VERIFY-COOKBOOK.md`, and the spec `docs/volumes/volume-{N}.md`
(dots become dashes: 5.5 → volume-5-5.md). Also read the most recent
`preflight-*`, `postflight-*`, and audit reports in `docs/volumes/` — do not
re-discover what a previous report already established; re-verify only what
volume N depends on.

## Steps

1. **Extract volume N's upstream dependencies.** From the spec, list every
   input volume N assumes exists: tables, features (name + timeframe +
   expected cadence), engines, API routes, events, config. Be concrete —
   "market structure features" is not a dependency; "`ms_breakout_probability`
   on timeframe D, fresh within one feature_engine_interval" is.

2. **Verify each dependency at three levels.** For every item:
   - **Code**: the producer exists and is wired (registered in
     `container.py`, scheduled in `main.py` if periodic — existence of a
     class proves nothing; `CompositeMarketIntelligenceEngine` sat unwired
     for weeks).
   - **Tests**: something exercises it beyond its own unit tests.
   - **Live**: it is actually producing on quantstack-vm right now, at the
     cadence volume N needs (cookbook §2 freshness SQL, §7 Redis, §1 health).
     This level is mandatory — the code and the VM have disagreed before
     (IntradayRiskFeatureEngine: code fine, live stalled 5h).

3. **Check the debt ledger.** For each Active entry in `prompts/DEBT.md`:
   does starting volume N trigger its expiry condition? Expired entries are
   blockers.

4. **Check invariants.** Any invariant currently VIOLATED that volume N would
   build on top of is a blocker (building signal logic on top of I-1 while
   it's violated just deepens the hole).

5. **Write the report** to `docs/volumes/preflight-vol{N}-{YYYY-MM-DD}.md`:
   - Dependency table: dependency | code | tests | live | verdict, with the
     actual evidence (query outputs, timestamps) inline — future audits must
     be able to check your work.
   - Blockers (anything failing level-3 verification, expired debt, violated
     load-bearing invariants) vs. non-blocking observations.
   - **GO / NO-GO verdict.**

6. **Report to the user.** If NO-GO: list the blockers with estimated fix
   scope and stop — the user decides whether to fix first or explicitly
   accept the risk (record any acceptance in DEBT.md with a new expiry
   condition). If GO: name the first build chunk and confirm the user wants
   to proceed to `/volume-build N`.

## Hard rules

- Never mark a dependency verified from reading code alone. Level 3 or it
  isn't verified.
- Never soften a NO-GO to be agreeable. The 2026-07-11 IRR exists because
  optimistic status reporting compounded for weeks.
- If the spec itself contradicts an invariant (e.g. specs a daily-only
  signal for an intraday system), flag the contradiction in the report
  rather than silently building either side of it.
